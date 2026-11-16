"""The CLI's Ethereum-leg ABI and receipt helpers."""

from __future__ import annotations

import pytest

from sccp.bridge.eth import (
    LOCKED_TOPIC,
    EthBridgeError,
    encode_claim,
    encode_get_lock,
    encode_lock,
    encode_refund,
    get_lock,
    lock_id_from_receipt,
)
from sccp.bridge.orders import OrderError, sccp_timelock_height

HASHLOCK = "02d449a31fbb267c8f352e9968a79e3e5fc95c1bbeaa502fd6454ebde5a4bedc"
PREIMAGE = "11" * 32
CONTRACT = f"0x{'12' * 20}"


def test_lock_calldata_matches_the_contract_abi() -> None:
    data = encode_lock(HASHLOCK, 1_700_003_600, CONTRACT)
    assert data.startswith("0x")
    assert data[2:10] == "c06aea5d"
    assert data[10:74] == HASHLOCK
    assert int(data[74:138], 16) == 1_700_003_600
    assert data[-40:] == "12" * 20


def test_claim_and_refund_calldata_are_fixed_width() -> None:
    lock_id = "ab" * 32
    claim = encode_claim(lock_id, PREIMAGE)
    refund = encode_refund(lock_id)
    assert claim[2:10] == "84cc9dfb"
    assert claim[10:74] == lock_id
    assert claim[74:138] == PREIMAGE
    assert refund == f"0x7249fbb6{lock_id}"
    assert encode_get_lock(lock_id) == f"0xd6f27b58{lock_id}"


def test_lock_id_comes_from_the_locked_event() -> None:
    lock_id = "cd" * 32
    receipt = {
        "transactionHash": f"0x{'34' * 32}",
        "blockNumber": "0x10",
        "status": "0x1",
        "logs": [
            {
                "address": CONTRACT,
                "topics": [LOCKED_TOPIC, f"0x{lock_id}", "0x" + "00" * 32],
                "data": "0x",
            }
        ],
    }
    assert lock_id_from_receipt(receipt, CONTRACT) == lock_id


def test_a_receipt_without_the_event_is_refused() -> None:
    with pytest.raises(EthBridgeError, match="no Locked event"):
        lock_id_from_receipt({"logs": []}, CONTRACT)


def test_get_lock_decodes_all_seven_static_words(monkeypatch: pytest.MonkeyPatch) -> None:
    def word(value: int) -> str:
        return f"{value:064x}"

    depositor = "0x" + "11" * 20
    beneficiary = "0x" + "22" * 20
    result = "0x" + "".join(
        [
            word(int(depositor, 16)),
            word(int(beneficiary, 16)),
            word(int(HASHLOCK, 16)),
            word(1_700_003_600),
            word(5_000_000_000_000_000),
            word(0),
            word(0),
        ]
    )

    def fake_rpc(url: str, method: str, params: list[object], *, timeout: float = 10.0) -> object:
        del url, method, params, timeout
        return result

    monkeypatch.setattr("sccp.bridge.eth._rpc", fake_rpc)
    stored = get_lock("http://anvil", CONTRACT, "ab" * 32)
    assert stored.depositor == depositor
    assert stored.beneficiary == beneficiary
    assert stored.hashlock == HASHLOCK
    assert stored.timelock == 1_700_003_600
    assert stored.amount == 5_000_000_000_000_000
    assert stored.claimed is False and stored.refunded is False


def test_the_sccp_timelock_keeps_the_margin_before_the_eth_lock() -> None:
    assert sccp_timelock_height(1_000, 1_700_007_200, 1_700_000_000, 60, 24, 0) == 1_096
    with pytest.raises(OrderError, match="too short"):
        sccp_timelock_height(1_000, 1_700_000_100, 1_700_000_000, 60, 24, 0)
    with pytest.raises(OrderError, match="only safe up to"):
        sccp_timelock_height(1_000, 1_700_003_600, 1_700_000_000, 60, 24, 2_000)
