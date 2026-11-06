"""FastAPI app: LOCAL stand-in for the 0G *testnet* Router billing API.

Why: the real testnet Router (router-api-testnet.integratenetwork.work) has been unreachable
(TLS handshake drops). On real testnet a robot deposits testnet 0G to the Payment Layer, a worker
credits its Router account, and inference fails with 402 `insufficient_balance` when empty.
This service reproduces ONLY that billing loop, per robot account (header `X-HC-Account`).

Inference stays REAL: `POST /v1/chat/completions` is forwarded byte-for-byte to the real 0G Router
(`$ZG_ROUTER_BASE_URL`, default https://router-api.0g.ai/v1) with our own API key, and the upstream
status, body and `X-Request-ID` / `x-0g-e2ee` headers are returned unchanged. This service never
generates, caches or alters a model answer. Every response carries
`X-HC-Mock: testnet-router-billing-stand-in`.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from testnet_router_mock.ledger import DepositIndexer, Ledger

log = logging.getLogger("testnet_router_mock")

MOCK_HEADER = ("X-HC-Mock", "testnet-router-billing-stand-in")
HOP_HEADER = "X-HC-Mock-Hop"          # loop guard: set on outbound calls, refused inbound
ACCOUNT_HEADER = "X-HC-Account"
NOTE = ("local stand-in for the 0G testnet Router billing API; inference is proxied to the real "
        "0G Router")
FORWARD_HEADERS = ("x-0g-provider-trust-mode", "x-client-request-id", "content-type")
RETURN_HEADERS = ("x-request-id", "x-0g-e2ee")

# Fallback cost estimate when x_0g_trace.billing.total_cost_usd_micro is missing.
EST_MICRO_USD_PER_1K_PROMPT = 20
EST_MICRO_USD_PER_1K_COMPLETION = 60


def _err(status: int, type_: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": type_, "message": message}}, status_code=status)


def cost_micro_usd(resp_json: Any) -> int:
    """Cost of one completion in µUSD: the Router's own billing figure, else a token estimate."""
    if not isinstance(resp_json, dict):
        return 0
    trace = resp_json.get("x_0g_trace") or {}
    billing = trace.get("billing") if isinstance(trace, dict) else None
    if isinstance(billing, dict) and billing.get("total_cost_usd_micro") not in (None, ""):
        try:
            return max(0, int(float(billing["total_cost_usd_micro"])))
        except (TypeError, ValueError):
            pass
    usage = resp_json.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    return -(-(pt * EST_MICRO_USD_PER_1K_PROMPT + ct * EST_MICRO_USD_PER_1K_COMPLETION) // 1000)


def create_app(ledger: Ledger, indexer: Optional[DepositIndexer], *, upstream_base_url: str,
               api_key: Optional[str], min_balance_micro_usd: int = 1, timeout_s: float = 120.0,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               start_indexer: bool = False) -> FastAPI:
    upstream = upstream_base_url.rstrip("/")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_indexer and indexer is not None:
            indexer.start()
        try:
            yield
        finally:
            if start_indexer and indexer is not None:
                indexer.stop()

    app = FastAPI(title="0G testnet Router billing stand-in (LOCAL MOCK)", lifespan=lifespan)

    @app.middleware("http")
    async def mark_mock(request: Request, call_next):
        if request.headers.get(HOP_HEADER):
            resp: Response = _err(508, "loop_detected",
                                  "stand-in received its own proxied request; upstream points at itself")
        else:
            resp = await call_next(request)
        resp.headers[MOCK_HEADER[0]] = MOCK_HEADER[1]
        return resp

    def account_of(request: Request) -> Optional[str]:
        a = (request.headers.get(ACCOUNT_HEADER) or "").strip()
        return a[:64] or None

    def missing_account() -> JSONResponse:
        return _err(400, "missing_account",
                    f"header {ACCOUNT_HEADER} (robot body id, e.g. R-04) is required")

    def balance_body(account: str) -> dict[str, Any]:
        return {**ledger.snapshot(account), "mock": True, "note": NOTE}

    def upstream_headers(request: Request) -> dict[str, str]:
        h = {k: request.headers[k] for k in FORWARD_HEADERS if k in request.headers}
        h["Authorization"] = f"Bearer {api_key or ''}"
        h[HOP_HEADER] = "1"
        return h

    def relay(up: httpx.Response) -> Response:
        out = Response(content=up.content, status_code=up.status_code,
                       media_type=up.headers.get("content-type"))
        for k in RETURN_HEADERS:           # keep repeated headers (router sends 2 X-Request-ID)
            for v in up.headers.get_list(k):
                out.headers.append(k, v)
        return out

    async def call_upstream(method: str, path: str, headers: dict[str, str],
                            content: Optional[bytes] = None):
        async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
            return await client.request(method, upstream + path, headers=headers, content=content)

    @app.get("/v1/account/balance")
    def get_balance(request: Request):
        acct = account_of(request)
        if not acct:
            return missing_account()
        return balance_body(acct)

    @app.post("/v1/account/sync")
    def sync(request: Request):
        acct = account_of(request)
        if not acct:
            return missing_account()
        credited = indexer.poll_once() if indexer is not None else 0
        body = balance_body(acct)
        body["sync"] = {"credited_deposits": credited,
                        **(indexer.status() if indexer is not None else {"last_error": "no indexer"})}
        return body

    @app.get("/v1/models")
    async def models(request: Request):
        try:
            up = await call_upstream("GET", "/models", upstream_headers(request))
        except httpx.HTTPError as e:
            return _err(502, "upstream_unreachable", f"{type(e).__name__}: {e}")
        return relay(up)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        acct = account_of(request)
        if not acct:
            return missing_account()
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _err(400, "invalid_request", "body must be JSON")
        if isinstance(body, dict) and body.get("stream"):
            return _err(400, "invalid_request", "stream=true is not supported by this stand-in")
        bal = ledger.balance(acct)
        if bal < min_balance_micro_usd:
            return _err(402, "insufficient_balance",
                        f"account {acct} balance {bal} µUSD < minimum {min_balance_micro_usd} µUSD; "
                        "deposit testnet 0G to the credit vault and POST /v1/account/sync "
                        "(local stand-in for the 0G testnet Router billing)")
        try:
            up = await call_upstream("POST", "/chat/completions", upstream_headers(request), raw)
        except httpx.HTTPError as e:
            log.warning("upstream unreachable for %s: %s", acct, type(e).__name__)
            return _err(502, "upstream_unreachable", f"{type(e).__name__}: {e}")
        if 200 <= up.status_code < 300:
            try:
                cost = cost_micro_usd(up.json())
            except ValueError:
                cost = 0
            new_bal = ledger.debit(acct, cost)
            log.info("%s: debited %d µUSD (req %s) -> balance %d", acct, cost,
                     up.headers.get("x-request-id"), new_bal)
        else:
            log.info("%s: upstream %d, nothing debited", acct, up.status_code)
        return relay(up)

    app.state.ledger = ledger
    app.state.indexer = indexer
    return app
