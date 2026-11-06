"""DEV-ONLY mock brain for physics testing. NEVER used in the demo path.

- Only constructible when MOCK_BRAIN=1 AND DEMO!=1 (common.config.mock_brain_enabled()).
- Prints a loud banner on construction and stamps every result with mock=True.
- The viewer renders mock results with a red "MOCK - NOT 0G" badge.
"""
from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor

from common.config import mock_brain_enabled
from sim.schemas import CheckpointRequest, CheckpointResult

BANNER = """
#####################################################################
#   MOCK_BRAIN=1  --  DEV ONLY  --  DECISIONS ARE NOT FROM 0G        #
#   Not valid for the demo, the video, or the submission.           #
#####################################################################
"""


class MockBrain:
    def __init__(self) -> None:
        if not mock_brain_enabled():
            raise RuntimeError("MockBrain requires MOCK_BRAIN=1 and DEMO!=1")
        print(BANNER, file=sys.stderr, flush=True)
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._unreachable = False

    def online(self) -> bool:
        return not self._unreachable

    def set_unreachable(self, on: bool) -> None:
        self._unreachable = on

    def submit(self, req: CheckpointRequest) -> "Future[CheckpointResult]":
        return self._pool.submit(self._decide, req)

    def _decide(self, req: CheckpointRequest) -> CheckpointResult:
        import time
        time.sleep(1.0)
        if self._unreachable:
            return CheckpointResult(ok=False, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                    error="mock: unreachable", mock=True)
        truth = req.context.get("_mock_truth", {})
        out: dict
        if req.checkpoint == "CP-H1":
            t = truth.get("type", "none")
            out = {"is_hazard": t != "none", "type": t, "severity": 0.8 if t != "none" else 0.0,
                   "passable_for": ["drone"] if t != "none" else ["rover", "drone"],
                   "confidence": 0.9, "description": "MOCK"}
        elif req.checkpoint == "CP-H2":
            out = {"action": "reroute", "reason": "MOCK", "trust_report": True}
        elif req.checkpoint == "CP-V":
            real = truth.get("real", True)
            out = {"verdict": "confirm" if real else "dispute",
                   "observed_type": truth.get("type", "none") if real else "none",
                   "confidence": 0.9, "reason": "MOCK"}
        elif req.checkpoint == "CP-Q":
            out = {"what_happened": "MOCK", "cause": "", "traffic_blocked": True, "eta_minutes": None,
                   "confidence": 0.5, "agrees_with_photo": True, "summary_hi": "MOCK", "summary_en": "MOCK"}
        else:
            out = {"at_correct_location": True, "confidence": 0.9}
        return CheckpointResult(ok=True, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                output=out, model="MOCK", zg_request_id=f"MOCK-{req.seq}",
                                tee_verified=None, latency_ms=1000, mock=True)
