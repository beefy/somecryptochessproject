"""Wallet keyfiles: scrypt + AES-256-GCM, plus BIP-39 mnemonic backup.

Keyfile layout (docs/PROTOCOL.md section 4)::

    {
        "version": 2,
        "scheme": "ed25519-mldsa65",
        "network": "testnet",
        "address": "sccpt1...",
        "public_key": "<3970 hex>",
        "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": "<32 hex>"},
        "cipher": {"name": "aes-256-gcm", "nonce": "<24 hex>"},
        "created_at": 1760000000,
        "ciphertext": "<96 hex>",
    }

The associated data for AES-GCM is the canonical JSON of every field except
``ciphertext``, so tampering with the network, address or KDF parameters fails
authentication rather than silently decrypting to a different wallet.

There is exactly one keyfile version and one scheme: ``version 2`` holding a
*hybrid* Ed25519 + ML-DSA-65 key (:mod:`sccp.crypto.pq`). The stored
secret is a 32-byte master seed from which both halves are derived. Version 1
(Ed25519-only) keyfiles are rejected: an account that a quantum computer can
impersonate is not a valid account, and silently upgrading one would be a
downgrade attack waiting to happen.

The 24-word mnemonic is a *lossless backup encoding of the 32-byte master seed*,
not a BIP-32 derivation path: restoring a phrase restores exactly one key (both
halves), and there is no child-key tree.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from mnemonic import Mnemonic

from sccp.config import NETWORKS, NetworkParams, get_network
from sccp.crypto.address import decode_address, encode_address, validate_address
from sccp.crypto.keys import SEED_SIZE, Keypair
from sccp.crypto.pq import (
    HYBRID_ALGORITHM,
    HYBRID_PUBLIC_KEY_SIZE,
    HybridKeypair,
    generate_hybrid_keypair,
    hybrid_keypair_from_seed,
)
from sccp.errors import (
    AddressError,
    KeyEncodingError,
    KeyfileError,
    PassphraseError,
)
from sccp.serialization import canonical_json

KEYFILE_VERSION: Final = 2
"""The only keyfile version; version 1 (Ed25519-only) is retired."""

SCHEME: Final = HYBRID_ALGORITHM
"""The only keyfile scheme: hybrid Ed25519 + ML-DSA-65."""

KDF_NAME: Final = "scrypt"
CIPHER_NAME: Final = "aes-256-gcm"
KEY_SIZE: Final = 32
SCRYPT_N: Final = 2**15
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_SALT_SIZE: Final = 16
AES_NONCE_SIZE: Final = 12
AES_TAG_SIZE: Final = 16
MNEMONIC_WORD_COUNT: Final = 24


@dataclass(frozen=True, slots=True)
class KdfParams:
    """scrypt cost parameters stored in the keyfile header."""

    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    def __post_init__(self) -> None:
        if self.n < 2 or self.n & (self.n - 1):
            raise KeyfileError("scrypt n must be a power of two >= 2")
        if self.r < 1 or self.p < 1:
            raise KeyfileError("scrypt r and p must be positive")

    def to_dict(self) -> dict[str, object]:
        return {"name": KDF_NAME, "n": self.n, "r": self.r, "p": self.p}


DEFAULT_KDF: Final = KdfParams(n=2**17)
"""Default scrypt cost: 128 MiB of memory per guess.

A key that survives a quantum computer is a long-lived target of a
harvest-now-decrypt-later attack, so the one profile is the expensive one.
"""

FAST_KDF: Final = KdfParams(n=2**10)
"""Cheap parameters for unit tests only; never write production keyfiles with it."""

MIN_PASSPHRASE_CHARS: Final = 12
"""Minimum passphrase length for a new keyfile.

