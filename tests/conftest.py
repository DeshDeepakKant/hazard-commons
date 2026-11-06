"""Hermetic tests: Engine() must never talk to a real chain (chain/deployments/galileo.json exists
in the repo). HC_CHAIN=local forces the in-memory LocalHazardChain in Engine._init_chain.
Tests that need Anvil/Galileo construct chain.bindings.HazardChain explicitly."""
import os

os.environ["HC_CHAIN"] = "local"
os.environ.pop("DEMO", None)
os.environ["HC_STORAGE"] = "off"   # never upload to real 0G Storage from tests
