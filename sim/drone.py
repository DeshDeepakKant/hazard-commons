"""Verification drone: real Crazyflie cf2x dynamics + vendored DSLPIDControl (gym-pybullet-drones).

Spinal cord only: attitude/position PID, a moving "carrot" setpoint for cruise, take-off and
landing. Mission *decisions* (where to verify, confirm/dispute) come from 0G via the engine.

Physics (mirrors gym-pybullet-drones BaseAviary._physics, Physics.PYB):
  thrust_i = KF * rpm_i^2 applied with applyExternalForce on prop links 0..3 (LINK_FRAME),
  z-torque = KM * (-rpm0^2 + rpm1^2 - rpm2^2 + rpm3^2) on link 4 (center_of_mass_link).
The physical body is the real 27 g Crazyflie; a large visual-only "skin" (≈1.2 m, operator
colour, no collision, no mass) is teleported onto it so it is visible in cameras / the viewer.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pybullet as p

from common.config import settings
from sim.robot_base import RobotBase
from sim.schemas import FsmState
from sim.vendor.gym_pybullet_drones.control.BaseControl import ASSETS_DIR
from sim.vendor.gym_pybullet_drones.control.DSLPIDControl import DSLPIDControl
from sim.vendor.gym_pybullet_drones.utils.enums import DroneModel

_CTRL_HZ = 48.0            # DSLPID rate used in upstream examples (physics 240 Hz)
_CLIMB_MPS = 4.0
_DESCEND_FAST_MPS = 3.0
_DESCEND_SLOW_MPS = 0.8
_ACCEL_MPS2 = 2.0          # carrot speed ramps (DSLPID saturates on velocity steps)
_DECEL_MPS2 = 2.0
_MAX_CARROT_LEAD_M = 2.0   # carrot never runs further than this ahead of the body
_SAFE_HOVER_S = 5.0
_YAW_RATE = 0.8            # rad/s, yaw setpoint slew limit
_GROUND_Z = 0.15           # body z below this (and slow) counts as on the ground


class Drone(RobotBase):
    kind = "drone"

    def __init__(self, client: int, robot_id: str, operator: str, xy: tuple[float, float],
                 color: tuple[float, float, float, float] = (0.2, 0.6, 1.0, 1.0), *,
                 physics_hz: float = 240.0, cruise_alt: Optional[float] = None,
                 max_speed: Optional[float] = None, timeout_s: Optional[float] = None,
                 retry_s: float = 10.0) -> None:
        super().__init__(robot_id, operator, timeout_s=timeout_s, retry_s=retry_s)
        cfg = settings()["sim"]["drone"]
        self.client = client
        self.cruise_alt = float(cruise_alt if cruise_alt is not None else cfg["cruise_alt_m"])
        self.max_speed = float(max_speed if max_speed is not None else cfg["max_speed_mps"])
        self.home = np.array([float(xy[0]), float(xy[1])])
        self.physics_dt = 1.0 / physics_hz
        self._ctrl_every = max(1, int(round(physics_hz / _CTRL_HZ)))
        self._ctrl_dt = self._ctrl_every * self.physics_dt
        self._substep_n = 0

        self.body = p.loadURDF(str(ASSETS_DIR / "cf2x.urdf"), [xy[0], xy[1], 0.05],
                               p.getQuaternionFromEuler([0, 0, 0]),
                               flags=p.URDF_USE_INERTIA_FROM_FILE, physicsClientId=client)
        self.ctrl = DSLPIDControl(drone_model=DroneModel.CF2X)
        self.KF, self.KM = self.ctrl.KF, self.ctrl.KM
        self.visual_body: Optional[int] = self._make_skin(color)

        self._rpm = np.zeros(4)
        self._motors_on = False
        # setpoint ("carrot") state
        self._sp = np.array([xy[0], xy[1], 0.0])
        self._sp_vel = np.zeros(3)
        self._sp_speed = 0.0
        self._cruise_leg = False
        self._yaw_sp = 0.0
        # mission: mode in {"ground","goto","hold","land","safe_hover"}
        self._mode = "ground"
        self._goal: Optional[np.ndarray] = None
        self._after_arrive: Optional[str] = None      # "land_home" for return_home()
        self._after_land: Optional[FsmState] = None   # FSM state once on the ground
        self._safe_hover_until = 0.0
        self._saved: Optional[dict] = None            # mission saved while paused on the brain
        self._kin = self._read_kinematics()
        self._sync_skin()

    # ------------------------------------------------------------------ bodies
    def _make_skin(self, color) -> int:
        c = self.client
        rgba = list(color)
        arm = 0.6
        vis = p.createVisualShapeArray(
            shapeTypes=[p.GEOM_BOX, p.GEOM_BOX, p.GEOM_BOX] + [p.GEOM_CYLINDER] * 4,
            halfExtents=[[arm, 0.05, 0.03], [0.05, arm, 0.03], [0.15, 0.15, 0.08]] + [[0, 0, 0]] * 4,
            radii=[0, 0, 0] + [0.22] * 4,
            lengths=[0, 0, 0] + [0.02] * 4,
            rgbaColors=[[0.15, 0.15, 0.15, 1], [0.15, 0.15, 0.15, 1], rgba] + [rgba] * 4,
            visualFramePositions=[[0, 0, 0], [0, 0, 0], [0, 0, 0.02],
                                  [arm, 0, 0.05], [-arm, 0, 0.05], [0, arm, 0.05], [0, -arm, 0.05]],
            physicsClientId=c)
        return p.createMultiBody(baseMass=0, baseCollisionShapeIndex=-1, baseVisualShapeIndex=vis,
                                 basePosition=[0, 0, -100], physicsClientId=c)

    def _sync_skin(self) -> None:
        if self.visual_body is None:
            return
        pos, quat = self._kin[0], self._kin[1]
        p.resetBasePositionAndOrientation(self.visual_body, pos.tolist(), quat,
                                          physicsClientId=self.client)

    def _read_kinematics(self):
        pos, quat = p.getBasePositionAndOrientation(self.body, physicsClientId=self.client)
        vel, angv = p.getBaseVelocity(self.body, physicsClientId=self.client)
        return np.array(pos), quat, np.array(vel), np.array(angv)

    # ------------------------------------------------------------------ physics (240 Hz)
    def physics_substep(self, dt: float) -> None:
        """Called by BodyWorld before every p.stepSimulation."""
        self._kin = self._read_kinematics()
        pos, quat, vel, angv = self._kin
        self._advance_carrot(dt, pos)
        if not self._motors_on:
            return
        if self._substep_n % self._ctrl_every == 0:
            rpm, _, _ = self.ctrl.computeControl(
                control_timestep=self._ctrl_dt, cur_pos=pos, cur_quat=np.array(quat),
                cur_vel=vel, cur_ang_vel=angv, target_pos=self._sp,
                target_rpy=np.array([0.0, 0.0, self._yaw_sp]), target_vel=self._sp_vel)
            self._rpm = rpm
        self._substep_n += 1
        forces = self._rpm ** 2 * self.KF
        torques = self._rpm ** 2 * self.KM
        z_torque = -torques[0] + torques[1] - torques[2] + torques[3]
        for i in range(4):
            p.applyExternalForce(self.body, i, forceObj=[0, 0, forces[i]], posObj=[0, 0, 0],
                                 flags=p.LINK_FRAME, physicsClientId=self.client)
        p.applyExternalTorque(self.body, 4, torqueObj=[0, 0, z_torque], flags=p.LINK_FRAME,
                              physicsClientId=self.client)

    def _advance_carrot(self, dt: float, pos: np.ndarray) -> None:
        mode = self._mode
        if mode in ("goto", "hold", "safe_hover") and self._goal is not None:
            g = self._goal
            sp = self._sp
            horiz = float(np.linalg.norm(g[:2] - sp[:2]))
            if mode == "goto" and not self._cruise_leg and horiz > 2.0 and (
                    abs(sp[2] - g[2]) > 0.05 or abs(pos[2] - g[2]) > 1.0):
                # vertical leg first (climb to cruise alt), and let the body settle before the
                # horizontal leg: cf2x motors saturate at MIN_PWM on overshoot -> loss of attitude
                tgt = np.array([sp[0], sp[1], g[2]])
                vmax = _CLIMB_MPS
            else:
                tgt = g
                vmax = self.max_speed
                self._cruise_leg = True
            delta = tgt - sp
            dist = float(np.linalg.norm(delta))
            if dist < 0.02 and float(np.linalg.norm(self._sp_vel)) < 0.2:
                self._sp = tgt.copy()
                self._sp_vel = np.zeros(3)
                self._sp_speed = 0.0
                return
            u = delta / max(dist, 1e-9)
            # speed profile: cruise at vmax, brake to stop at the leg end (sqrt profile)
            speed = min(vmax, math.sqrt(2 * _DECEL_MPS2 * dist))
            if abs(u[2]) > 1e-6:   # cap vertical rate
                speed = min(speed, _CLIMB_MPS / abs(u[2]))
            if mode == "goto" and tgt is g and horiz > 1.0:
                # face the direction of travel (camera looks forward); yaw setpoint is rate
                # limited because a 180 deg yaw step saturates the motors and flips the cf2x
                want = math.atan2(u[1], u[0])
                err = (want - self._yaw_sp + math.pi) % (2 * math.pi) - math.pi
                self._yaw_sp += max(-_YAW_RATE * dt, min(_YAW_RATE * dt, err))
                self._yaw_sp = (self._yaw_sp + math.pi) % (2 * math.pi) - math.pi
                if abs(err) > math.radians(45):          # turn first, then accelerate
                    speed = 0.0
            # carrot velocity is a vector ramped at <= ACCEL (also when the goal changes
            # direction mid-flight): velocity steps saturate the cf2x motors and flip it
            dv = u * speed - self._sp_vel
            n = float(np.linalg.norm(dv))
            if n > _ACCEL_MPS2 * dt:
                dv *= _ACCEL_MPS2 * dt / n
            v = self._sp_vel + dv
            d = v * dt
            if float(np.linalg.norm(d)) >= dist:
                d = delta
            if np.linalg.norm(sp + d - pos) <= _MAX_CARROT_LEAD_M:
                self._sp = sp + d
                self._sp_vel = v
            else:                     # body lagging: wait for it, don't build up speed
                n = float(np.linalg.norm(self._sp_vel))
                self._sp_vel = self._sp_vel * (max(0.0, n - _ACCEL_MPS2 * dt) / n) if n > 1e-9 \
                    else np.zeros(3)
            self._sp_speed = float(np.linalg.norm(self._sp_vel))
        elif mode == "land":
            # descend fast high up, slow near the ground; ramped to avoid thrust saturation
            h = max(0.0, float(self._sp[2]) - 0.5)
            rate = min(_DESCEND_FAST_MPS, max(_DESCEND_SLOW_MPS, math.sqrt(2 * _DECEL_MPS2 * h)))
            rate = min(rate, self._sp_speed + _ACCEL_MPS2 * dt)
            self._sp_speed = rate
            self._sp = np.array([self._sp[0], self._sp[1], max(-0.3, self._sp[2] - rate * dt)])
            if self._sp[2] < pos[2] - _MAX_CARROT_LEAD_M:   # don't run away below the body
                self._sp[2] = pos[2] - _MAX_CARROT_LEAD_M
            self._sp_vel = np.array([0.0, 0.0, -rate if self._sp[2] > -0.3 else 0.0])
        else:  # ground
            self._sp_vel = np.zeros(3)
            self._sp_speed = 0.0

    # ------------------------------------------------------------------ control tick (20 Hz)
    def tick(self, now: float) -> None:
        self.now = now
        self._kin = self._read_kinematics()
        self._poll_checkpoint(now)
        self._mission_logic(now)
        self._sync_skin()

    def _mission_logic(self, now: float) -> None:
        pos, _, vel, _ = self._kin
        if self._mode == "safe_hover" and now >= self._safe_hover_until:
            self._start_land(after=None)
        if self._mode == "land":
            if pos[2] < _GROUND_Z and abs(vel[2]) < 0.3:
                self._motors_on = False
                self._rpm = np.zeros(4)
                self._mode = "ground"
                if self._after_land is not None and not self.paused:
                    self.set_state(self._after_land, note="landed")
                self._after_land = None
            return
        if self._mode == "goto" and self.at_target and not self.paused:
            if self._after_arrive == "land_home":
                self._after_arrive = None
                self._start_land(after=FsmState.IDLE)
                self.set_state(FsmState.LANDING, note="landing at home")
            else:
                self._hold_here(at=self._goal)

    # ------------------------------------------------------------------ mission API
    def fly_to(self, xy: tuple[float, float], alt: Optional[float] = None,
               state: Optional[FsmState] = FsmState.VERIFYING) -> None:
        goal = np.array([float(xy[0]), float(xy[1]), float(alt if alt is not None else self.cruise_alt)])
        mission = {"mode": "goto", "goal": goal, "after_arrive": None, "after_land": None}
        self._apply_or_save(mission)
        if state is not None:
            self.set_state(state)

    def return_home(self, state: FsmState = FsmState.RETURNING) -> None:
        goal = np.array([self.home[0], self.home[1], self.cruise_alt])
        mission = {"mode": "goto", "goal": goal, "after_arrive": "land_home", "after_land": None}
        self._apply_or_save(mission)
        self.set_state(state)

    def hover(self) -> None:
        self._apply_or_save({"mode": "hold", "goal": self._kin[0].copy(),
                             "after_arrive": None, "after_land": None})

    def land(self) -> None:
        self._apply_or_save({"mode": "land", "goal": None, "after_arrive": None,
                             "after_land": FsmState.IDLE})
        self.set_state(FsmState.LANDING)

    def _apply_or_save(self, mission: dict) -> None:
        if self.paused:
            self._saved = mission      # becomes active when the brain pause ends
        else:
            self._apply_mission(mission)

    def _apply_mission(self, m: dict) -> None:
        mode = m["mode"]
        if mode == "goto":
            self._takeoff_if_needed()
            self._goal = np.array(m["goal"], dtype=float)
            self._mode = "goto"
            self._cruise_leg = False
            self._after_arrive = m.get("after_arrive")
        elif mode == "hold":
            if self._motors_on:
                self._hold_here(at=m.get("goal"))
            else:
                self._mode = "ground"
        elif mode == "land":
            self._start_land(after=m.get("after_land"))
        elif mode == "ground":
            self._mode = "ground"

    def _current_mission(self) -> dict:
        return {"mode": self._mode, "goal": None if self._goal is None else self._goal.copy(),
                "after_arrive": self._after_arrive, "after_land": self._after_land}

    def _takeoff_if_needed(self) -> None:
        if not self._motors_on:
            pos = self._kin[0]
            self.ctrl.reset()
            self._motors_on = True
            self._sp = pos.copy()
            self._sp[2] = max(pos[2], 0.3)

    def _hold_here(self, at: Optional[np.ndarray] = None) -> None:
        """Hover at `at`, or brake smoothly to a stop from the current velocity (a setpoint step
        at cruise speed would saturate the cf2x motors)."""
        self._mode = "hold"
        if at is not None:
            self._goal = np.array(at, dtype=float)
            return
        pos, _, vel, _ = self._kin
        spd = float(np.linalg.norm(vel))
        stop = pos + vel * (spd / (2 * _DECEL_MPS2))
        stop[2] = max(stop[2], 1.0)
        self._goal = stop
        self._sp = pos.copy()
        self._sp_vel = np.array(vel, dtype=float)
        self._sp_speed = spd

    def _start_land(self, after: Optional[FsmState]) -> None:
        if not self._motors_on:
            self._mode = "ground"
            return
        pos = self._kin[0]
        self._mode = "land"
        self._sp_speed = 0.0
        self._after_land = after
        self._goal = None
        self._sp = np.array([pos[0], pos[1], pos[2]])

    # ------------------------------------------------------------------ RobotBase hooks
    def _on_pause_begin(self) -> None:
        if self._saved is None:
            self._saved = self._current_mission()
        if self._motors_on and self._mode != "land":
            self._hold_here()          # hover where it is while 0G decides

    def _on_pause_end(self) -> None:
        m, self._saved = self._saved, None
        if m is not None:
            self._apply_mission(m)     # resumes (takes off again if it landed in SAFE_STOP)

    def _on_safe_stop(self) -> None:
        if self._saved is None:
            self._saved = self._current_mission()
        if self._motors_on:
            self._hold_here()
            self._mode = "safe_hover"
            self._safe_hover_until = self.now + _SAFE_HOVER_S
        self.note = "SAFE_STOP: hover, then land here"

    # ------------------------------------------------------------------ queries
    @property
    def at_target(self) -> bool:
        if self._goal is None or not self._motors_on:
            return False
        pos, _, vel, _ = self._kin
        return (float(np.linalg.norm(pos[:2] - self._goal[:2])) < 1.5
                and abs(pos[2] - self._goal[2]) < 1.5 and float(np.linalg.norm(vel)) < 1.0)

    @property
    def landed(self) -> bool:
        return not self._motors_on

    @property
    def mode(self) -> str:
        return self._mode

    def pose(self) -> tuple[float, float, float, float, float]:
        pos, quat, vel, _ = self._kin
        yaw = p.getEulerFromQuaternion(quat)[2]
        return float(pos[0]), float(pos[1]), float(pos[2]), float(yaw), float(np.linalg.norm(vel))

    def camera_pose(self) -> tuple[list[float], list[float], list[float]]:
        """Eye slightly under the body, looking forward and ~60 deg below the horizon."""
        x, y, z, yaw, _ = self.pose()
        f = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        eye = np.array([x, y, z - 0.4]) + 0.3 * f
        pitch = math.radians(60.0)
        d = f * math.cos(pitch) + np.array([0.0, 0.0, -math.sin(pitch)])
        target = eye + 10.0 * d
        up = f * math.sin(pitch) + np.array([0.0, 0.0, math.cos(pitch)])
        return eye.tolist(), target.tolist(), up.tolist()
