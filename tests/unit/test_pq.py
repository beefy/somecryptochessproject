"""Hybrid Ed25519 + ML-DSA-65 keys: addresses, keyfiles, and ledger spends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sccp.chain.state import State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    validate_transaction,
)
from sccp.config import DEVNET
from sccp.crypto.address import (
    ADDRESS_VERSION,
    HYBRID_ADDRESS_VERSION,
    address_version,
    decode_address,
    encode_address,
    encode_hybrid_address,
    validate_address,
)
from sccp.crypto.keys import keypair_from_seed
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HYBRID_SIGNATURE_SIZE,
    ed25519_public_key_hex,
    hybrid_keypair_from_seed,
    hybrid_public_key_from_bytes,
    hybrid_sign,
    hybrid_verify,
    is_hybrid_public_key_hex,
    pq_available,
)
from sccp.crypto.wallet import (
    KEYFILE_VERSION,
    KEYFILE_VERSION_PQ,
    SCHEME_ED25519,
    SCHEME_HYBRID,
    create_wallet,
    export_mnemonic,
    import_mnemonic,
    load_wallet,
    read_header,
)
from sccp.errors import (
    KeyEncodingError,
    KeyfileError,
    PassphraseError,
    TxValidationError,
)
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService

requires_pq = pytest.mark.skipif(not pq_available(), reason="ML-DSA-65 backend unavailable")

SEED = bytes([21]) * 32
HEIGHT = 3
COINS = 100_000_000


def _hybrid() -> Any:
    return hybrid_keypair_from_seed(SEED)


def _ledger(keypair: Any, address: str) -> State:
    """Devnet state with one funded account holding ``keypair``'s public key."""
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[1_700_000_000])
    account = state.ensure(address)
    account.public_key = keypair.public_key_hex
    account.balance = 1_000 * COINS
    return state


def _reason(excinfo: pytest.ExceptionInfo[TxValidationError]) -> TxFailure:
    return excinfo.value.reason


@requires_pq
def test_hybrid_signature_round_trip() -> None:
    keypair = _hybrid()
    message = b"sccp hybrid message"
    signature = hybrid_sign(keypair, message)
    assert len(signature) == HYBRID_SIGNATURE_SIZE
    assert signature[0] == 1
    assert hybrid_verify(keypair.public_key_bytes, message, signature)
    assert not hybrid_verify(keypair.public_key_bytes, message + b"!", signature)
    other = hybrid_keypair_from_seed(bytes([22]) * 32)
    assert not hybrid_verify(other.public_key_bytes, message, signature)


@requires_pq
def test_hybrid_keypair_is_deterministic_and_self_describing() -> None:
    first = _hybrid()
    second = _hybrid()
    assert first.public_key_bytes == second.public_key_bytes
    assert len(first.public_key_bytes) == HYBRID_PUBLIC_KEY_SIZE
    assert first.public_key_bytes[0] == 1
    assert first.public_key_bytes[1:33] == first.ed25519.public_key_bytes
    assert is_hybrid_public_key_hex(first.public_key_hex)
    assert is_hybrid_public_key_hex(first.ed25519.public_key_hex) is False


@requires_pq
def test_ed25519_public_key_hex_extracts_the_classical_half() -> None:
    keypair = _hybrid()
    extracted = ed25519_public_key_hex(keypair.public_key_hex)
    assert extracted == keypair.ed25519.public_key_hex
    assert len(extracted) == 64
    # Already-classical keys and malformed blobs pass through untouched so a
    # verifier still fails closed instead of silently accepting a rewrite.
    assert ed25519_public_key_hex(keypair.ed25519.public_key_hex) == keypair.ed25519.public_key_hex
    assert ed25519_public_key_hex("00") == "00"


@requires_pq
def test_malformed_hybrid_inputs_fail_closed() -> None:
    keypair = _hybrid()
    message = b"scheme-tagged message"
    signature = hybrid_sign(keypair, message)

    # Signing is the only path that raises: a caller handed the wrong key type
    # learns about it before a half-signed blob can reach the wire.
    with pytest.raises(KeyEncodingError):
        hybrid_sign(keypair_from_seed(SEED), message)  # type: ignore[arg-type]
    with pytest.raises(KeyEncodingError):
        hybrid_public_key_from_bytes(keypair.public_key_bytes[:-1])
    wrong_scheme = bytes([2]) + keypair.public_key_bytes[1:]
    with pytest.raises(KeyEncodingError):
        hybrid_public_key_from_bytes(wrong_scheme)

    # Verification consumes untrusted data, so every malformed shape is False.
    assert hybrid_verify(keypair.public_key_bytes, message, signature[:-1]) is False
    assert hybrid_verify(keypair.public_key_bytes, message, b"\x02" + signature[1:]) is False
    assert hybrid_verify(keypair.public_key_bytes[:-1], message, signature) is False
    assert hybrid_verify(wrong_scheme, message, signature) is False
    assert hybrid_verify(keypair.public_key_bytes, b"another message", signature) is False


