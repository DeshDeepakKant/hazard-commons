#!/usr/bin/env python3
"""Proof of Safe Stop (SPEC §2, §8, §10.4 & §12).

Tests that when 0G Compute is unreachable (router base URL pointed at a dead host),
robots enter SAFE_STOP, pull to the kerb / hover & land, and ZERO decisions are
fabricated or guessed.

Run:
    .venv/bin/python scripts/test_safestop.py
Exit 0 on success; non-zero on any violation of the never-fake rule.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.client import ZGBrain
from brain.zg_router import DEAD_BASE_URL, ZGRouter
from server.bus import BUS
from sim.engine import Engine
from sim.robot import Rover
from sim.schemas import FsmState, HazardProp


def main() -> int:
    print("=" * 68)
    print("  HAZARD COMMONS: SAFE-STOP VERIFICATION TEST")
    print(f"  Pointing 0G Router at dead endpoint: {DEAD_BASE_URL}")
    print("=" * 68)

    # 1. Instantiate real ZGBrain pointed at the dead address
    router = ZGRouter(base_url=DEAD_BASE_URL, api_key="sk-real-test-safestop-key-123456789")
    brain = ZGBrain(router=router, max_workers=2)

    # 2. Track all decision events emitted on the bus
    decisions = []
    BUS.subscribe_sync(decisions.append, {"decision"})

    # 3. Initialize engine with the unreachable brain
    engine = Engine(
        brain=brain,
        enable_traffic=False,
        seed=42,
        realtime_factor=0.0,
        attach_to_server=False,
    )

    try:
        # Find a rover (MinuteMart)
        rover = next(r for r in engine.world.robots.values()
                     if isinstance(r, Rover) and r.operator == "MinuteMart")
        rx, ry, _, ryaw, _ = rover.pose()

        # Place a physical hazard prop directly in front of the rover
        px = rx + 8.0 * math.cos(ryaw)
        py = ry + 8.0 * math.sin(ryaw)
        edge_id = list(engine.world.planner.edges.keys())[0]

        prop = HazardProp(
            id="P-safestop-test",
            edge_id=edge_id,
            type="accident",
            x=px,
            y=py,
            real=True,
        )
        engine.add_prop(prop)

        # Rover is delivering past the hazard
        rover.set_state(FsmState.DELIVERING)
        rover.pp.set_path([[rx, ry], [px, py], [px + 20.0, py + 20.0]])
        rover.route_edge_ids = [edge_id]

        print(f"\n[1] Rover {rover.id} ({rover.operator}) dispatched towards hazard on {edge_id}")
        print(f"    Initial state: {rover.fsm_state.value}")

        # Step the engine until checkpoint triggers and resolves
        print("\n[2] Stepping simulation...")
        for tick in range(60):
            engine.step(0.05)
            if rover.in_safe_stop:
                print(f"    t={engine.sim_time:.2f}s: Rover entered SAFE_STOP!")
                break

        # 4. Strict verification checks
        print("\n[3] Verifying safe stop requirements:")
        print(f"    Rover FSM state: {rover.fsm_state.value}")
        print(f"    Rover note: {rover.note}")
        print(f"    Decisions recorded: {len(decisions)}")

        assert rover.in_safe_stop, f"FAIL: Rover is NOT in SAFE_STOP (state: {rover.fsm_state.value})"
        assert rover.fsm_state == FsmState.SAFE_STOP, f"FAIL: Rover FSM state is {rover.fsm_state.value}"

        # ZERO decisions may be accepted as ok=True
        ok_decisions = [d for d in decisions if d.data["result"].get("ok") is True]
        assert len(ok_decisions) == 0, f"FAIL: {len(ok_decisions)} decisions recorded ok=True! Never fake a 0G decision."

        # Failed decision should record the connection error
        failed_decisions = [d for d in decisions if d.data["result"].get("ok") is False]
        assert len(failed_decisions) >= 1, "FAIL: Expected at least 1 failed checkpoint attempt"
        err = failed_decisions[0].data["result"].get("error", "")
        print(f"    Checkpoint failure error: {err}")
        assert "unreachable" in err or "Connection" in err or "refused" in err, f"Unexpected error: {err}"

        # Check that no heuristic or fake report was posted on chain
        hazards = engine.chain.hazards()
        assert len(hazards) == 0, f"FAIL: Hazards were reported to chain without a valid 0G decision: {hazards}"

        print("\n" + "=" * 68)
        print("  PASS: All safe-stop requirements satisfied.")
        print("  - Robot held position and pulled to kerb upon 0G failure.")
        print("  - ZERO decisions fabricated or guessed.")
        print("  - On-chain truth unpolluted.")
        print("=" * 68)
        return 0

    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
