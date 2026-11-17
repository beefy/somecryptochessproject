"""The small Ethereum JSON-RPC surface the CLI bridge needs.

The browser bridge signs the Ethereum leg with an EIP-1193 wallet. The CLI has
no browser, so this module speaks JSON-RPC to the node named by the SCCP
network's bridge environment and sends from an account the node has unlocked.
That is exactly the devnet case: Anvil prefunds and unlocks its test accounts.
For Sepolia or mainnet the wallet path is the supported one, because a
production RPC will not hold an unlocked key.

Only three contract calls are used -- ``lock``, ``claim`` and ``refund`` -- plus
the read-only ``getLock``. Their selectors and the ``Locked`` event topic are
pinned constants so this module does not need a keccak dependency.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import httpx

from sccp.bridge.config import ADDRESS_PATTERN, EthEnvironment
from sccp.errors import ProtocolError

LOCK_SELECTOR: Final = "c06aea5d"
CLAIM_SELECTOR: Final = "84cc9dfb"
REFUND_SELECTOR: Final = "7249fbb6"
GET_LOCK_SELECTOR: Final = "d6f27b58"
LOCKED_TOPIC: Final = "0x3d0ef7f4c5b5934e6cab6328a293eab713091d7ec133d6007b8821459344b6e8"
"""``keccak256("Locked(bytes32,address,address,bytes32,uint64,uint256)")``."""

MAX_UINT64: Final = 2**64 - 1
MAX_UINT256: Final = 2**256 - 1


class EthBridgeError(ProtocolError):
    """A malformed request, receipt or contract answer on the Ethereum leg."""


@dataclass(frozen=True, slots=True)
class EthLock:
    """One ``EthHtlc.Lock`` struct as ``getLock`` returns it."""

    depositor: str
    beneficiary: str
    hashlock: str
    timelock: int
    amount: int
    claimed: bool
    refunded: bool


def _word(value: int) -> str:
    if value < 0 or value > MAX_UINT256:
        raise EthBridgeError("a value does not fit one ABI word")
    return f"{value:064x}"


def _address_word(value: str) -> str:
    if not ADDRESS_PATTERN.fullmatch(value):
        raise EthBridgeError(f"{value!r} is not a 0x-prefixed 20-byte address")
    return f"{int(value, 16):064x}"


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or value != value.lower():
        raise EthBridgeError(f"{label} must be 32-byte lowercase hex")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise EthBridgeError(f"{label} must be hexadecimal") from None
    return value


def encode_lock(hashlock: str, timelock: int, beneficiary: str) -> str:
    """Calldata for ``lock(bytes32,uint64,address)``."""
    if timelock < 0 or timelock > MAX_UINT64:
        raise EthBridgeError("the Ethereum timelock must fit uint64")
    return (
        f"0x{LOCK_SELECTOR}{_digest(hashlock, 'hashlock')}"
        f"{_word(timelock)}{_address_word(beneficiary)}"
    )


def encode_claim(lock_id: str, preimage: str) -> str:
    """Calldata for ``claim(bytes32,bytes32)``."""
    return f"0x{CLAIM_SELECTOR}{_digest(lock_id, 'lock id')}{_digest(preimage, 'preimage')}"


def encode_refund(lock_id: str) -> str:
    """Calldata for ``refund(bytes32)``."""
    return f"0x{REFUND_SELECTOR}{_digest(lock_id, 'lock id')}"


def encode_get_lock(lock_id: str) -> str:
    """Calldata for ``getLock(bytes32)``."""
    return f"0x{GET_LOCK_SELECTOR}{_digest(lock_id, 'lock id')}"


def _rpc(url: str, method: str, params: Sequence[object], *, timeout: float = 10.0) -> object:
    """One JSON-RPC call, with a readable error when the node refuses it."""
    try:
        response = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            timeout=timeout,
        )
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EthBridgeError(f"cannot reach the Ethereum RPC at {url}: {exc}") from None
    if not isinstance(document, Mapping):
        raise EthBridgeError(f"{method} returned no JSON-RPC object")
    error = document.get("error")
    if error is not None:
        raise EthBridgeError(f"{method} failed: {error}")
    return document.get("result")


def _hex_int(value: object, label: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthBridgeError(f"{label} must be a 0x-prefixed hexadecimal string")
    try:
        return int(value, 16)
    except ValueError:
        raise EthBridgeError(f"{label} is not hexadecimal") from None


def chain_id(url: str) -> int:
    """The RPC's current chain id."""
    return _hex_int(_rpc(url, "eth_chainId", []), "eth_chainId")