Grover's algorithm halves the effective search space of an offline guessing
attack, so the profile asks for more margin than a classical keyfile would. The
check is a usability floor, not an entropy proof: prefer a long random phrase.
"""


def normalize_passphrase(passphrase: str) -> bytes:
    """NFKD-normalize and UTF-8 encode a passphrase (BIP-39 convention)."""
    return unicodedata.normalize("NFKD", passphrase).encode("utf-8")


def derive_key(passphrase: str, salt: bytes, params: KdfParams = DEFAULT_KDF) -> bytes:
    """Derive the 32-byte AES key with scrypt.

    ``cryptography`` 50 is a Rust implementation with no ``maxmem`` knob; the
    cost is bounded by the header parameters, which the loader validates.
    """
    if len(salt) != SCRYPT_SALT_SIZE:
        raise KeyfileError(f"scrypt salt must be {SCRYPT_SALT_SIZE} bytes")
    kdf = Scrypt(
        salt=salt,
        length=KEY_SIZE,
        n=params.n,
        r=params.r,
        p=params.p,
    )
    return kdf.derive(normalize_passphrase(passphrase))


def _require(document: Mapping[str, object], key: str) -> object:
    try:
        return document[key]
    except KeyError:
        raise KeyfileError(f"keyfile is missing field {key!r}") from None


def _require_str(document: Mapping[str, object], key: str) -> str:
    value = _require(document, key)
    if not isinstance(value, str):
        raise KeyfileError(f"keyfile field {key!r} must be a string")
    return value


def _require_int(document: Mapping[str, object], key: str) -> int:
    value = _require(document, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise KeyfileError(f"keyfile field {key!r} must be an integer")
    return value


def _require_mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = _require(document, key)
    if not isinstance(value, Mapping):
        raise KeyfileError(f"keyfile field {key!r} must be an object")
    return cast("Mapping[str, object]", value)


def _require_hex(document: Mapping[str, object], key: str, size: int) -> bytes:
    text = _require_str(document, key)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise KeyfileError(f"keyfile field {key!r} is not valid hex") from None
    if len(raw) != size:
        raise KeyfileError(f"keyfile field {key!r} must be {size} bytes, got {len(raw)}")
    return raw


@dataclass(frozen=True, slots=True)
class KeyfileHeader:
    """Every keyfile field except ``ciphertext``; also the AEAD associated data."""

    version: int
    network: str
    address: str
    public_key: str
    kdf: KdfParams
    salt: bytes
    nonce: bytes
    created_at: int
    scheme: str = SCHEME

    def as_dict(self) -> dict[str, object]:
        """The canonical header mapping (names and ordering fixed by the spec).

        ``scheme`` is always written: a keyfile states its algorithm rather than
        leaving a reader to infer one from a key length.
        """
        kdf_dict = self.kdf.to_dict()
        kdf_dict["salt"] = self.salt.hex()
        return {
            "version": self.version,
            "scheme": self.scheme,
            "network": self.network,
            "address": self.address,
            "public_key": self.public_key,
            "kdf": kdf_dict,
            "cipher": {"name": CIPHER_NAME, "nonce": self.nonce.hex()},
            "created_at": self.created_at,
        }

    @property
    def associated_data(self) -> bytes:
        """AES-GCM associated data: canonical JSON of the header."""
        return canonical_json(self.as_dict())

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> KeyfileHeader:
        """Parse and validate a header, raising :class:`KeyfileError` on any defect."""
        version = _require_int(document, "version")
        if version != KEYFILE_VERSION:
            raise KeyfileError(f"unsupported keyfile version {version}")
        scheme_value = _require_str(document, "scheme")
        if scheme_value != SCHEME:
            raise KeyfileError(f"unsupported keyfile scheme {scheme_value!r} (only {SCHEME!r})")
        scheme = scheme_value
        network = _require_str(document, "network")
        if network not in NETWORKS:
            known = ", ".join(sorted(NETWORKS))
            raise KeyfileError(f"keyfile names unknown network {network!r} (known: {known})")
        address = _require_str(document, "address")
        if not validate_address(address, network):
            raise KeyfileError(f"keyfile address {address!r} is not valid on {network}")
        try:
            decoded = decode_address(address)
        except AddressError as exc:  # pragma: no cover - validate_address already parsed it
            raise KeyfileError(f"keyfile address {address!r} is malformed: {exc}") from exc
        if decoded.version != 1:  # pragma: no cover - decode_address rejects others
            raise KeyfileError(
                f"keyfile scheme {scheme!r} needs a version-1 address, not {decoded.version}"
            )
        public_key = _require_str(document, "public_key")
        try:
            public_key_bytes = bytes.fromhex(public_key)
        except ValueError:
            raise KeyfileError("keyfile field 'public_key' is not valid hex") from None
        if len(public_key_bytes) != HYBRID_PUBLIC_KEY_SIZE:
            raise KeyfileError(
                f"keyfile field 'public_key' must be {HYBRID_PUBLIC_KEY_SIZE} bytes "
                f"for scheme {scheme!r}"
            )

        kdf_document = _require_mapping(document, "kdf")
        kdf_name = _require_str(kdf_document, "name")
        if kdf_name != KDF_NAME:
            raise KeyfileError(f"unsupported keyfile kdf {kdf_name!r}")
        kdf = KdfParams(
            n=_require_int(kdf_document, "n"),
            r=_require_int(kdf_document, "r"),
            p=_require_int(kdf_document, "p"),
        )
        salt = _require_hex(kdf_document, "salt", SCRYPT_SALT_SIZE)

        cipher_document = _require_mapping(document, "cipher")
        cipher_name = _require_str(cipher_document, "name")
        if cipher_name != CIPHER_NAME:
            raise KeyfileError(f"unsupported keyfile cipher {cipher_name!r}")
        nonce = _require_hex(cipher_document, "nonce", AES_NONCE_SIZE)

        return cls(
            version=version,
            network=network,
            address=address,
            public_key=public_key,
            kdf=kdf,
            salt=salt,
            nonce=nonce,
            created_at=_require_int(document, "created_at"),
            scheme=scheme,
        )


@dataclass(frozen=True, slots=True)
class Wallet:
    """A decrypted hybrid keypair together with the keyfile header it came from."""

    header: KeyfileHeader
    pq_keypair: HybridKeypair
    path: Path | None = None

    @property
    def keypair(self) -> Keypair:
        """The Ed25519 half: what the move, receipt and gossip streams sign with.

        Naming it separately keeps every call site honest about which key it is
        holding. Ledger and identity signatures take :attr:`signer` instead.
        """
        return self.pq_keypair.ed25519

    @property
    def signer(self) -> HybridKeypair:
        """The hybrid keypair: what transactions, certificates and results sign with."""
        return self.pq_keypair

    @property
    def seed(self) -> bytes:
        """The 32-byte master seed a mnemonic backs up."""
        return self.pq_keypair.seed

    @property
    def scheme(self) -> str:
        return self.header.scheme

    @property
    def address(self) -> str:
        return self.header.address

    @property
    def network(self) -> str:
        return self.header.network

    @property
    def public_key_hex(self) -> str:
        return self.header.public_key

    @property
    def network_params(self) -> NetworkParams:
        return get_network(self.header.network)


def create_wallet(
    path: str | Path,
    network: str | NetworkParams,
    passphrase: str,
    *,
    seed: bytes | None = None,
    kdf: KdfParams | None = None,
    created_at: int | None = None,
    force: bool = False,
) -> Wallet:
    """Write a new hybrid keyfile and return the decrypted wallet.

    ``seed`` exists for deterministic tests and for restoring from a mnemonic;
    when omitted a fresh 32-byte master seed is generated from the OS CSPRNG and
    both key halves are derived from it. There is no scheme argument: the
    protocol has one.
    """
    params = get_network(network)
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise PassphraseError(
            f"a keyfile passphrase must be at least {MIN_PASSPHRASE_CHARS} characters"
        )
    pq_keypair = hybrid_keypair_from_seed(seed) if seed is not None else generate_hybrid_keypair()
    header = KeyfileHeader(
        version=KEYFILE_VERSION,
        scheme=SCHEME,
        network=params.network_id,
        address=encode_address(pq_keypair.public_key_bytes, params),
        public_key=pq_keypair.public_key_hex,
        kdf=DEFAULT_KDF if kdf is None else kdf,
        salt=secrets.token_bytes(SCRYPT_SALT_SIZE),
        nonce=secrets.token_bytes(AES_NONCE_SIZE),
        created_at=int(time.time()) if created_at is None else created_at,
    )
    key = derive_key(passphrase, header.salt, header.kdf)
    ciphertext = AESGCM(key).encrypt(header.nonce, pq_keypair.seed, header.associated_data)

    document = header.as_dict()
    document["ciphertext"] = ciphertext.hex()
    target = Path(path)
    _write_document(target, document, force=force)
    return Wallet(header=header, pq_keypair=pq_keypair, path=target)


def read_header(path: str | Path) -> KeyfileHeader:
    """Read and validate the public part of a keyfile (no passphrase needed)."""
    return KeyfileHeader.from_dict(_read_document(Path(path)))


def decrypt_seed(header: KeyfileHeader, ciphertext: bytes, passphrase: str) -> bytes:
    """Decrypt the stored seed, mapping authentication failure to ``PassphraseError``."""
    key = derive_key(passphrase, header.salt, header.kdf)
    try:
        return AESGCM(key).decrypt(header.nonce, ciphertext, header.associated_data)
    except InvalidTag:
        raise PassphraseError(
            f"cannot decrypt keyfile for {header.address}: wrong passphrase or corrupted ciphertext"
        ) from None


@cache
def _english_mnemonic() -> Mnemonic:
    return Mnemonic("english")


def seed_to_mnemonic(seed: bytes | bytearray) -> str:
    """Encode a 32-byte master seed as 24 BIP-39 words (backup, not derivation)."""
    raw = bytes(seed)
    if len(raw) != SEED_SIZE:
        raise KeyEncodingError(f"seed must be {SEED_SIZE} bytes, got {len(raw)}")
    return _english_mnemonic().to_mnemonic(raw)


def mnemonic_to_seed(phrase: str) -> bytes:
    """Decode a 24-word BIP-39 phrase back into the 32-byte master seed."""
    words = phrase.split()
    if len(words) != MNEMONIC_WORD_COUNT:
        raise KeyEncodingError(f"expected {MNEMONIC_WORD_COUNT} words, got {len(words)}")
    try:
        entropy = _english_mnemonic().to_entropy(words)
    except (KeyError, ValueError) as exc:
        raise KeyEncodingError(f"invalid mnemonic: {exc}") from None
    if len(entropy) != SEED_SIZE:  # pragma: no cover - 24 words always yield 32 bytes
        raise KeyEncodingError(f"mnemonic decoded to {len(entropy)} bytes, expected {SEED_SIZE}")
    return bytes(entropy)


def export_mnemonic(path: str | Path, passphrase: str) -> str:
    """Decrypt a keyfile and return its 24-word backup phrase.

    For a hybrid keyfile the phrase encodes the master seed, so restoring it
    reproduces both the Ed25519 and the ML-DSA-65 halves.
    """
    wallet = load_wallet(path, passphrase)
    return seed_to_mnemonic(wallet.seed)


def import_mnemonic(
    path: str | Path,
    phrase: str,
    network: str | NetworkParams,
    passphrase: str,
    *,
    kdf: KdfParams | None = None,
    created_at: int | None = None,
    force: bool = False,
) -> Wallet:
    """Recreate a keyfile from a 24-word phrase.

    The phrase encodes the master seed, so both halves of the hybrid key come
    back byte for byte.
    """
    return create_wallet(
        path,
        network,
        passphrase,
        seed=mnemonic_to_seed(phrase),
        kdf=kdf,
        created_at=created_at,
        force=force,
    )


def read_passphrase_file(path: str | Path) -> str:
    """Read a passphrase from a mode-checked file (not the keyfile itself).

    Group/other permission bits are rejected because a world-readable
    passphrase file defeats the purpose of encrypting the keyfile.
    """
    target = Path(path)
    try:
        status = target.stat()
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PassphraseError(f"cannot read passphrase file {target}: {exc}") from exc
    if status.st_mode & 0o077:
        raise PassphraseError(
            f"passphrase file {target} is accessible by group or others; chmod 600 it"
        )
    passphrase = text.rstrip("\r\n")
    if not passphrase:
        raise PassphraseError(f"passphrase file {target} is empty")
    return passphrase


def _read_document(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyfileError(f"keyfile not found: {path}") from None
    except OSError as exc:
        raise KeyfileError(f"cannot read keyfile {path}: {exc}") from exc
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KeyfileError(f"keyfile {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise KeyfileError(f"keyfile {path} must contain a JSON object")
    document: dict[str, object] = parsed
    return document


def _write_all(fd: int, payload: bytes) -> None:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_document(path: Path, document: Mapping[str, object], *, force: bool) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    if force:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, payload)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)
        return

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise KeyfileError(
            f"keyfile already exists: {path} (pass force=True to overwrite)"
        ) from None
    except OSError as exc:
        raise KeyfileError(f"cannot write keyfile {path}: {exc}") from exc
    _write_all(fd, payload)


def load_wallet(path: str | Path, passphrase: str) -> Wallet:
    """Open a keyfile and verify that the decrypted seed matches its header."""
    target = Path(path)
    document = _read_document(target)
    header = KeyfileHeader.from_dict(document)
    ciphertext = _require_hex(document, "ciphertext", SEED_SIZE + AES_TAG_SIZE)
    seed = decrypt_seed(header, ciphertext, passphrase)
    pq_keypair = hybrid_keypair_from_seed(seed)
    if pq_keypair.public_key_hex != header.public_key:
        raise KeyfileError("decrypted seed does not match the keyfile public key")
    if encode_address(pq_keypair.public_key_bytes, header.network) != header.address:
        raise KeyfileError("decrypted seed does not match the keyfile address")
    return Wallet(header=header, pq_keypair=pq_keypair, path=target)