@requires_pq
def test_hybrid_address_is_version_one_and_round_trips() -> None:
    keypair = _hybrid()
    address = encode_hybrid_address(keypair.public_key_bytes, DEVNET)
    decoded = decode_address(address)
    assert decoded.version == HYBRID_ADDRESS_VERSION
    assert len(decoded.payload) == 32
    assert address_version(address) == 1
    assert validate_address(address, DEVNET)
    assert validate_address(address, "testnet")
    # The same Ed25519 half still has a distinct version-0 address.
    classical = encode_address(keypair.ed25519.public_key_bytes, DEVNET)
    assert classical != address
    assert address_version(classical) == ADDRESS_VERSION


def test_hybrid_address_rejects_a_classical_key() -> None:
    keypair = keypair_from_seed(SEED)
    with pytest.raises(KeyEncodingError):
        encode_hybrid_address(keypair.public_key_bytes, DEVNET)


def _recipient() -> str:
    return encode_address(keypair_from_seed(bytes([3]) * 32).public_key_bytes, DEVNET)


@requires_pq
def test_hybrid_spend_moves_funds_and_verifies() -> None:
    keypair = _hybrid()
    address = encode_hybrid_address(keypair.public_key_bytes, DEVNET)
    state = _ledger(keypair, address)
    service = WalletService(DEVNET, state)
    to = _recipient()
    tx = service.build_transfer(keypair, to, 10 * COINS)
    assert tx.sender == address
    assert tx.payload["public_key"] == keypair.public_key_hex
    assert len(tx.signature or "") == HYBRID_SIGNATURE_SIZE * 2
    validate_transaction(tx, DEVNET, state, height=HEIGHT)
    apply_transaction(state, tx, height=HEIGHT)
    assert state.account(to).balance == 10 * COINS
    assert state.account(address).balance == 990 * COINS - int(tx.fee)


@requires_pq
def test_first_use_reveals_the_hybrid_key() -> None:
    keypair = _hybrid()
    address = encode_hybrid_address(keypair.public_key_bytes, DEVNET)
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[1_700_000_000])
    state.ensure(address).balance = 5 * COINS
    assert state.accounts[address].public_key is None
    service = WalletService(DEVNET, state)
    tx = service.build_transfer(keypair, _recipient(), COINS)
    validate_transaction(tx, DEVNET, state, height=HEIGHT)
    apply_transaction(state, tx, height=HEIGHT)
    assert state.accounts[address].public_key == keypair.public_key_hex
    # Ed25519-only consumers get the classical half, never the blob.
    assert state.public_keys()[address] == keypair.ed25519.public_key_hex


@requires_pq
def test_a_classical_signature_cannot_spend_a_hybrid_account() -> None:
    keypair = _hybrid()
    address = encode_hybrid_address(keypair.public_key_bytes, DEVNET)
    state = _ledger(keypair, address)
    tx = Transaction(
        network=DEVNET.network_id,
        type=TxType.TRANSFER,
        sender=address,
        nonce=0,
        fee=DEVNET.min_fee,
        payload={"to": _recipient(), "amount": COINS, "public_key": keypair.public_key_hex},
    )
    signed = sign_transaction(keypair.ed25519, tx)
    with pytest.raises(TxValidationError) as excinfo:
        validate_transaction(signed, DEVNET, state, height=HEIGHT)
    assert _reason(excinfo) is TxFailure.SIGNATURE


@requires_pq
def test_a_hybrid_key_cannot_spend_a_classical_address() -> None:
    classical = keypair_from_seed(SEED)
    address = encode_address(classical.public_key_bytes, DEVNET)
    state = State(params=DEVNET, height=HEIGHT - 1, timestamps=[1_700_000_000])
    state.ensure(address).balance = 5 * COINS
    keypair = _hybrid()
    tx = Transaction(
        network=DEVNET.network_id,
        type=TxType.TRANSFER,
        sender=address,
        nonce=0,
        fee=DEVNET.min_fee,
        payload={"to": _recipient(), "amount": COINS, "public_key": keypair.public_key_hex},
    )
    signed = sign_transaction(keypair, tx)
    with pytest.raises(TxValidationError) as excinfo:
        validate_transaction(signed, DEVNET, state, height=HEIGHT)
    assert _reason(excinfo) is TxFailure.SIGNATURE