def block_number(url: str) -> int:
    """The RPC's current block number."""
    return _hex_int(_rpc(url, "eth_blockNumber", []), "eth_blockNumber")


def assert_environment(environment: EthEnvironment, *, url: str | None = None) -> str:
    """Return the RPC URL after checking its chain id matches ``environment``."""
    rpc_url = url or environment.rpc_url
    if rpc_url is None:
        raise EthBridgeError(
            f"no Ethereum RPC is configured for {environment.network}; set SCCP_ETH_RPC_URL"
        )
    actual = chain_id(rpc_url)
    if actual != environment.chain_id:
        raise EthBridgeError(
            f"the RPC at {rpc_url} is on chain {actual}, "
            f"but {environment.network} bridges to chain {environment.chain_id}"
        )
    return rpc_url


def send_transaction(
    url: str,
    *,
    sender: str,
    contract: str,
    data: str,
    value: int = 0,
) -> str:
    """Send from an unlocked account and return the transaction hash."""
    if not ADDRESS_PATTERN.fullmatch(sender):
        raise EthBridgeError(f"{sender!r} is not a 0x-prefixed 20-byte address")
    if not ADDRESS_PATTERN.fullmatch(contract):
        raise EthBridgeError(f"{contract!r} is not a 0x-prefixed 20-byte address")
    if value < 0 or value > MAX_UINT256:
        raise EthBridgeError("the ETH value must fit uint256")
    result = _rpc(
        url,
        "eth_sendTransaction",
        [
            {
                "from": sender,
                "to": contract,
                "data": data,
                "value": f"0x{value:x}",
            }
        ],
    )
    if not isinstance(result, str) or not result.startswith("0x") or len(result) != 66:
        raise EthBridgeError("eth_sendTransaction returned no transaction hash")
    return result.lower()


def receipt(url: str, tx_hash: str) -> Mapping[str, object] | None:
    """One transaction receipt, or ``None`` while it is still pending."""
    normalized = tx_hash.lower()
    _digest(normalized.removeprefix("0x"), "transaction hash")
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"
    result = _rpc(url, "eth_getTransactionReceipt", [normalized])
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise EthBridgeError("eth_getTransactionReceipt returned no object")
    return result


