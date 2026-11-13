"""Hybrid Ed25519 + ML-DSA-65 keys and signatures (post-quantum hardening).

The classical scheme in :mod:`sccp.crypto.keys` is Ed25519, which a
cryptographically relevant quantum computer breaks with Shor's algorithm. A
hybrid keypair keeps Ed25519 and adds ML-DSA-65 (NIST FIPS 204, category 3). A
hybrid signature is valid only if *both* halves verify over the same
scheme-tagged message, so an attacker must break both schemes and a classical
signature can never be replayed as a hybrid one.

Layouts (lowercase hex when stored in a transaction or keyfile)::

    public key  = 0x01 || ed25519_pub(32) || mldsa65_pub(1952)  = 1985 bytes
    signature   = 0x01 || ed25519_sig(64) || mldsa65_sig(3309)  = 3374 bytes

The leading scheme byte makes the encoding self-describing, so the ledger can
reject a key or signature of the wrong scheme instead of guessing. Private keys
are never serialised here: Ed25519 and ML-DSA-65 both take a 32-byte seed, so a
32-byte *master seed* derives both halves with HMAC-SHA-256 domain separation.
That master seed is what a hybrid keyfile stores and a mnemonic backs up.

ML-DSA ships with ``cryptography`` 50 but is backend-gated: a wheel built against
an OpenSSL without the provider exposes no ``mldsa`` module. Key generation then
raises :class:`~sccp.errors.PQUnavailableError`; verification fails closed
(``False``), because it consumes untrusted network data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sccp.crypto.keys import PUBLIC_KEY_SIZE as ED25519_PUBLIC_KEY_SIZE
from sccp.crypto.keys import SEED_SIZE as ED25519_SEED_SIZE
from sccp.crypto.keys import SIGNATURE_SIZE as ED25519_SIGNATURE_SIZE
from sccp.crypto.keys import Keypair, keypair_from_seed, public_key_from_bytes, sign, verify
from sccp.errors import KeyEncodingError, PQUnavailableError

HYBRID_SCHEME_ID: Final = 1
"""Leading byte identifying the hybrid ``ed25519-mldsa65`` scheme."""

HYBRID_ALGORITHM: Final = "ed25519-mldsa65"
"""Algorithm name committed to by a hybrid address."""

MLDSA_VARIANT: Final = "ML-DSA-65"
"""NIST FIPS 204 parameter set (category 3)."""

MLDSA_SEED_SIZE: Final = 32
"""Raw seed length accepted by ``MLDSA65PrivateKey.from_seed_bytes``."""

MLDSA65_PUBLIC_KEY_SIZE: Final = 1952
"""Raw ML-DSA-65 public key length in bytes (FIPS 204)."""

MLDSA65_SIGNATURE_SIZE: Final = 3309
"""Raw ML-DSA-65 signature length in bytes (FIPS 204)."""

HYBRID_PUBLIC_KEY_SIZE: Final = 1 + ED25519_PUBLIC_KEY_SIZE + MLDSA65_PUBLIC_KEY_SIZE
"""``scheme || ed25519 || mldsa65`` public key blob: 1985 bytes."""

HYBRID_SIGNATURE_SIZE: Final = 1 + ED25519_SIGNATURE_SIZE + MLDSA65_SIGNATURE_SIZE
"""``scheme || ed25519 || mldsa65`` signature blob: 3374 bytes."""

SIGNATURE_CONTEXT: Final = b"sccp/v1/hybrid-ed25519-mldsa65"
"""FIPS 204 context string; binds every ML-DSA signature to this protocol."""

_MESSAGE_TAG: Final = b"sccp/v1/pq-hybrid\x00"
"""Prefix hashed into the message both halves sign (cross-scheme separation)."""

_ED25519_LABEL: Final = b"sccp/v1/pq/ed25519"
_MLDSA_LABEL: Final = b"sccp/v1/pq/mldsa65"


def _mldsa_module() -> Any:
    """Return the ``cryptography`` ML-DSA module or raise ``PQUnavailableError``."""
    try:
        from cryptography.hazmat.primitives.asymmetric import mldsa
    except ImportError as exc:  # pragma: no cover - depends on the wheel
        raise PQUnavailableError(
            "ML-DSA is unavailable: the installed cryptography backend has no "
            "post-quantum provider (cryptography>=50 built with ML-DSA is required)"
        ) from exc
    return mldsa


def pq_available() -> bool:
    """Whether this process can create and verify ML-DSA keys."""
    try:
        _mldsa_module()
    except PQUnavailableError:
        return False
    supported = getattr(backend, "mldsa_supported", None)
    if supported is None:
        return False
    try:
        return bool(supported())
    except (UnsupportedAlgorithm, ValueError):  # pragma: no cover - backend-specific
        return False


def _derive_seed(master: bytes, label: bytes) -> bytes:
    """HMAC-SHA-256 domain-separated sub-seed for one half of the hybrid key."""
    return hmac.new(master, label, hashlib.sha256).digest()


def _check_master_seed(seed: bytes | bytearray) -> bytes:
    raw = bytes(seed)
    if len(raw) != ED25519_SEED_SIZE:
        raise KeyEncodingError(
            f"hybrid master seed must be {ED25519_SEED_SIZE} bytes, got {len(raw)}"
        )
    return raw


@dataclass(frozen=True, slots=True)
class HybridKeypair:
    """An Ed25519 keypair, an ML-DSA-65 private key, and the master seed."""

    ed25519: Keypair
    mldsa_private: Any
    seed: bytes
    """The 32-byte master seed both halves are derived from."""

    def __post_init__(self) -> None:
        _check_master_seed(self.seed)

    @property
    def ed25519_seed(self) -> bytes:
        """The Ed25519 sub-seed derived from the master seed."""
        return _derive_seed(self.seed, _ED25519_LABEL)

    @property
    def mldsa_seed(self) -> bytes:
        """The ML-DSA-65 sub-seed derived from the master seed."""
        return _derive_seed(self.seed, _MLDSA_LABEL)

    @property
    def mldsa_public_key_bytes(self) -> bytes:
        return bytes(self.mldsa_private.public_key().public_bytes_raw())

    @property
    def public_key_bytes(self) -> bytes:
        return (
            bytes([HYBRID_SCHEME_ID]) + self.ed25519.public_key_bytes + self.mldsa_public_key_bytes
        )

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()


@dataclass(frozen=True, slots=True)
class HybridPublicKey:
    """A parsed hybrid public key: one Ed25519 and one ML-DSA-65 public key."""

    ed25519: Ed25519PublicKey
    mldsa: Any

    @property
    def public_key_bytes(self) -> bytes:
        return (
            bytes([HYBRID_SCHEME_ID])
            + self.ed25519.public_bytes_raw()
            + bytes(self.mldsa.public_bytes_raw())
        )

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()


def generate_hybrid_keypair() -> HybridKeypair:
    """Generate a hybrid keypair from the OS CSPRNG."""
    return hybrid_keypair_from_seed(secrets.token_bytes(ED25519_SEED_SIZE))


def hybrid_keypair_from_seed(seed: bytes | bytearray) -> HybridKeypair:
    """Deterministically derive both halves of a hybrid keypair from a master seed."""
    master = _check_master_seed(seed)
    module = _mldsa_module()
    mldsa_private = module.MLDSA65PrivateKey.from_seed_bytes(_derive_seed(master, _MLDSA_LABEL))
    return HybridKeypair(
        ed25519=keypair_from_seed(_derive_seed(master, _ED25519_LABEL)),
        mldsa_private=mldsa_private,
        seed=master,
    )


def hybrid_public_key_from_bytes(data: bytes | bytearray) -> HybridPublicKey:
    """Parse a raw ``scheme || ed25519 || mldsa65`` public key blob."""
    raw = bytes(data)
    if len(raw) != HYBRID_PUBLIC_KEY_SIZE:
        raise KeyEncodingError(
            f"hybrid public key must be {HYBRID_PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    if raw[0] != HYBRID_SCHEME_ID:
        raise KeyEncodingError(f"unsupported hybrid key scheme byte {raw[0]}")
    module = _mldsa_module()
    offset = 1
    ed25519_raw = raw[offset : offset + ED25519_PUBLIC_KEY_SIZE]
    mldsa_raw = raw[offset + ED25519_PUBLIC_KEY_SIZE :]
    return HybridPublicKey(
        ed25519=public_key_from_bytes(ed25519_raw),
        mldsa=module.MLDSA65PublicKey.from_public_bytes(mldsa_raw),
    )


def hybrid_public_key_from_hex(text: str) -> HybridPublicKey:
    """Parse a hex-encoded hybrid public key blob."""
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise KeyEncodingError(f"hybrid public key is not valid hex: {text!r}") from None
    return hybrid_public_key_from_bytes(raw)


def _tagged(message: bytes | bytearray) -> bytes:
    return _MESSAGE_TAG + bytes(message)


def hybrid_sign(keypair: HybridKeypair, message: bytes | bytearray) -> bytes:
    """Sign ``message`` with both halves, returning a hybrid signature blob."""
    if not isinstance(keypair, HybridKeypair):
        raise KeyEncodingError("hybrid signing needs a HybridKeypair")
    tagged = _tagged(message)
    ed25519_signature = sign(keypair.ed25519, tagged)
    mldsa_signature = bytes(keypair.mldsa_private.sign(tagged, SIGNATURE_CONTEXT))
    if len(mldsa_signature) != MLDSA65_SIGNATURE_SIZE:  # pragma: no cover - FIPS 204 fixed
        raise KeyEncodingError(
            f"ML-DSA-65 produced a {len(mldsa_signature)}-byte signature, "
            f"expected {MLDSA65_SIGNATURE_SIZE}"
        )
    return bytes([HYBRID_SCHEME_ID]) + ed25519_signature + mldsa_signature


def hybrid_verify(
    public_key: HybridPublicKey | bytes | bytearray,
    message: bytes | bytearray,
    signature: bytes | bytearray,
) -> bool:
    """Return whether *both* halves of ``signature`` verify over ``message``.

    Never raises for malformed keys or signatures (including a backend without
    ML-DSA): every failure returns ``False``.
    """
    raw_signature = bytes(signature)
    if len(raw_signature) != HYBRID_SIGNATURE_SIZE:
        return False
    if raw_signature[0] != HYBRID_SCHEME_ID:
        return False
    try:
        key = (
            public_key
            if isinstance(public_key, HybridPublicKey)
            else hybrid_public_key_from_bytes(public_key)
        )
    except (KeyEncodingError, PQUnavailableError, ValueError):
        return False
    tagged = _tagged(message)
    split = 1 + ED25519_SIGNATURE_SIZE
    if not verify(key.ed25519, tagged, raw_signature[1:split]):
        return False
    try:
        key.mldsa.verify(raw_signature[split:], tagged, SIGNATURE_CONTEXT)
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        return False
    return True


def _is_lower_hex(text: str) -> bool:
    if text != text.lower():
        return False
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True


def is_hybrid_public_key_hex(value: object) -> bool:
    """Whether ``value`` is a lowercase hex hybrid public key blob."""
    return (
        isinstance(value, str) and len(value) == HYBRID_PUBLIC_KEY_SIZE * 2 and _is_lower_hex(value)
    )


SigningKey: TypeAlias = Keypair | HybridKeypair
"""Anything that can sign a ledger transaction: Ed25519 or hybrid."""


def ed25519_public_key_hex(value: str) -> str:
    """The Ed25519 half of a stored public key, as raw 32-byte hex.

    The ledger stores whichever key its owner registered: a raw Ed25519 key
    (64 hex characters) or a hybrid blob (3970 hex characters). Every
    Ed25519-only verifier -- match transcripts, clock receipts -- needs the
    classical half, and for a hybrid account that is the 32 bytes right after
    the scheme byte. Anything that is not a hybrid blob is returned unchanged,
    so a malformed key still fails closed inside the verifier rather than being
    silently rewritten here.
    """
    if is_hybrid_public_key_hex(value):
        return value[2 : 2 + ED25519_PUBLIC_KEY_SIZE * 2]
    return value


__all__ = [
    "HYBRID_ALGORITHM",
    "HYBRID_PUBLIC_KEY_SIZE",
    "HYBRID_SCHEME_ID",
    "HYBRID_SIGNATURE_SIZE",
    "MLDSA65_PUBLIC_KEY_SIZE",
    "MLDSA65_SIGNATURE_SIZE",
    "MLDSA_SEED_SIZE",
    "MLDSA_VARIANT",
    "SIGNATURE_CONTEXT",
    "HybridKeypair",
    "HybridPublicKey",
    "SigningKey",
    "ed25519_public_key_hex",
    "generate_hybrid_keypair",
    "hybrid_keypair_from_seed",
    "hybrid_public_key_from_bytes",
    "hybrid_public_key_from_hex",
    "hybrid_sign",
    "hybrid_verify",
    "is_hybrid_public_key_hex",
    "pq_available",
]
