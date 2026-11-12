"""Ed25519 keys, signatures and domain-separated signing.

Consensus uses raw 32-byte public keys and 64-byte signatures
(docs/IMPLEMENTATION_PLAN.md section 5.2). :func:`verify` is total: it returns
``False`` instead of raising for any malformed input, because it consumes
untrusted network data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypeAlias

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sccp.errors import KeyEncodingError
from sccp.serialization import domain_hash

SEED_SIZE: Final = 32
PUBLIC_KEY_SIZE: Final = 32
SIGNATURE_SIZE: Final = 64

PublicKeyLike: TypeAlias = Ed25519PublicKey | bytes | bytearray
"""Anything accepted by :func:`verify` as a public key."""


@dataclass(frozen=True, slots=True)
class Keypair:
    """An Ed25519 private key together with its public key."""

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @property
    def seed(self) -> bytes:
        """The 32-byte seed the private key was derived from."""
        return self.private_key.private_bytes_raw()

    @property
    def public_key_bytes(self) -> bytes:
        return self.public_key.public_bytes_raw()

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()


def generate_keypair() -> Keypair:
    """Generate a keypair from the OS CSPRNG."""
    private_key = Ed25519PrivateKey.generate()
    return Keypair(private_key=private_key, public_key=private_key.public_key())


def _check_seed(seed: bytes | bytearray) -> bytes:
    raw = bytes(seed)
    if len(raw) != SEED_SIZE:
        raise KeyEncodingError(f"Ed25519 seed must be {SEED_SIZE} bytes, got {len(raw)}")
    return raw


def keypair_from_seed(seed: bytes | bytearray) -> Keypair:
    """Deterministically build a keypair from a 32-byte seed."""
    private_key = Ed25519PrivateKey.from_private_bytes(_check_seed(seed))
    return Keypair(private_key=private_key, public_key=private_key.public_key())


def public_key_from_bytes(data: bytes | bytearray) -> Ed25519PublicKey:
    """Parse a raw 32-byte public key."""
    raw = bytes(data)
    if len(raw) != PUBLIC_KEY_SIZE:
        raise KeyEncodingError(
            f"Ed25519 public key must be {PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_from_hex(text: str) -> Ed25519PublicKey:
    """Parse a hex-encoded raw public key."""
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise KeyEncodingError(f"public key is not valid hex: {text!r}") from None
    return public_key_from_bytes(raw)


def _as_public_key(public_key: PublicKeyLike) -> Ed25519PublicKey:
    if isinstance(public_key, Ed25519PublicKey):
        return public_key
    return public_key_from_bytes(public_key)


def sign(private_key: Ed25519PrivateKey | Keypair, message: bytes | bytearray) -> bytes:
    """Sign ``message`` (raw bytes) with ``private_key``."""
    if isinstance(private_key, Keypair):
        private_key = private_key.private_key
    return private_key.sign(bytes(message))


def verify(
    public_key: PublicKeyLike,
    message: bytes | bytearray,
    signature: bytes | bytearray,
) -> bool:
    """Return whether ``signature`` is a valid signature of ``message``.

    Never raises for malformed keys or signatures: all failures return ``False``.
    """
    raw_signature = bytes(signature)
    if len(raw_signature) != SIGNATURE_SIZE:
        return False
    try:
        key = _as_public_key(public_key)
        key.verify(raw_signature, bytes(message))
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_domain(
    private_key: Ed25519PrivateKey | Keypair,
    domain: str,
    payload: object,
) -> bytes:
    """Sign ``sha256(domain + 0x00 + canonical_json(payload))``."""
    return sign(private_key, domain_hash(domain, payload))


def verify_domain(
    public_key: PublicKeyLike,
    domain: str,
    payload: object,
    signature: bytes | bytearray,
) -> bool:
    """Verify a signature produced by :func:`sign_domain`."""
    return verify(public_key, domain_hash(domain, payload), signature)
