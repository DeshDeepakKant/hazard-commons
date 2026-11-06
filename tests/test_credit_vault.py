"""Offline tests for chain.credit_vault (no network: synthetic logs + a temp deployment json)."""
import json

import pytest
from eth_abi import encode
from web3 import Web3

from chain.credit_vault import (DEPOSITED_TOPIC, VAULT_ABI_PATH, CreditVault, decode_deposited,
                                error_selectors, load_vault_abi, robot_key)

VAULT = "0x00000000000000000000000000000000000000Aa"
SENDER = "0x359f563EE42A093AF6465dd8c2D24D4f88e6ebdb"


@pytest.fixture
def vault(tmp_path):
    p = tmp_path / "galileo.json"
    p.write_text(json.dumps({"chainId": 16602, "rpc": "http://127.0.0.1:1",
                             "ComputeCreditVault": {"address": VAULT, "tx": "0x" + "11" * 32,
                                                    "abi_path": "chain/abi/ComputeCreditVault.json",
                                                    "block": 123}}))
    return CreditVault(rpc_url="http://127.0.0.1:1", deployment_path=str(p))


def _log(robot_id="R-04", amount=10**13, total=3 * 10**13, *, topic0=DEPOSITED_TOPIC):
    return {
        "address": Web3.to_checksum_address(VAULT),
        "topics": [bytes.fromhex(topic0[2:]), robot_key(robot_id),
                   bytes(12) + bytes.fromhex(SENDER[2:])],
        "data": encode(["string", "uint256", "uint256"], [robot_id, amount, total]),
        "blockNumber": 456, "transactionHash": bytes.fromhex("ab" * 32), "logIndex": 3,
        "transactionIndex": 0, "blockHash": bytes(32), "removed": False,
    }


def test_abi_shape():
    abi = load_vault_abi()
    names = {i.get("name") for i in abi}
    assert {"deposit", "creditOf", "withdraw", "owner", "credit", "Deposited"} <= names
    dep = next(i for i in abi if i.get("name") == "deposit")
    assert dep["stateMutability"] == "payable"
    ev = next(i for i in abi if i.get("type") == "event" and i["name"] == "Deposited")
    sig = f"Deposited({','.join(i['type'] for i in ev['inputs'])})"
    assert "0x" + Web3.keccak(text=sig).hex().removeprefix("0x") == DEPOSITED_TOPIC
    assert [i["indexed"] for i in ev["inputs"]] == [False, True, True, False, False]
    assert set(error_selectors(abi).values()) >= {"NotOwner", "ZeroValue", "BadRobotId"}


def test_construct_from_deployment_no_network(vault):
    assert vault.address == Web3.to_checksum_address(VAULT)
    assert vault.deploy_block == 123
    assert vault.rpc_url == "http://127.0.0.1:1"


def test_missing_entry_raises(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"chainId": 16602}))
    with pytest.raises(RuntimeError):
        CreditVault(deployment_path=str(p))


def test_decode_deposited_synthetic_log(vault):
    ev = decode_deposited(vault.contract, _log())
    assert ev == {"robot_id": "R-04", "from": SENDER, "amount_wei": 10**13, "total_wei": 3 * 10**13,
                  "tx_hash": "0x" + "ab" * 32, "log_index": 3, "block": 456}


def test_decode_ignores_other_topics(vault):
    assert decode_deposited(vault.contract, _log(topic0="0x" + "00" * 32)) is None
    assert decode_deposited(vault.contract, {"topics": []}) is None


def test_robot_key_is_keccak_of_bytes():
    assert robot_key("R-04") == bytes(Web3.keccak(b"R-04"))


@pytest.mark.parametrize("rid,amt", [("", 1), ("x" * 33, 1), ("R-04", 0)])
def test_deposit_validates_before_network(vault, rid, amt):
    with pytest.raises(RuntimeError):
        vault.deposit(rid, amt, "0x" + "01" * 32)


def test_abi_file_matches_path():
    assert VAULT_ABI_PATH.name == "ComputeCreditVault.json" and VAULT_ABI_PATH.is_file()
