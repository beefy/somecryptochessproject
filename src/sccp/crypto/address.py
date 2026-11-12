"""Bech32 addresses: ``sccp1...`` on mainnet, ``sccpt1...`` on testnet/devnet.

An address is a 21-byte program -- one version byte (``0`` = Ed25519) plus the
first 20 bytes of ``sha256("sccp/v1/address" + 0x00 + canonical_json({"network",
"public_key"}))`` -- encoded with BIP-173 bech32. The segwit-style helpers in the
``bech32`` package are not usable here: they hard-code witness program lengths
and reject the 21-byte program this protocol needs, so we use the primitives
``bech32_encode``/``bech32_decode``/``convertbits`` directly
(docs/PROTOCOL.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import bech32

from sccp.config import NetworkParams, get_network
from sccp.crypto.keys import PUBLIC_KEY_SIZE
from sccp.errors import AddressError, KeyEncodingError
from sccp.serialization import Domain, domain_hash

ADDRESS_VERSION: Final = 0
"""Program version byte for Ed25519 keys."""

HASH_SIZE: Final = 20
"""Address hash length in bytes."""

PROGRAM_SIZE: Final = 1 + HASH_SIZE
"""Version byte plus hash."""

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
        """The 21-byte ``version || hash`` program."""
        return bytes([self.version]) + self.payload

    @property
    def hash_hex(self) -> str:
        return self.payload.hex()


def address_hash(network_id: str, public_key: bytes | bytearray) -> bytes:
    """The 20-byte address hash for ``public_key`` on ``network_id``."""
    raw = bytes(public_key)
    if len(raw) != PUBLIC_KEY_SIZE:
        raise KeyEncodingError(
            f"Ed25519 public key must be {PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    digest = domain_hash(Domain.ADDRESS, {"network": network_id, "public_key": raw.hex()})
    return digest[:HASH_SIZE]


def address_program(network_id: str, public_key: bytes | bytearray) -> bytes:
    """The 21-byte program committed to by the bech32 string."""
    return bytes([ADDRESS_VERSION]) + address_hash(network_id, public_key)


def encode_address(public_key: bytes | bytearray, network: str | NetworkParams) -> str:
    """Encode ``public_key`` as a lowercase bech32 address on ``network``."""
    params = get_network(network)
    program = address_program(params.network_id, public_key)
    words = bech32.convertbits(program, 8, 5, True)
    if words is None:  # pragma: no cover - impossible for 21 bytes
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
    if program is None or len(program) != PROGRAM_SIZE:
        raise AddressError(f"address program must be {PROGRAM_SIZE} bytes")
    version = program[0]
    if version != ADDRESS_VERSION:
        raise AddressError(f"unsupported address version {version}")
    return DecodedAddress(hrp=hrp, version=version, payload=bytes(program[1:]))


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