def wait_for_receipt(
    url: str,
    tx_hash: str,
    confirmations: int,
    *,
    timeout_seconds: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 1.0,
) -> Mapping[str, object]:
    """Wait for ``confirmations`` blocks, then return a successful receipt."""
    if confirmations < 1:
        raise EthBridgeError("confirmations must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        found = receipt(url, tx_hash)
        if found is not None:
            mined = _hex_int(found.get("blockNumber"), "receipt blockNumber")
            tip = block_number(url)
            if tip - mined + 1 >= confirmations:
                if _hex_int(found.get("status"), "receipt status") != 1:
                    raise EthBridgeError("the Ethereum transaction reverted")
                return found
        if time.monotonic() >= deadline:
            raise EthBridgeError("timed out waiting for Ethereum confirmations")
        sleep(poll_seconds)


def _logs(found: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    raw = found.get("logs")
    if not isinstance(raw, list):
        return ()
    return [entry for entry in raw if isinstance(entry, Mapping)]


def lock_id_from_receipt(found: Mapping[str, object], contract: str) -> str:
    """The id in the ``Locked`` event emitted by ``contract``."""
    wanted = contract.lower()
    for entry in _logs(found):
        address = entry.get("address")
        topics = entry.get("topics")
        if not isinstance(address, str) or address.lower() != wanted:
            continue
        if not isinstance(topics, list) or len(topics) < 2:
            continue
        if str(topics[0]).lower() != LOCKED_TOPIC:
            continue
        lock_id = topics[1]
        if not isinstance(lock_id, str):
            break
        return lock_id.removeprefix("0x").lower()
    raise EthBridgeError("the Ethereum lock emitted no Locked event")


def get_lock(url: str, contract: str, lock_id: str) -> EthLock:
    """Read one lock through ``getLock``."""
    result = _rpc(
        url,
        "eth_call",
        [{"to": contract, "data": encode_get_lock(lock_id)}, "latest"],
    )
    if not isinstance(result, str) or not result.startswith("0x"):
        raise EthBridgeError("getLock returned no ABI data")
    body = result[2:]
    if len(body) < 64 * 7:
        raise EthBridgeError("getLock returned fewer than seven ABI words")
    words = [body[index : index + 64] for index in range(0, 64 * 7, 64)]
    try:
        values = [int(word, 16) for word in words]
    except ValueError:
        raise EthBridgeError("getLock returned non-hex ABI words") from None
    return EthLock(
        depositor=f"0x{values[0]:040x}",
        beneficiary=f"0x{values[1]:040x}",
        hashlock=f"{values[2]:064x}",
        timelock=values[3],
        amount=values[4],
        claimed=values[5] != 0,
        refunded=values[6] != 0,
    )


def verify_lock(
    url: str,
    contract: str,
    lock_id: str,
    *,
    depositor: str,
    beneficiary: str,
    hashlock: str,
    timelock: int,
    amount: int,
) -> EthLock:
    """Check a lock against the fields a take names, and that it is still open."""
    stored = get_lock(url, contract, lock_id)
    if stored.depositor.lower() != depositor.lower():
        raise EthBridgeError("the Ethereum lock has a different depositor")
    if stored.beneficiary.lower() != beneficiary.lower():
        raise EthBridgeError("the Ethereum lock pays a different beneficiary")
    if stored.hashlock != hashlock.lower():
        raise EthBridgeError("the Ethereum lock has a different hashlock")
    if (stored.timelock, stored.amount) != (timelock, amount):
        raise EthBridgeError("the Ethereum lock has a different timelock or amount")
    if stored.claimed or stored.refunded:
        raise EthBridgeError("the Ethereum lock is already closed")
    return stored


def lock_eth(
    url: str,
    contract: str,
    *,
    sender: str,
    hashlock: str,
    timelock: int,
    beneficiary: str,
    value: int,
    confirmations: int,
) -> tuple[str, str]:
    """Lock ETH and return ``(tx_hash, lock_id)`` after confirmations."""
    tx_hash = send_transaction(
        url,
        sender=sender,
        contract=contract,
        data=encode_lock(hashlock, timelock, beneficiary),
        value=value,
    )
    found = wait_for_receipt(url, tx_hash, confirmations)
    return tx_hash, lock_id_from_receipt(found, contract)


def claim_eth(
    url: str,
    contract: str,
    *,
    sender: str,
    lock_id: str,
    preimage: str,
    confirmations: int,
) -> str:
    """Claim ETH with the shared preimage and return the transaction hash."""
    tx_hash = send_transaction(
        url,
        sender=sender,
        contract=contract,
        data=encode_claim(lock_id, preimage),
    )
    wait_for_receipt(url, tx_hash, confirmations)
    return tx_hash


def refund_eth(
    url: str,
    contract: str,
    *,
    sender: str,
    lock_id: str,
    confirmations: int,
) -> str:
    """Refund an expired ETH lock to its depositor."""
    tx_hash = send_transaction(
        url,
        sender=sender,
        contract=contract,
        data=encode_refund(lock_id),
    )
    wait_for_receipt(url, tx_hash, confirmations)
    return tx_hash


__all__ = [
    "LOCKED_TOPIC",
    "EthBridgeError",
    "EthLock",
    "assert_environment",
    "block_number",
    "chain_id",
    "claim_eth",
    "encode_claim",
    "encode_get_lock",
    "encode_lock",
    "encode_refund",
    "get_lock",
    "lock_eth",
    "lock_id_from_receipt",
    "receipt",
    "refund_eth",
    "send_transaction",
    "verify_lock",
    "wait_for_receipt",
]
