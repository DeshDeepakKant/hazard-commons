"""ZGBrain: the real checkpoint brain (Brain Protocol, common/interfaces.py) on the 0G Router.

Never fabricates a decision. Every failure -> CheckpointResult(ok=False, error=...) -> SAFE_STOP:
timeout, connection error, HTTP 4xx/5xx, 503 no_provider_for_trust_mode, invalid JSON after
ONE corrective retry, call budget exceeded, any unexpected exception.
"""
from __future__ import annotations

import logging
import os
import threading
import time
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
                 on_result: Optional[Callable[[CheckpointRequest, CheckpointResult], None]] = None,
                 wallets: Any = "auto"):
        bs = brain_settings()
        self.router = router or ZGRouter()
        self._real_base_url = self.router.base_url
        # Optional per-checkpoint routes (brain profile). Explicitly chosen for a whole run and shown
        # on every card; never an automatic fallback. Checkpoints without a route use self.router.
        self.routes: dict[str, ZGRouter] = {}
        self.max_calls = int(max_calls if max_calls is not None else bs.get("max_calls_per_run", 100))
        self.max_retries_invalid = int(bs.get("max_retries_invalid_json", 1))
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zg-brain")
        self._lock = threading.Lock()
        self._calls = 0
        # Circuit breaker: N consecutive transport failures (timeout/5xx/unreachable) pause calls for a
        # cooldown. The robots SAFE_STOP meanwhile; nothing is guessed. Failed calls do NOT use budget.
        self.max_consecutive_failures = int(bs.get("max_consecutive_failures", 10))
        self.breaker_cooldown_s = float(bs.get("breaker_cooldown_s", 20))
        self._fail_streak: dict[str, int] = {}       # per model: a dead provider must not block another
        self._breaker_until: dict[str, float] = {}
        self._unreachable = False
        self._online = True
        self._on_result = on_result
        # Per-robot credit wallets (brain/wallets.py). None = disabled: behaviour identical to before.
        # "auto" builds a registry from settings brain.wallets when enabled (or env ZG_WALLETS=1).
        self.economy_routes: dict[str, ZGRouter] = {}
        if wallets == "auto":
            from brain.wallets import RobotWalletRegistry, wallet_settings, wallets_enabled
            wcfg = wallet_settings()
            wallets = RobotWalletRegistry(wcfg) if wallets_enabled(wcfg) else None
            if wallets is not None and wallets.economy_profile:
                try:
                    self.economy_routes, notes = build_profile_routes(wallets.economy_profile)
                    for n in notes:
                        log.warning("wallet economy route %s: %s", wallets.economy_profile, n)
                except ValueError as e:
                    log.warning("wallet economy profile unusable (%s): economy tier keeps default routes", e)
        self.wallets = wallets

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

    def set_routes(self, routes: dict[str, ZGRouter]) -> None:
        self.routes = dict(routes)

    def set_economy_routes(self, routes: dict[str, ZGRouter]) -> None:
        self.economy_routes = dict(routes)

    def router_for(self, checkpoint: str, tier: Optional[str] = None) -> ZGRouter:
        if tier in ("economy", "throttled") and checkpoint in self.economy_routes:
            return self.economy_routes[checkpoint]
        return self.routes.get(checkpoint, self.router)

    def set_unreachable(self, on: bool) -> None:
        """Demo toggle: point the client at a dead address so calls REALLY fail."""
        with self._lock:
            self._unreachable = on
            self._breaker_until.clear()
            self._fail_streak.clear()
            self.router.set_base_url(DEAD_BASE_URL if on else self._real_base_url)
            for r in [*self.routes.values(), *self.economy_routes.values()]:
                r.set_base_url(DEAD_BASE_URL if on else self._real_base_url)
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
        kw.setdefault("model", self.router_for(req.checkpoint).model)
        return CheckpointResult(ok=False, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                client_request_id=f"{req.episode_id}:{req.seq}", error=error, **kw)

    def _take_budget(self) -> bool:
        with self._lock:
            if self._calls >= self.max_calls:
                return False
            self._calls += 1
            return True

    def _refund_budget(self) -> None:
        with self._lock:
            self._calls = max(0, self._calls - 1)

    def _breaker_open(self, model: str) -> bool:
        with self._lock:
            return time.monotonic() < self._breaker_until.get(model, 0.0)

    def _note_transport(self, model: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._fail_streak[model] = 0
                return
            self._fail_streak[model] = self._fail_streak.get(model, 0) + 1
            if self._fail_streak[model] >= self.max_consecutive_failures:
                self._breaker_until[model] = time.monotonic() + self.breaker_cooldown_s
                self._fail_streak[model] = 0
                log.warning("0G router: %d consecutive failures on %s, pausing its calls for %.0fs",
                            self.max_consecutive_failures, model, self.breaker_cooldown_s)

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
        tier: Optional[str] = None
        if self.wallets is not None:
            gate = self.wallets.authorize(req.robot_id)
            if not gate.allowed:
                # No model call at all: the robot cannot afford one. Nothing is guessed.
                return self._fail(req, gate.reason)
            tier = gate.tier
        router = self.router_for(req.checkpoint, tier)
        model = router.model
        cost_total = 0
        cost_src = "router"
        messages = checkpoints.build_messages(req)
        attempts: list[dict[str, Any]] = []
        total_ms = 0.0
        last: Optional[RouterResponse] = None
        problem = ""
        for attempt in range(1 + self.max_retries_invalid):
            if self.wallets is not None and attempt > 0 and not self.wallets.authorize(
                    req.robot_id, rate_limit=False).allowed:
                return self._fail(req, "budget: robot credit exhausted (tier paused)",
                                  **self._fields(req, last, attempts, total_ms, cost_total, cost_src, router))
            if self._breaker_open(model):
                return self._fail(req, "0G unreachable: circuit breaker open after repeated failures "
                                       "(SAFE_STOP, nothing guessed)", **_result_fields(last, attempts, total_ms))
            if not self._take_budget():
                return self._fail(req, f"budget: call budget exceeded ({self.max_calls} calls per run)",
                                  **_result_fields(last, attempts, total_ms))
            # Each attempt gets its own client id so router logs stay unambiguous.
            attempt_crid = crid if attempt == 0 else f"{crid}:retry{attempt}"
            try:
                last = router.chat(messages, client_request_id=attempt_crid)
            except RouterError as e:
                self._refund_budget()          # a failed call is not a billed model call
                self._note_transport(model, False)
                total_ms += e.latency_ms or 0.0
                attempts.append({"client_request_id": attempt_crid, "request_id": e.request_id,
                                 "error": str(e)})
                return self._fail(req, f"0G {e}", latency_ms=round(total_ms, 1),
                                  zg_request_id=e.request_id,
                                  zg_trace={"attempts": attempts} if len(attempts) > 1 or e.request_id else None)
            self._note_transport(model, True)
            total_ms += last.latency_ms
            if self.wallets is not None:        # a 2xx call was billed (also if the output is invalid)
                cost, cost_src = _call_cost(last, self.wallets.cfg)
                cost_total += cost
                self.wallets.debit(req.robot_id, cost, ref=last.request_id, sim_time=req.sim_time)
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
            fields = self._fields(req, last, attempts, total_ms, cost_total, cost_src, router)
            return CheckpointResult(ok=True, checkpoint=req.checkpoint, robot_id=req.robot_id,
                                    output=output, client_request_id=crid, **fields)
        return self._fail(req, f"invalid output after {self.max_retries_invalid} retry: {problem}",
                          **self._fields(req, last, attempts, total_ms, cost_total, cost_src, router))

    def _fields(self, req: CheckpointRequest, last: Optional[RouterResponse], attempts: list[dict[str, Any]],
                total_ms: float, cost: int, cost_src: str, router: ZGRouter) -> dict[str, Any]:
        f = _result_fields(last, attempts, total_ms)
        if self.wallets is not None and f.get("zg_trace"):
            f["zg_trace"]["_hc"]["wallet"] = self.wallets.wallet_info(
                req.robot_id, cost, last.model if last else router.model, cost_source=cost_src,
                trust_mode=router.trust_mode, route="economy" if router is not self.routes.get(
                    req.checkpoint, self.router) else "default")
        return f


def _call_cost(last: RouterResponse, cfg: dict[str, Any]) -> tuple[int, str]:
    """Actual billed micro-USD from x_0g_trace billing; else estimate from token usage x config rate."""
    b = last.billing
    if isinstance(b, dict):
        for k in ("total_cost_usd_micro", "cost_usd_micro"):
            try:
                if b.get(k) is not None:
                    return max(0, int(round(float(b[k])))), "router"
            except (TypeError, ValueError):
                pass
    u = last.usage or {}
    est = (float(u.get("prompt_tokens") or 0) * float(cfg.get("estimate_in_micro_usd_per_1k", 20)) +
           float(u.get("completion_tokens") or 0) * float(cfg.get("estimate_out_micro_usd_per_1k", 60))) / 1000.0
    return int(round(est)), "estimate"


def _result_fields(last: Optional[RouterResponse], attempts: list[dict[str, Any]],
                   total_ms: float) -> dict[str, Any]:
    if last is None:
        return {"latency_ms": round(total_ms, 1) if total_ms else None}
    trace = last.full_trace()
    if len(attempts) > 1:
        trace["_hc"]["attempts"] = attempts
    return {"model": last.model, "zg_request_id": last.request_id, "zg_trace": trace,
            "tee_verified": last.tee_verified, "latency_ms": round(total_ms, 1)}


def build_profile_routes(profile: Optional[str]) -> tuple[dict[str, ZGRouter], list[str]]:
    """brain.profiles.<name> in settings.yaml -> {checkpoint: ZGRouter}. A route whose api_key_env
    is not set is skipped (that checkpoint stays on the default TEE model). Returns (routes, notes)."""
    from common.config import env, settings
    notes: list[str] = []
    if not profile or profile == "default":
        return {}, notes
    profiles = (settings().get("brain", {}) or {}).get("profiles", {}) or {}
    if profile not in profiles:
        raise ValueError(f"unknown brain profile {profile!r}; known: {sorted(profiles)}")
    routes: dict[str, ZGRouter] = {}
    for cp, spec in (profiles[profile] or {}).items():
        key_env = spec.get("api_key_env")
        key = env(key_env) if key_env else None
        if key_env and not api_key_looks_real(key):
            notes.append(f"{cp}: {spec['model']} skipped ({key_env} not set) -> stays on default model")
            continue
        routes[cp] = ZGRouter(model=spec["model"], api_key=key,
                              trust_mode=spec.get("trust_mode"), verify_tee=spec.get("verify_tee"))
        tee = "TEE" if (spec.get("trust_mode") or "private") == "private" else "NO TEE"
        notes.append(f"{cp}: {spec['model']} ({spec.get('trust_mode') or 'private'}, {tee})")
    return routes, notes


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
    profile = kw.pop("profile", None) or os.environ.get("ZG_BRAIN_PROFILE")
    brain = ZGBrain(**kw)
    routes, notes = build_profile_routes(profile)
    brain.set_routes(routes)
    for n in notes:
        log.warning("brain profile %s: %s", profile, n)
    return brain
