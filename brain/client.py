"""ZGBrain: the real checkpoint brain (Brain Protocol, common/interfaces.py) on the 0G Router.

Never fabricates a decision. Every failure -> CheckpointResult(ok=False, error=...) -> SAFE_STOP:
timeout, connection error, HTTP 4xx/5xx, 503 no_provider_for_trust_mode, invalid JSON after
ONE corrective retry, call budget exceeded, any unexpected exception.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from brain import checkpoints
from brain.zg_router import (DEAD_BASE_URL, RouterError, RouterResponse, ZGRouter,
                             api_key_looks_real, brain_settings, configured_api_key)
from common.config import mock_brain_enabled
from sim.schemas import CheckpointRequest, CheckpointResult

log = logging.getLogger("hazard.brain")


class ZGBrain:
    def __init__(self, router: Optional[ZGRouter] = None, max_workers: int = 4,
                 max_calls: Optional[int] = None,
                 on_result: Optional[Callable[[CheckpointRequest, CheckpointResult], None]] = None):
        bs = brain_settings()
        self.router = router or ZGRouter()
        self._real_base_url = self.router.base_url
        self.max_calls = int(max_calls if max_calls is not None else bs.get("max_calls_per_run", 100))
        self.max_retries_invalid = int(bs.get("max_retries_invalid_json", 1))
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zg-brain")
        self._lock = threading.Lock()
        self._calls = 0
        self._unreachable = False
        self._online = True
        self._on_result = on_result

    # ------------------------------------------------------------------ Brain Protocol
    def submit(self, req: CheckpointRequest) -> "Future[CheckpointResult]":
        try:
            return self._pool.submit(self._safe_decide, req)
        except Exception as e:  # pool shut down etc.: still never raise
            f: Future[CheckpointResult] = Future()
            f.set_result(self._fail(req, f"submit failed: {e}"))
            return f

    def online(self) -> bool:
        return self._online and not self._unreachable

    def set_unreachable(self, on: bool) -> None:
        """Demo toggle: point the client at a dead address so calls REALLY fail."""
        with self._lock:
            self._unreachable = on
            self.router.set_base_url(DEAD_BASE_URL if on else self._real_base_url)
            if on:
                self._online = False
        log.warning("0G router %s", "UNREACHABLE (dead endpoint)" if on else "restored")

    # ------------------------------------------------------------------ extras
    @property
    def calls_used(self) -> int:
        return self._calls

    def calls_left(self) -> int:
        return max(0, self.max_calls - self._calls)

    def reset_budget(self) -> None:
        with self._lock:
            self._calls = 0

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ internals
    def _fail(self, req: CheckpointRequest, error: str, **kw: Any) -> CheckpointResult:
        return CheckpointResult(ok=False, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                client_request_id=f"{req.episode_id}:{req.seq}", error=error,
                                model=self.router.model, **kw)

    def _take_budget(self) -> bool:
        with self._lock:
            if self._calls >= self.max_calls:
                return False
            self._calls += 1
            return True

    def _safe_decide(self, req: CheckpointRequest) -> CheckpointResult:
        try:
            res = self._decide(req)
        except Exception as e:  # never let an exception escape the Future
            log.exception("brain internal error")
            res = self._fail(req, f"internal error: {type(e).__name__}: {e}")
        self._online = res.ok or (res.error or "").startswith(("invalid output", "budget"))
        if self._on_result:
            try:
                self._on_result(req, res)
            except Exception:
                log.exception("on_result callback failed")
        return res

    def _decide(self, req: CheckpointRequest) -> CheckpointResult:
        if req.checkpoint not in checkpoints.PROMPT_FILES:
            return self._fail(req, f"unknown checkpoint {req.checkpoint}")
        crid = f"{req.episode_id}:{req.seq}"
        messages = checkpoints.build_messages(req)
        attempts: list[dict[str, Any]] = []
        total_ms = 0.0
        last: Optional[RouterResponse] = None
        problem = ""
        for attempt in range(1 + self.max_retries_invalid):
            if not self._take_budget():
                return self._fail(req, f"budget: call budget exceeded ({self.max_calls} calls per run)",
                                  **_result_fields(last, attempts, total_ms))
            # Each attempt gets its own client id so router logs stay unambiguous.
            attempt_crid = crid if attempt == 0 else f"{crid}:retry{attempt}"
            try:
                last = self.router.chat(messages, client_request_id=attempt_crid)
            except RouterError as e:
                total_ms += e.latency_ms or 0.0
                attempts.append({"client_request_id": attempt_crid, "request_id": e.request_id,
                                 "error": str(e)})
                return self._fail(req, f"0G {e}", latency_ms=round(total_ms, 1),
                                  zg_request_id=e.request_id,
                                  zg_trace={"attempts": attempts} if len(attempts) > 1 or e.request_id else None)
            total_ms += last.latency_ms
            try:
                output = checkpoints.validate_output(req.checkpoint, last.content)
            except ValueError as e:
                problem = str(e)
                attempts.append({"client_request_id": attempt_crid, "request_id": last.request_id,
                                 "error": f"invalid output: {problem}",
                                 "content": (last.content or "")[:500]})
                messages = messages + [{"role": "assistant", "content": last.content or ""},
                                       checkpoints.corrective_message(req.checkpoint, problem)]
                continue
            attempts.append({"client_request_id": attempt_crid, "request_id": last.request_id})
            fields = _result_fields(last, attempts, total_ms)
            return CheckpointResult(ok=True, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                    output=output, client_request_id=crid, **fields)
        return self._fail(req, f"invalid output after {self.max_retries_invalid} retry: {problem}",
                          **_result_fields(last, attempts, total_ms))


def _result_fields(last: Optional[RouterResponse], attempts: list[dict[str, Any]],
                   total_ms: float) -> dict[str, Any]:
    if last is None:
        return {"latency_ms": round(total_ms, 1) if total_ms else None}
    trace = last.full_trace()
    if len(attempts) > 1:
        trace["_hc"]["attempts"] = attempts
    return {"model": last.model, "zg_request_id": last.request_id, "zg_trace": trace,
            "tee_verified": last.tee_verified, "latency_ms": round(total_ms, 1)}


def get_brain(**kw: Any):
    """Factory. MockBrain ONLY when MOCK_BRAIN=1 (and DEMO!=1, else mock_brain_enabled raises).
    Otherwise the real 0G brain; refuses to start without a real router API key."""
    if mock_brain_enabled():
        from brain.mock import MockBrain
        return MockBrain()
    key = kw.get("router").api_key if kw.get("router") is not None else configured_api_key()
    if not api_key_looks_real(key):
        raise RuntimeError(
            "ZG_ROUTER_API_KEY is missing or a placeholder. Put a real 0G Router key in .env "
            "(or set ENV_FILE to the .env path). There is no fallback brain: without 0G the "
            "robots can only SAFE_STOP. For local physics dev only: MOCK_BRAIN=1 (refused with DEMO=1).")
    return ZGBrain(**kw)
