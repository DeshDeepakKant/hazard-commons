"""Thin client for the 0G Compute Router (OpenAI-compatible), built on the `openai` SDK.

One `chat()` call == one HTTP request to the router. No hidden retries (max_retries=0), no
fallback provider, no trust-mode downgrade. Every failure raises `RouterError`; the caller
(brain/client.py) turns it into CheckpointResult(ok=False) -> SAFE_STOP.

Verified router facts (CLAUDE.md):
- request id = `X-Request-ID` response header == `x_0g_trace.request_id`
- we send `X-Client-Request-Id: {episode_id}:{seq}`
- TEE: header `X-0G-Provider-Trust-Mode: private` + body `verify_tee: true`
  -> `x_0g_trace.tee_verified`; HTTP 503 `no_provider_for_trust_mode` -> fail.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import openai
from dotenv import load_dotenv

from common.config import env, settings

# A port nothing listens on (discard). Used by the demo "0G unreachable" toggle so the call
# genuinely fails at the TCP level instead of being a simulated failure.
DEAD_BASE_URL = "http://127.0.0.1:9/v1"
DEFAULT_BASE_URL = "https://router-api.0g.ai/v1"

_env_loaded = False


def load_env_file() -> None:
    """Load an extra .env from $ENV_FILE (worktrees / spikes). Never overrides set vars."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    p = os.environ.get("ENV_FILE")
    if p and os.path.exists(p):
        load_dotenv(p, override=False)


def brain_settings() -> dict[str, Any]:
    return settings().get("brain", {})


def configured_base_url() -> str:
    load_env_file()
    return env(brain_settings().get("base_url_env", "ZG_ROUTER_BASE_URL"), DEFAULT_BASE_URL)


def configured_model() -> str:
    load_env_file()
    bs = brain_settings()
    return env(bs.get("model_env", "ZG_MODEL"), bs.get("default_model", "0gm-1.0-35b-a3b"))


def configured_api_key() -> Optional[str]:
    load_env_file()
    return env("ZG_ROUTER_API_KEY")


def api_key_looks_real(key: Optional[str]) -> bool:
    if not key:
        return False
    k = key.strip()
    if len(k) < 16 or "..." in k or k.lower() in ("sk-", "changeme", "placeholder"):
        return False
    return True


