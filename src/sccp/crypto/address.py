"""Bech32 addresses: ``sccp1...`` on mainnet, ``sccpt1...`` on testnet/devnet.

The protocol has exactly one account type: hybrid Ed25519 + ML-DSA-65. An
address is a bech32 program with a one-byte version and a 32-byte hash::

    program = version(1) || sha256("sccp/v1/address",
                                   {"algorithm", "network", "public_key"})

The hash is the *complete* SHA-256 digest rather than a truncated 20 bytes: an
address is the only thing between a quantum adversary and someone else's balance
once Grover halves the preimage margin, so it keeps 128-bit preimage security.
The version byte makes the scheme explicit, so a future scheme is a new version
rather than a guess.

The segwit-style helpers in the ``bech32`` package are not usable here: they
hard-code witness program lengths and reject the programs this protocol needs,
so we use the primitives ``bech32_encode``/``bech32_decode``/``convertbits``
directly (docs/PROTOCOL.md section 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import bech32

from sccp.config import NetworkParams, get_network
from sccp.crypto.pq import HYBRID_ALGORITHM, HYBRID_PUBLIC_KEY_SIZE
from sccp.errors import AddressError, KeyEncodingError
from sccp.serialization import Domain, domain_hash

ADDRESS_VERSION: Final = 1
"""Program version byte. Version ``0`` (truncated Ed25519 addresses) is retired."""

HASH_SIZE: Final = 32
"""Address hash length in bytes (the complete SHA-256 digest)."""

PROGRAM_SIZE: Final = 1 + HASH_SIZE
"""Version byte plus the address hash: 33 bytes."""

MAX_ADDRESS_LENGTH: Final = 90
"""BIP-173 limit, in characters."""


@dataclass(frozen=True, slots=True)
class DecodedAddress:
    """A syntactically valid bech32 address, independent of network choice."""

    hrp: str
    version: int
    payload: bytes

    @property
    def program(self) -> bytes:
        """The ``version || hash`` program (21 bytes for v0, 33 bytes for v1)."""
        return bytes([self.version]) + self.payload

    @property
    def hash_hex(self) -> str:
        return self.payload.hex()


def address_hash(network_id: str, public_key: bytes | bytearray) -> bytes:
    """The 32-byte address hash for a hybrid public key blob on ``network_id``."""
    raw = bytes(public_key)
    if len(raw) != HYBRID_PUBLIC_KEY_SIZE:
        raise KeyEncodingError(
            f"hybrid public key must be {HYBRID_PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    return domain_hash(
        Domain.ADDRESS,
        {
            "algorithm": HYBRID_ALGORITHM,
            "network": network_id,
            "public_key": raw.hex(),
        },
    )


def address_program(network_id: str, public_key: bytes | bytearray) -> bytes:
    """The 33-byte program committed to by the bech32 string."""
    return bytes([ADDRESS_VERSION]) + address_hash(network_id, public_key)


def encode_address(public_key: bytes | bytearray, network: str | NetworkParams) -> str:
    """Encode a hybrid public key blob as a lowercase bech32 address on ``network``."""
    params = get_network(network)
    program = address_program(params.network_id, public_key)
    words = bech32.convertbits(program, 8, 5, True)
    if words is None:  # pragma: no cover - impossible for 33 bytes
        raise AddressError("cannot convert address program to 5-bit words")
    return bech32.bech32_encode(params.address_hrp, words)


def _decode_words(address: str) -> tuple[str, list[int]]:
    if not isinstance(address, str):
        raise AddressError(f"address must be a string, got {type(address).__name__}")
    if not address:
        raise AddressError("address is empty")
    if len(address) > MAX_ADDRESS_LENGTH:
        raise AddressError(f"address exceeds {MAX_ADDRESS_LENGTH} characters")
    if address.lower() != address and address.upper() != address:
        raise AddressError("address mixes upper and lower case")
    hrp, words = bech32.bech32_decode(address)
    if hrp is None or words is None:
        raise AddressError(f"invalid bech32 address: {address!r}")
    return hrp, words


def decode_address(address: str) -> DecodedAddress:
    """Parse any well-formed sccp address, regardless of network."""
    hrp, words = _decode_words(address)
    program = bech32.convertbits(words, 5, 8, False)
    if program is None:
        raise AddressError(f"invalid bech32 payload in address: {address!r}")
    if not program:
        raise AddressError("address program is empty")
    version = program[0]
    if version != ADDRESS_VERSION:
        raise AddressError(f"unsupported address version {version}")
    if len(program) != PROGRAM_SIZE:
        raise AddressError(f"address program must be {PROGRAM_SIZE} bytes, got {len(program)}")
    return DecodedAddress(hrp=hrp, version=version, payload=bytes(program[1:]))


def address_version(address: str) -> int:
    """The scheme version byte of ``address``, raising :class:`AddressError` if malformed."""
    return decode_address(address).version


def validate_address(address: str, network: str | NetworkParams) -> bool:
    """Return whether ``address`` parses and belongs to ``network``."""
    params = get_network(network)
    try:
        decoded = decode_address(address)
    except AddressError:
        return False
    return decoded.hrp == params.address_hrp


def address_for_public_key(public_key: bytes | bytearray, network: str | NetworkParams) -> str:
    """Alias for :func:`encode_address`, spelled for readability at call sites."""
    return encode_address(public_key, network)
