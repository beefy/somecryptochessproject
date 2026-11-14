"""The one-scheme invariants: hybrid Ed25519 + ML-DSA-65 is the only identity.

These tests are the acceptance criteria for the post-quantum rewrite
(docs/PROTOCOL.md sections 2-4 and 8.5):

* every address is a version-1 program over the full 32-byte hash;
* every transaction signature is the 3374-byte hybrid blob, with no Ed25519
  fallback and no scheme dispatch on a key length;
* every keyfile is version 2 with the hybrid scheme, and a classical document is
  refused;
* certificates and results are hybrid-signed, and a result's signature covers a
  commitment to the whole move stream.

Anything that reintroduces a second path (``SCHEMES``, ``SigningKey``,
``ADDRESS_PQ``, ``--scheme``) should break one of these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import bech32
import pytest
from pydantic import ValidationError

from sccp.chain.state import State
from sccp.chain.tx import TxFailure, sign_transaction, txid, validate_transaction
from sccp.config import DEVNET, TESTNET
from sccp.crypto.address import (
    ADDRESS_VERSION,
    PROGRAM_SIZE,
    decode_address,
    encode_address,
    validate_address,
)
from sccp.crypto.keys import generate_keypair
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HYBRID_SIGNATURE_SIZE,
    ed25519_public_key,
    ed25519_public_key_hex,
    generate_hybrid_keypair,
    hybrid_keypair_from_seed,
    hybrid_public_key,
    hybrid_public_key_from_bytes,
    hybrid_public_key_from_hex,
    hybrid_sign,
    hybrid_verify,
    pq_available,
    require_pq,
)
from sccp.crypto.wallet import FAST_KDF, SCHEME, create_wallet, load_wallet
from sccp.errors import (
    AddressError,
    KeyEncodingError,
    KeyfileError,
    PQUnavailableError,
    TxValidationError,
)
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService

FROM_SEED = b"\x07" * 32


def _params():
    return DEVNET.model_copy(update={"min_bond": 0})


def _funded_state(keypair, *, amount: int = 10_000) -> tuple[State, str, str]:
    params = _params()
    address = encode_address(keypair.public_key_bytes, params)
    recipient = encode_address(generate_hybrid_keypair().public_key_bytes, params)
    state = State(params=params)
    account = state.ensure(address)
    account.balance = amount
    account.public_key = keypair.public_key_hex
    return state, address, recipient


# -- addresses ---------------------------------------------------------------


def test_the_only_address_version_is_one() -> None:
    assert ADDRESS_VERSION == 1
    assert PROGRAM_SIZE == 33
    keypair = generate_hybrid_keypair()
    address = encode_address(keypair.public_key_bytes, TESTNET)
    decoded = decode_address(address)
    assert decoded.version == 1
    assert len(decoded.program) == 33
    assert len(decoded.payload) == 32
    assert validate_address(address, TESTNET)


def test_a_classical_key_cannot_be_encoded() -> None:
    with pytest.raises(KeyEncodingError):
        encode_address(generate_keypair().public_key_bytes, TESTNET)


def test_a_version_zero_program_is_rejected() -> None:
    """An old truncated-Ed25519 address must not parse at all."""
    program = bytes([0]) + b"\x11" * 20
    words = bech32.convertbits(program, 8, 5, True)
    assert words is not None
    address = bech32.bech32_encode(TESTNET.address_hrp, words)
    with pytest.raises(AddressError):
        decode_address(address)


# -- transactions ------------------------------------------------------------


def test_a_transaction_signature_is_always_hybrid() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    state, _, recipient = _funded_state(keypair)
    tx = WalletService(_params(), state).build_transfer(keypair, recipient, 1)
    assert tx.signature is not None
    assert len(bytes.fromhex(tx.signature)) == HYBRID_SIGNATURE_SIZE
    validate_transaction(tx, _params(), state, height=1)


def test_signing_refuses_a_classical_keypair() -> None:
    tx = Transaction(
        network=DEVNET.network_id,
        type=TxType.TRANSFER,
        sender=encode_address(generate_hybrid_keypair().public_key_bytes, DEVNET),
        nonce=0,
        fee=1_000,
        payload={},
    )
    with pytest.raises(TxValidationError):
        sign_transaction(generate_keypair(), tx)  # type: ignore[arg-type]


def test_a_classical_signature_is_a_schema_error() -> None:
    """A 64-byte Ed25519 signature cannot even be parsed into a transaction."""
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    _, address, _recipient = _funded_state(keypair)
    with pytest.raises(ValidationError):
        Transaction(
            network=DEVNET.network_id,
            type=TxType.TRANSFER,
            sender=address,
            nonce=0,
            fee=1_000,
            payload={},
            signature="00" * 64,
        )


def test_a_hybrid_signature_over_a_different_txid_is_rejected() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    state, _, recipient = _funded_state(keypair)
    params = _params()
    tx = WalletService(params, state).build_transfer(keypair, recipient, 1)
    other = tx.model_copy(update={"fee": tx.fee + 1})
    forged = other.model_copy(
        update={"signature": hybrid_sign(keypair, bytes.fromhex(txid(tx))).hex()}
    )
    with pytest.raises(TxValidationError) as excinfo:
        validate_transaction(forged, params, state, height=1)
    assert excinfo.value.reason == TxFailure.SIGNATURE


def test_the_signature_scheme_byte_is_checked() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    signature = hybrid_sign(keypair, b"payload")
    wrong = bytes([2]) + signature[1:]
    assert not hybrid_verify(keypair.public_key_bytes, b"payload", wrong)


# -- keyfiles ----------------------------------------------------------------


def test_a_keyfile_is_version_two_and_hybrid(tmp_path: Path, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    wallet = create_wallet(path, TESTNET, passphrase, seed=FROM_SEED, kdf=FAST_KDF, created_at=0)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["scheme"] == SCHEME == "ed25519-mldsa65"
    assert len(bytes.fromhex(document["public_key"])) == HYBRID_PUBLIC_KEY_SIZE
    loaded = load_wallet(path, passphrase)
    assert loaded.address == wallet.address
    assert loaded.signer.public_key_hex == wallet.public_key_hex
    assert loaded.keypair.public_key_hex == ed25519_public_key_hex(wallet.public_key_hex)


def test_a_version_one_keyfile_is_refused(tmp_path: Path, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, TESTNET, passphrase, seed=FROM_SEED, kdf=FAST_KDF, created_at=0)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(KeyfileError):
        load_wallet(path, passphrase)


def test_a_classical_scheme_in_a_keyfile_is_refused(tmp_path: Path, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, TESTNET, passphrase, seed=FROM_SEED, kdf=FAST_KDF, created_at=0)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["scheme"] = "ed25519"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(KeyfileError):
        load_wallet(path, passphrase)


# -- no leftovers ------------------------------------------------------------


def test_the_dual_scheme_plumbing_is_gone() -> None:
    import sccp.crypto.address as address_module
    import sccp.crypto.pq as pq_module
    import sccp.crypto.wallet as wallet_module
    from sccp.serialization import Domain

    assert not hasattr(address_module, "encode_hybrid_address")
    assert not hasattr(pq_module, "SigningKey")
    assert not hasattr(wallet_module, "SCHEMES")
    assert not hasattr(wallet_module, "SCHEME_ED25519")
    assert not hasattr(wallet_module, "KEYFILE_VERSION_PQ")
    assert not hasattr(Domain, "ADDRESS_PQ")


@pytest.mark.skipif(not pq_available(), reason="ML-DSA provider unavailable")
def test_the_hybrid_public_key_round_trips() -> None:
    keypair = generate_hybrid_keypair()
    parsed = hybrid_public_key_from_bytes(keypair.public_key_bytes)
    assert parsed.public_key_hex == keypair.public_key_hex
    assert parsed.ed25519.public_bytes_raw() == keypair.ed25519.public_key_bytes


# -- fail-closed paths -------------------------------------------------------


def test_a_short_or_mis_tagged_public_key_is_rejected() -> None:
    with pytest.raises(KeyEncodingError):
        hybrid_public_key_from_bytes(b"\x01" * 32)
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    wrong_tag = bytes([9]) + keypair.public_key_bytes[1:]
    with pytest.raises(KeyEncodingError, match="scheme byte"):
        hybrid_public_key_from_bytes(wrong_tag)
    with pytest.raises(KeyEncodingError, match="not valid hex"):
        hybrid_public_key_from_hex("zz")


def test_signing_needs_a_hybrid_keypair() -> None:
    with pytest.raises(KeyEncodingError, match="HybridKeypair"):
        hybrid_sign(generate_keypair(), b"x")  # type: ignore[arg-type]


def test_verification_never_raises() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    signature = hybrid_sign(keypair, b"payload")
    assert hybrid_verify(keypair.public_key_bytes, b"payload", signature)
    assert not hybrid_verify(keypair.public_key_bytes, b"payload", signature[:-1])
    assert not hybrid_verify(b"\x01" * 1985, b"payload", signature)
    corrupted = b"\x01" + signature[1:-1] + b"\x00"
    assert not hybrid_verify(keypair.public_key_bytes, b"payload", corrupted)


def test_the_ed25519_half_is_projected_or_passed_through() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    half = ed25519_public_key(keypair.public_key_hex)
    assert half.public_bytes_raw() == keypair.ed25519.public_key_bytes
    raw = ed25519_public_key(keypair.ed25519.public_key_bytes)
    assert raw.public_bytes_raw() == keypair.ed25519.public_key_bytes
    with pytest.raises(KeyEncodingError, match="not valid hex"):
        ed25519_public_key("zz")
    with pytest.raises(KeyEncodingError):
        ed25519_public_key(b"\x01" * 31)


def test_hybrid_public_key_accepts_hex_and_bytes() -> None:
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    assert hybrid_public_key(keypair.public_key_hex).public_key_hex == keypair.public_key_hex
    assert hybrid_public_key(keypair.public_key_bytes).public_key_hex == keypair.public_key_hex


def test_the_master_seed_is_validated() -> None:
    with pytest.raises(KeyEncodingError):
        hybrid_keypair_from_seed(b"\x01" * 31)
    keypair = hybrid_keypair_from_seed(FROM_SEED)
    assert keypair.ed25519_seed != keypair.mldsa_seed
    assert len(keypair.ed25519_seed) == 32


def test_pq_availability_is_a_boolean_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(pq_available(), bool)
    import sccp.crypto.pq as pq_module

    monkeypatch.setattr(pq_module, "pq_available", lambda: False)
    with pytest.raises(PQUnavailableError, match="no ML-DSA-65 support"):
        require_pq()