class RouterError(Exception):
    """A router call failed. `kind` is one of: timeout, unreachable, http, no_provider_for_trust_mode,
    bad_response."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None,
                 request_id: Optional[str] = None, latency_ms: Optional[float] = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status
        self.request_id = request_id
        self.latency_ms = latency_ms


@dataclass
class RouterResponse:
    content: Optional[str]
    request_id: Optional[str]           # X-Request-ID header (== x_0g_trace.request_id)
    trace: dict[str, Any]               # full x_0g_trace
    tee_verified: Optional[bool]
    provider: Optional[Any]
    billing: Optional[Any]
    model: Optional[str]
    completion_id: Optional[str]        # OpenAI-style response `id`
    client_request_id: Optional[str]
    latency_ms: float
    status: int
    headers: dict[str, str] = field(default_factory=dict)   # 0G-relevant response headers
    usage: Optional[dict[str, Any]] = None

    def full_trace(self) -> dict[str, Any]:
        """Everything we log for audit: router trace + ids + provider/billing + usage."""
        out = dict(self.trace)
        out.setdefault("request_id", self.request_id)
        out["_hc"] = {
            "x_request_id_header": self.headers.get("x-request-id") or self.request_id,
            "completion_id": self.completion_id,
            "client_request_id": self.client_request_id,
            "provider": self.provider,
            "billing": self.billing,
            "usage": self.usage,
            "http_status": self.status,
            "headers": self.headers,
        }
        return out


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


class ZGRouter:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout_s: Optional[float] = None,
                 trust_mode: Optional[str] = None, verify_tee: Optional[bool] = None,
                 thinking: Optional[bool] = None, max_tokens: Optional[int] = None):
        bs = brain_settings()
        self.base_url = base_url or configured_base_url()
        self.api_key = api_key or configured_api_key() or ""
        self.model = model or configured_model()
        self.timeout_s = float(timeout_s if timeout_s is not None else bs.get("timeout_s", 15))
        self.trust_mode = trust_mode or bs.get("trust_mode", "private")
        self.verify_tee = bool(bs.get("verify_tee", True) if verify_tee is None else verify_tee)
        # 0gm-1.0-35b-a3b has "thinking enabled by default" (GET /models). Checkpoints need a fast
        # JSON verdict, so thinking is off unless brain.thinking / ZG_THINKING=1.
        self.thinking = bool(bs.get("thinking", False) or (env("ZG_THINKING", "0") in ("1", "true"))
                             if thinking is None else thinking)
        self.max_tokens = int(max_tokens or bs.get("max_tokens", 1024))
        self._client = self._make_client(self.base_url)

    # ------------------------------------------------------------------ plumbing
    def _make_client(self, base_url: str) -> openai.OpenAI:
        return openai.OpenAI(
            base_url=base_url,
            api_key=self.api_key or "missing-key",
            timeout=httpx.Timeout(self.timeout_s, connect=min(self.timeout_s, 5.0)),
            max_retries=0,                      # retries are explicit and visible, never hidden
        )

    def set_base_url(self, base_url: str) -> None:
        self.base_url = base_url
        self._client = self._make_client(base_url)

    # ------------------------------------------------------------------ calls
    def chat(self, messages: list[dict[str, Any]], client_request_id: Optional[str] = None,
             max_tokens: Optional[int] = None, temperature: float = 0.0) -> RouterResponse:
        headers = {"X-0G-Provider-Trust-Mode": self.trust_mode}
        if client_request_id:
            headers["X-Client-Request-Id"] = client_request_id
        t0 = time.perf_counter()
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens or self.max_tokens,
                extra_headers=headers,
                extra_body={"verify_tee": self.verify_tee,
                            "chat_template_kwargs": {"enable_thinking": self.thinking}},
            )
        except openai.APITimeoutError as e:
            raise RouterError("timeout", f"no response within {self.timeout_s:.1f}s ({e})",
                              latency_ms=_ms(t0)) from e
        except openai.APIConnectionError as e:
            raise RouterError("unreachable", f"cannot reach {self.base_url}: {e}",
                              latency_ms=_ms(t0)) from e
        except openai.APIStatusError as e:
            body = _safe_text(e.response)
            rid = e.response.headers.get("x-request-id") if e.response is not None else None
            kind = "no_provider_for_trust_mode" if "no_provider_for_trust_mode" in body else "http"
            raise RouterError(kind, f"HTTP {e.status_code}: {body[:400]}", status=e.status_code,
                              request_id=rid, latency_ms=_ms(t0)) from e
        latency = _ms(t0)
        try:
            completion = raw.parse()
        except Exception as e:  # malformed body from the router
            raise RouterError("bad_response", f"unparseable router response: {e}",
                              status=raw.status_code, latency_ms=latency) from e

        extra = dict(completion.model_extra or {})
        trace = extra.get("x_0g_trace") or {}
        if not isinstance(trace, dict):
            trace = {"value": trace}
        hdr_rid = raw.headers.get("x-request-id")
        # The gateway can return a chained header ("<edge id>, <router id>"). The canonical id is
        # x_0g_trace.request_id; the full header stays in zg_trace._hc.x_request_id_header.
        request_id = trace.get("request_id") or (hdr_rid.split(",")[-1].strip() if hdr_rid else None)
        content = None
        if completion.choices:
            content = completion.choices[0].message.content
        usage = completion.usage.model_dump() if completion.usage is not None else None
        keep_headers = {k: v for k, v in raw.headers.items()
                        if k.lower().startswith(("x-request-id", "x-0g", "x-client-request-id"))}
        return RouterResponse(
            content=content,
            request_id=request_id,
            trace=trace,
            tee_verified=_pick(trace, "tee_verified"),
            provider=_pick(trace, "provider", "provider_address", "provider_id"),
            billing=_pick(trace, "billing", "cost", "fee"),
            model=completion.model or self.model,
            completion_id=completion.id,
            client_request_id=client_request_id,
            latency_ms=latency,
            status=raw.status_code,
            headers=keep_headers,
            usage=usage,
        )

    def list_models(self) -> Any:
        r = httpx.get(self.base_url.rstrip("/") + "/models",
                      headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 1)


def _safe_text(resp: Optional[httpx.Response]) -> str:
    if resp is None:
        return ""
    try:
        return resp.text
    except Exception:
        return ""
