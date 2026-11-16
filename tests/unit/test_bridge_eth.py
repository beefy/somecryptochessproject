"""The CLI's Ethereum-leg ABI and receipt helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from sccp.bridge.config import EthEnvironment
from sccp.bridge.eth import (
    LOCKED_TOPIC,
    EthBridgeError,
    EthLock,
    assert_environment,
    block_number,
    chain_id,
    claim_eth,
    encode_claim,
    encode_get_lock,
    encode_lock,
    encode_refund,
    get_lock,
    lock_eth,
    lock_id_from_receipt,
    receipt,
    refund_eth,
    send_transaction,
    verify_lock,
    wait_for_receipt,
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


def _environment(**updates: Any) -> EthEnvironment:
    values: dict[str, Any] = {
        "network": "devnet",
        "chain_id": 31337,
        "chain_name": "Anvil devnet",
        "explorer_url": None,
        "rpc_url": "http://anvil",
        "contract": CONTRACT,
        "confirmations": 1,
        "sccp_margin": 24,
    }
    values.update(updates)
    return EthEnvironment(**values)


class _Response:
    def __init__(
        self,
        document: object = None,
        *,
        status_error: Exception | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.document = document
        self.status_error = status_error
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_error is not None:
            raise self.status_error

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.document


def test_calldata_rejects_values_outside_the_abi() -> None:
    with pytest.raises(EthBridgeError, match="uint64"):
        encode_lock(HASHLOCK, -1, CONTRACT)
    with pytest.raises(EthBridgeError, match="uint64"):
        encode_lock(HASHLOCK, 2**64, CONTRACT)
    with pytest.raises(EthBridgeError, match="20-byte address"):
        encode_lock(HASHLOCK, 1, "0x1234")
    with pytest.raises(EthBridgeError, match="lowercase"):
        encode_claim("AB" * 32, PREIMAGE)
    with pytest.raises(EthBridgeError, match="hexadecimal"):
        encode_refund("gg" * 32)


def test_json_rpc_returns_a_result_and_wraps_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def ok(url: str, *, json: dict[str, object], timeout: float) -> _Response:
        seen.update(url=url, json=json, timeout=timeout)
        return _Response({"result": "0x7a69"})

    monkeypatch.setattr("sccp.bridge.eth.httpx.post", ok)
    assert chain_id("http://anvil") == 31_337
    assert seen["url"] == "http://anvil"
    assert seen["json"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_chainId",
        "params": [],
    }

    def refuses(url: str, *, json: dict[str, object], timeout: float) -> _Response:
        del url, json, timeout
        return _Response(status_error=httpx.ConnectError("down"))

    monkeypatch.setattr("sccp.bridge.eth.httpx.post", refuses)
    with pytest.raises(EthBridgeError, match="cannot reach"):
        chain_id("http://anvil")

    def bad_json(url: str, *, json: dict[str, object], timeout: float) -> _Response:
        del url, json, timeout
        return _Response(json_error=ValueError("not JSON"))

    monkeypatch.setattr("sccp.bridge.eth.httpx.post", bad_json)
    with pytest.raises(EthBridgeError, match="cannot reach"):
        chain_id("http://anvil")

    monkeypatch.setattr(
        "sccp.bridge.eth.httpx.post",
        lambda *args, **kwargs: _Response(["not", "an", "object"]),
    )
    with pytest.raises(EthBridgeError, match="no JSON-RPC object"):
        chain_id("http://anvil")

    monkeypatch.setattr(
        "sccp.bridge.eth.httpx.post",
        lambda *args, **kwargs: _Response({"error": {"message": "nope"}}),
    )
    with pytest.raises(EthBridgeError, match="eth_chainId failed"):
        chain_id("http://anvil")


def test_chain_and_block_numbers_must_be_hexadecimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: "0x10")
    assert block_number("http://anvil") == 16
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: 16)
    with pytest.raises(EthBridgeError, match="0x-prefixed"):
        chain_id("http://anvil")
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: "0xten")
    with pytest.raises(EthBridgeError, match="not hexadecimal"):
        block_number("http://anvil")


def test_assert_environment_requires_an_rpc_and_the_expected_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EthBridgeError, match="no Ethereum RPC"):
        assert_environment(_environment(rpc_url=None))
    monkeypatch.setattr("sccp.bridge.eth.chain_id", lambda url: 1)
    with pytest.raises(EthBridgeError, match="is on chain 1"):
        assert_environment(_environment())
    monkeypatch.setattr("sccp.bridge.eth.chain_id", lambda url: 31337)
    assert assert_environment(_environment(), url="http://other") == "http://other"


def test_send_transaction_validates_addresses_value_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx_hash = "0x" + "AB" * 32
    seen: dict[str, object] = {}

    def fake_rpc(url: str, method: str, params: list[object]) -> object:
        seen.update(url=url, method=method, params=params)
        return tx_hash

    monkeypatch.setattr("sccp.bridge.eth._rpc", fake_rpc)
    assert (
        send_transaction(
            "http://anvil",
            sender=CONTRACT,
            contract=CONTRACT,
            data="0xdead",
            value=2,
        )
        == tx_hash.lower()
    )
    assert seen["method"] == "eth_sendTransaction"
    assert seen["params"] == [
        {
            "from": CONTRACT,
            "to": CONTRACT,
            "data": "0xdead",
            "value": "0x2",
        }
    ]

    with pytest.raises(EthBridgeError, match="20-byte address"):
        send_transaction("http://anvil", sender="nope", contract=CONTRACT, data="0x")
    with pytest.raises(EthBridgeError, match="20-byte address"):
        send_transaction("http://anvil", sender=CONTRACT, contract="nope", data="0x")
    with pytest.raises(EthBridgeError, match="value"):
        send_transaction("http://anvil", sender=CONTRACT, contract=CONTRACT, data="0x", value=-1)
    with pytest.raises(EthBridgeError, match="value"):
        send_transaction(
            "http://anvil",
            sender=CONTRACT,
            contract=CONTRACT,
            data="0x",
            value=2**256,
        )

    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: None)
    with pytest.raises(EthBridgeError, match="no transaction hash"):
        send_transaction("http://anvil", sender=CONTRACT, contract=CONTRACT, data="0x")
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: "0x1234")
    with pytest.raises(EthBridgeError, match="no transaction hash"):
        send_transaction("http://anvil", sender=CONTRACT, contract=CONTRACT, data="0x")


def test_receipt_normalises_hashes_and_handles_pending_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EthBridgeError, match="transaction hash"):
        receipt("http://anvil", "0x1234")
    with pytest.raises(EthBridgeError, match="hexadecimal"):
        receipt("http://anvil", "gg" * 32)

    seen: dict[str, object] = {}

    def pending(url: str, method: str, params: list[object]) -> object:
        seen.update(url=url, method=method, params=params)
        return None

    monkeypatch.setattr("sccp.bridge.eth._rpc", pending)
    assert receipt("http://anvil", "AB" * 32) is None
    assert seen["params"] == ["0x" + "ab" * 32]

    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: ["not", "a", "receipt"])
    with pytest.raises(EthBridgeError, match="no object"):
        receipt("http://anvil", "ab" * 32)
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: {"status": "0x1"})
    assert receipt("http://anvil", "ab" * 32) == {"status": "0x1"}


def test_wait_for_receipt_times_out_reverts_and_waits_for_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EthBridgeError, match="positive"):
        wait_for_receipt("http://anvil", "ab" * 32, 0)

    monkeypatch.setattr("sccp.bridge.eth.receipt", lambda *args, **kwargs: None)
    with pytest.raises(EthBridgeError, match="timed out"):
        wait_for_receipt("http://anvil", "ab" * 32, 1, timeout_seconds=0)

    found = {"blockNumber": "0x5", "status": "0x1"}
    monkeypatch.setattr("sccp.bridge.eth.receipt", lambda *args, **kwargs: found)
    monkeypatch.setattr("sccp.bridge.eth.block_number", lambda url: 5)
    with pytest.raises(EthBridgeError, match="timed out"):
        wait_for_receipt("http://anvil", "ab" * 32, 2, timeout_seconds=0)

    monkeypatch.setattr("sccp.bridge.eth.block_number", lambda url: 6)
    assert wait_for_receipt("http://anvil", "ab" * 32, 2) == found

    reverted = {"blockNumber": "0x5", "status": "0x0"}
    monkeypatch.setattr("sccp.bridge.eth.receipt", lambda *args, **kwargs: reverted)
    with pytest.raises(EthBridgeError, match="reverted"):
        wait_for_receipt("http://anvil", "ab" * 32, 1)


def test_only_the_contracts_locked_event_names_a_lock() -> None:
    bad_receipts = [
        {"logs": None},
        {"logs": ["not-a-log"]},
        {"logs": [{"address": "0x" + "99" * 20, "topics": [LOCKED_TOPIC, "0x" + "ab" * 32]}]},
        {"logs": [{"address": CONTRACT, "topics": "not-a-list"}]},
        {"logs": [{"address": CONTRACT, "topics": [LOCKED_TOPIC]}]},
        {"logs": [{"address": CONTRACT, "topics": ["0xdead", "0x" + "ab" * 32]}]},
        {"logs": [{"address": CONTRACT, "topics": [LOCKED_TOPIC, 7]}]},
    ]
    for found in bad_receipts:
        with pytest.raises(EthBridgeError, match="no Locked event"):
            lock_id_from_receipt(found, CONTRACT)

    found = {
        "logs": [
            "not-a-log",
            {"address": 7, "topics": [LOCKED_TOPIC, "0x" + "cd" * 32]},
            {"address": CONTRACT, "topics": ["0xdead", "0x" + "cd" * 32]},
            {"address": CONTRACT, "topics": [LOCKED_TOPIC, "0x" + "CD" * 32]},
        ]
    }
    assert lock_id_from_receipt(found, CONTRACT) == "cd" * 32


def test_get_lock_refuses_answers_that_are_not_seven_abi_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: None)
    with pytest.raises(EthBridgeError, match="no ABI data"):
        get_lock("http://anvil", CONTRACT, "ab" * 32)
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: "0x" + "00" * 64)
    with pytest.raises(EthBridgeError, match="fewer than seven"):
        get_lock("http://anvil", CONTRACT, "ab" * 32)
    monkeypatch.setattr("sccp.bridge.eth._rpc", lambda *args, **kwargs: "0x" + "gg" * 224)
    with pytest.raises(EthBridgeError, match="non-hex"):
        get_lock("http://anvil", CONTRACT, "ab" * 32)


_STORED_LOCK = EthLock(
    depositor="0x" + "11" * 20,
    beneficiary="0x" + "22" * 20,
    hashlock=HASHLOCK,
    timelock=1_700_003_600,
    amount=5_000_000_000_000_000,
    claimed=False,
    refunded=False,
)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("depositor", "0x" + "33" * 20, "different depositor"),
        ("beneficiary", "0x" + "44" * 20, "different beneficiary"),
        ("hashlock", "ef" * 32, "different hashlock"),
        ("timelock", 1, "different timelock or amount"),
        ("amount", 1, "different timelock or amount"),
        ("claimed", True, "already closed"),
        ("refunded", True, "already closed"),
    ],
)
def test_verify_lock_checks_every_named_field(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    changed = replace(_STORED_LOCK, **{field: value})
    monkeypatch.setattr("sccp.bridge.eth.get_lock", lambda *args, **kwargs: changed)
    with pytest.raises(EthBridgeError, match=message):
        verify_lock(
            "http://anvil",
            CONTRACT,
            "ab" * 32,
            depositor=_STORED_LOCK.depositor,
            beneficiary=_STORED_LOCK.beneficiary,
            hashlock=HASHLOCK,
            timelock=_STORED_LOCK.timelock,
            amount=_STORED_LOCK.amount,
        )


def test_verify_lock_returns_a_matching_open_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sccp.bridge.eth.get_lock", lambda *args, **kwargs: _STORED_LOCK)
    assert (
        verify_lock(
            "http://anvil",
            CONTRACT,
            "ab" * 32,
            depositor=_STORED_LOCK.depositor.upper().replace("0X", "0x"),
            beneficiary=_STORED_LOCK.beneficiary,
            hashlock=HASHLOCK.upper(),
            timelock=_STORED_LOCK.timelock,
            amount=_STORED_LOCK.amount,
        )
        == _STORED_LOCK
    )


def test_transaction_wrappers_send_wait_and_extract_the_lock_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx_hash = "0x" + "ab" * 32
    calls: list[dict[str, object]] = []

    def fake_send(url: str, **fields: object) -> str:
        calls.append({"url": url, **fields})
        return tx_hash

    def fake_wait(url: str, found_hash: str, confirmations: int) -> dict[str, object]:
        assert (url, found_hash, confirmations) == ("http://anvil", tx_hash, 2)
        return {"logs": []}

    monkeypatch.setattr("sccp.bridge.eth.send_transaction", fake_send)
    monkeypatch.setattr("sccp.bridge.eth.wait_for_receipt", fake_wait)
    monkeypatch.setattr("sccp.bridge.eth.lock_id_from_receipt", lambda *args: "cd" * 32)

    assert lock_eth(
        "http://anvil",
        CONTRACT,
        sender=CONTRACT,
        hashlock=HASHLOCK,
        timelock=1_700_003_600,
        beneficiary=CONTRACT,
        value=5,
        confirmations=2,
    ) == (tx_hash, "cd" * 32)
    assert (
        claim_eth(
            "http://anvil",
            CONTRACT,
            sender=CONTRACT,
            lock_id="cd" * 32,
            preimage=PREIMAGE,
            confirmations=2,
        )
        == tx_hash
    )
    assert (
        refund_eth(
            "http://anvil",
            CONTRACT,
            sender=CONTRACT,
            lock_id="cd" * 32,
            confirmations=2,
        )
        == tx_hash
    )
    assert calls[0]["value"] == 5
    assert calls[1]["data"] == encode_claim("cd" * 32, PREIMAGE)
    assert calls[2]["data"] == encode_refund("cd" * 32)
