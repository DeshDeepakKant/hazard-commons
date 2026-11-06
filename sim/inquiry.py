"""Voice inquiry: after CP-H1 says is_hazard, the rover asks bystanders (speaker + mic in the viewer)
and the answers go to 0G as a CP-Q checkpoint. Nothing here decides anything: with no answers in
time the rover just carries on with its CP-H1 result; with a failed 0G call the normal SAFE_STOP/
not-ok result is shown and no summary is invented."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

QUESTIONS = [
    {"hi": "आगे क्या हुआ?", "en": "What happened ahead?"},
    {"hi": "ट्रैफिक क्यों नहीं चल रहा?", "en": "Why is traffic stopped?"},
    {"hi": "कौन सा हादसा हुआ?", "en": "What kind of accident?"},
    {"hi": "कितना समय लगेगा?", "en": "How long will it take?"},
]
EXPIRE_WALL_S = 25.0
MAX_ANSWER_CHARS = 400


class InquiryManager:
    """Thread-safe: REST handlers call answer(); the sim thread calls open() and tick()."""

    def __init__(self, publish: Callable[[dict[str, Any]], None],
                 start_cp: Callable[[dict[str, Any], list[dict[str, Any]], str], bool],
                 expire_s: float = EXPIRE_WALL_S) -> None:
        self._publish = publish
        self._start_cp = start_cp
        self.expire_s = expire_s
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._ready: list[tuple[dict[str, Any], list[dict[str, Any]], str, float]] = []
        self._n = 0

    def open(self, robot_id: str, hazard: dict[str, Any], *, frame_png_b64: Optional[str] = None,
             private: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._lock:
            self._n += 1
            iid = f"Q{self._n}"
            inq = {"id": iid, "robot_id": robot_id, "hazard": hazard, "questions": QUESTIONS,
                   "expires_in_s": self.expire_s, "status": "asking"}
            self._pending[iid] = {**inq, "_deadline": time.monotonic() + self.expire_s,
                                  "_frame": frame_png_b64, "_private": private or {}}
        self._publish(inq)
        return inq

    def pending(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [{**{k: v for k, v in p.items() if not k.startswith("_")},
                     "expires_in_s": round(max(0.0, p["_deadline"] - now), 1)}
                    for p in self._pending.values()]

    def answer(self, iid: str, answers: list[dict[str, Any]], source: str) -> dict[str, Any]:
        with self._lock:
            p = self._pending.pop(iid, None)
            if p is None:
                raise KeyError(iid)
            clean = []
            for a in answers[:len(QUESTIONS)]:
                lang = "en" if a.get("lang") == "en" else "hi"
                clean.append({"q": str(a.get("q", ""))[:200], "a": str(a.get("a", "")).strip()[:MAX_ANSWER_CHARS],
                              "lang": lang, "source": source})
            if not any(c["a"] for c in clean):
                status = "no_answer"
            else:
                status = "answered"
                self._ready.append((p, clean, source, time.monotonic()))
        self._publish({"id": iid, "robot_id": p["robot_id"], "status": status, "source": source})
        return {"ok": True, "status": status}

    def tick(self) -> None:
        """Sim thread, every step: expire unanswered inquiries; start CP-Q for answered ones."""
        now = time.monotonic()
        expired: list[dict[str, Any]] = []
        with self._lock:
            for iid in [i for i, p in self._pending.items() if now > p["_deadline"]]:
                expired.append(self._pending.pop(iid))
            ready, self._ready = self._ready, []
        for p in expired:
            self._publish({"id": p["id"], "robot_id": p["robot_id"], "status": "expired"})
        keep = []
        for item in ready:
            p, clean, source, t0 = item
            if self._start_cp(p, clean, source):
                continue
            if now - t0 < 60.0:      # robot busy with another checkpoint: try again next tick
                keep.append(item)
        if keep:
            with self._lock:
                self._ready = keep + self._ready


# ---------------------------------------------------------------- engine glue
def start_cp_q(engine: Any, p: dict[str, Any], answers: list[dict[str, Any]], source: str) -> bool:
    """Submit the CP-Q checkpoint for an answered inquiry through the normal brain path (Future,
    polled by the robot each tick). False if the robot is busy with another checkpoint."""
    from memory.episode_log import record_from_result
    from server.bus import BUS
    from sim.schemas import CheckpointRequest, DecisionEvent

    rover = engine.world.robots.get(p["robot_id"])
    if rover is None:
        return True
    if rover.cp is not None and rover.cp.pending is not None:
        return False
    priv = p.get("_private") or {}
    seq = engine._next_seq(rover.id)
    req = CheckpointRequest(
        robot_id=rover.id, operator=rover.operator, robot_kind=rover.kind, checkpoint="CP-Q",
        sim_time=engine.sim_time, episode_id=engine.episode_id_for(rover.id), seq=seq,
        frame_png_b64=p.get("_frame"),
        context={
            "bystander_testimony": answers,
            "hazard": p["hazard"],
            "inquiry_id": p["id"],
            "_source": source,
        },
    )

    def on_ok(robot: Any, res: Any) -> None:
        robot.note = "0G: bystander inquiry summarised"

    def on_result(robot: Any, res: Any) -> None:
        action = f"inquiry_summarised ({source})" if res.ok else "safe_stop"
        engine.episode_log.append(record_from_result(req, res, action_taken=action),
                                  frame_b64=p.get("_frame"))
        BUS.publish("decision", engine.sim_time, DecisionEvent(
            result=res, operator=robot.operator, sim_time=engine.sim_time, action_taken=action,
            frame_thumb_b64=priv.get("thumb"), frame_full_b64=priv.get("full")))
        BUS.publish("log", engine.sim_time, {"level": "info", "msg": (
            f"inquiry {p['id']} ({source}): " + ", ".join(
                f"{a['q'][:24]}={a['a'][:40]!r}" for a in answers if a["a"]))})

    return rover.begin_checkpoint(engine.brain, req, on_ok=on_ok, on_result=on_result,
                                  now=engine.sim_time)