@requires_pq
def test_transaction_signature_sizes_are_scheme_tagged() -> None:
    base = {
        "network": DEVNET.network_id,
        "type": TxType.TRANSFER,
        "sender": _recipient(),
        "nonce": 0,
        "fee": DEVNET.min_fee,
        "payload": {},
    }
    with pytest.raises(ValidationError):
        Transaction(**base, signature="00" * 100)
    assert len(Transaction(**base, signature="00" * HYBRID_SIGNATURE_SIZE).signature or "") == (
        HYBRID_SIGNATURE_SIZE * 2
    )
    assert len(Transaction(**base, signature="00" * 64).signature or "") == 128


def _document(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


@requires_pq
def test_hybrid_keyfile_round_trip(tmp_path: Path, passphrase: str, fast_kdf: Any) -> None:
    path = tmp_path / "hybrid.key"
    wallet = create_wallet(
        path,
        "testnet",
        passphrase,
        seed=SEED,
        scheme=SCHEME_HYBRID,
        kdf=fast_kdf,
        created_at=1_700_000_000,
    )
    assert wallet.is_hybrid
    assert wallet.scheme == SCHEME_HYBRID
    assert wallet.header.version == KEYFILE_VERSION_PQ
    assert wallet.address.startswith("sccpt1")
    assert address_version(wallet.address) == HYBRID_ADDRESS_VERSION
    assert wallet.signer is wallet.pq_keypair

    document = _document(path)
    assert document["version"] == KEYFILE_VERSION_PQ
    assert document["scheme"] == SCHEME_HYBRID
    assert document["public_key"] == _hybrid().public_key_hex

    loaded = load_wallet(path, passphrase)
    assert loaded.is_hybrid
    assert loaded.address == wallet.address
    assert loaded.public_key_hex == wallet.public_key_hex
    assert loaded.signer.public_key_hex == wallet.public_key_hex
    assert load_wallet(path, passphrase).seed == SEED


def test_version_one_keyfile_is_unchanged(tmp_path: Path, passphrase: str, fast_kdf: Any) -> None:
    path = tmp_path / "classical.key"
    wallet = create_wallet(
        path, "testnet", passphrase, seed=SEED, kdf=fast_kdf, created_at=1_700_000_000
    )
    document = _document(path)
    assert set(document) == {
        "version",
        "network",
        "address",
        "public_key",
        "kdf",
        "cipher",
        "created_at",
        "ciphertext",
    }
    assert document["version"] == KEYFILE_VERSION
    assert wallet.scheme == SCHEME_ED25519
    assert wallet.is_hybrid is False
    assert read_header(path).scheme == SCHEME_ED25519
    assert load_wallet(path, passphrase).public_key_hex == wallet.public_key_hex


@requires_pq
def test_hybrid_mnemonic_backup_restores_both_halves(
    tmp_path: Path, passphrase: str, fast_kdf: Any
) -> None:
    source = create_wallet(
        tmp_path / "source.key",
        "testnet",
        passphrase,
        seed=SEED,
        scheme=SCHEME_HYBRID,
        kdf=fast_kdf,
    )
    phrase = export_mnemonic(source.path, passphrase)  # type: ignore[arg-type]
    restored = import_mnemonic(
        tmp_path / "restored.key",
        phrase,
        "testnet",
        passphrase,
        scheme=SCHEME_HYBRID,
        kdf=fast_kdf,
    )
    assert restored.address == source.address
    assert restored.public_key_hex == source.public_key_hex
    assert restored.seed == SEED


@requires_pq
def test_hybrid_keyfiles_require_a_long_passphrase(tmp_path: Path, fast_kdf: Any) -> None:
    with pytest.raises(PassphraseError):
        create_wallet(
            tmp_path / "weak.key",
            "testnet",
            "short",
            seed=SEED,
            scheme=SCHEME_HYBRID,
            kdf=fast_kdf,
        )
    assert not (tmp_path / "weak.key").exists()


def test_unknown_scheme_is_refused(tmp_path: Path, passphrase: str, fast_kdf: Any) -> None:
    with pytest.raises(KeyfileError):
        create_wallet(
            tmp_path / "bad.key",
            "testnet",
            passphrase,
            seed=SEED,
            scheme="ed25519-mldsa87",
            kdf=fast_kdf,
        )


@requires_pq
def test_hybrid_header_cannot_claim_a_classical_address(
    tmp_path: Path, passphrase: str, fast_kdf: Any
) -> None:
    from sccp.errors import KeyfileError

    path = tmp_path / "hybrid.key"
    wallet = create_wallet(
        path,
        "testnet",
        passphrase,
        seed=SEED,
        scheme=SCHEME_HYBRID,
        kdf=fast_kdf,
    )
    document = _document(path)
    document["address"] = encode_address(wallet.keypair.public_key_bytes, "testnet")
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(KeyfileError):
        read_header(path)
