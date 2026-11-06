"""LOCAL stand-in for the 0G testnet Router *billing* API (deposit -> balance -> 402).

Inference is proxied to the real 0G Router; this package never produces a model answer.
Run: .venv/bin/python scripts/run_testnet_router_mock.py --port 8790
"""
from testnet_router_mock.app import create_app
from testnet_router_mock.ledger import DepositIndexer, Ledger

__all__ = ["create_app", "DepositIndexer", "Ledger"]
