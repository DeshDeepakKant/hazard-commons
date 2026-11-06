"""Run the LOCAL stand-in for the 0G testnet Router billing API.

    .venv/bin/python scripts/run_testnet_router_mock.py --port 8790
    ... --start-micro-usd 500 --state-file logs/testnet_router_mock_state.json

Robots point their Router base URL at http://127.0.0.1:8790/v1 and send `X-HC-Account: R-04`.
Billing (balance, 402 insufficient_balance, deposit crediting from the on-chain credit vault) is
local and mocked; chat completions are forwarded to the REAL 0G Router (upstream =
--upstream / $HC_MOCK_UPSTREAM_BASE_URL / $ZG_ROUTER_BASE_URL / https://router-api.0g.ai/v1) using
ZG_ROUTER_API_KEY from .env. Deposit conversion (--micro-usd-per-0g, default 50_000_000, i.e.
0.00001 0G = 500 µUSD) is a declared demo rate: testnet 0G has no price.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from brain.zg_router import api_key_looks_real, configured_api_key, configured_base_url  # noqa: E402
from common.config import env  # noqa: E402
from testnet_router_mock.app import create_app  # noqa: E402
from testnet_router_mock.ledger import DEFAULT_MICRO_USD_PER_0G, DepositIndexer, Ledger  # noqa: E402

BANNER = """
==========================================================================================
  LOCAL STAND-IN for the 0G testnet Router BILLING API (balances / 402 / deposit credits)
  Inference is proxied to the REAL 0G Router: {upstream}
  Deposit rate {rate} µUSD per 0G is a declared demo conversion (testnet 0G has no price).
==========================================================================================
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--upstream", default=None, help="real 0G Router base URL (…/v1)")
    p.add_argument("--start-micro-usd", type=int, default=0, help="starting balance per new account")
    p.add_argument("--min-balance-micro-usd", type=int, default=1, help="402 below this balance")
    p.add_argument("--micro-usd-per-0g", type=int, default=DEFAULT_MICRO_USD_PER_0G)
    p.add_argument("--poll-s", type=float, default=4.0, help="deposit indexer poll interval")
    p.add_argument("--from-block", type=int, default=None,
                   help="first block to index (default: latest - 2000, or the state-file cursor)")
    p.add_argument("--state-file", default=None, help="optional JSON persistence")
    p.add_argument("--timeout-s", type=float, default=120.0, help="upstream timeout")
    p.add_argument("--no-indexer", action="store_true", help="don't poll the credit vault")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    upstream = (a.upstream or env("HC_MOCK_UPSTREAM_BASE_URL") or configured_base_url()).rstrip("/")
    u = urlparse(upstream)
    if u.hostname in ("127.0.0.1", "localhost", "0.0.0.0") and (u.port or 80) == a.port:
        print(f"refusing: upstream {upstream} is this stand-in itself; pass --upstream "
              "https://router-api.0g.ai/v1 or set HC_MOCK_UPSTREAM_BASE_URL", file=sys.stderr)
        return 2
    key = configured_api_key()
    if not api_key_looks_real(key):
        print("warning: ZG_ROUTER_API_KEY missing/placeholder; upstream calls will fail (401)",
              file=sys.stderr)

    ledger = Ledger(start_micro_usd=a.start_micro_usd, micro_usd_per_0g=a.micro_usd_per_0g,
                    state_file=a.state_file)
    if a.from_block is not None:
        ledger.cursor = a.from_block
    indexer = None if a.no_indexer else DepositIndexer(ledger, poll_s=a.poll_s, from_block=a.from_block)
    app = create_app(ledger, indexer, upstream_base_url=upstream, api_key=key,
                     min_balance_micro_usd=a.min_balance_micro_usd, timeout_s=a.timeout_s,
                     start_indexer=indexer is not None)
    print(BANNER.format(upstream=upstream, rate=a.micro_usd_per_0g), file=sys.stderr)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
