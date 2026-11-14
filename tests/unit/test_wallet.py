"""Unit tests for encrypted keyfiles and mnemonic backup."""

from __future__ import annotations

import copy
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from sccp.config import get_network
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.crypto.wallet import (
    DEFAULT_KDF,
    KEYFILE_VERSION,
    MNEMONIC_WORD_COUNT,
    KdfParams,
    create_wallet,
    derive_key,
    export_mnemonic,
    import_mnemonic,
    load_wallet,
    mnemonic_to_seed,
    read_header,
    read_mnemonic_file,
    read_passphrase_file,
    seed_to_mnemonic,
)
from sccp.errors import KeyEncodingError, KeyfileError, PassphraseError

Vectors = dict[str, Any]
OTHER_SEED = bytes([7]) * 32


def _seed(vectors: Vectors) -> bytes:
    return bytes.fromhex(vectors["seed_hex"])


def _document(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _rewrite(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_create_and_load(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str, vectors: Vectors
) -> None:
    path = tmp_path / "wallet.key"
    wallet = create_wallet(
        path,
        "testnet",
        passphrase,
        seed=_seed(vectors),
        kdf=fast_kdf,
        created_at=1_700_000_000,
    )

    assert wallet.address == vectors["addresses"]["testnet"]
    assert wallet.network == "testnet"
    assert wallet.public_key_hex == vectors["hybrid_public_key_hex"]
    assert wallet.path == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    document = _document(path)
    assert set(document) == {
        "version",
        "scheme",
        "network",
        "address",
        "public_key",
        "kdf",
        "cipher",
        "created_at",
        "ciphertext",
    }
    assert document["version"] == KEYFILE_VERSION
    assert document["created_at"] == 1_700_000_000
    assert document["kdf"]["name"] == "scrypt"
    assert (document["kdf"]["n"], document["kdf"]["r"], document["kdf"]["p"]) == (fast_kdf.n, 8, 1)
    assert len(document["ciphertext"]) == 2 * (32 + 16)

    loaded = load_wallet(path, passphrase)
    assert loaded.signer.seed == _seed(vectors)
    assert loaded.address == wallet.address
    assert loaded.header == read_header(path)


def test_create_randomizes_salt_and_nonce(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str, vectors: Vectors
) -> None:
    first = create_wallet(
        tmp_path / "a.key", "testnet", passphrase, seed=_seed(vectors), kdf=fast_kdf
    )
    second = create_wallet(
        tmp_path / "b.key", "testnet", passphrase, seed=_seed(vectors), kdf=fast_kdf
    )
    assert first.header.salt != second.header.salt
    assert first.header.nonce != second.header.nonce
    assert first.address == second.address


def test_create_refuses_to_overwrite(tmp_path: Path, fast_kdf: KdfParams, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    with pytest.raises(KeyfileError):
        create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    replaced = create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf, force=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_wallet(path, passphrase).address == replaced.address


def test_wrong_passphrase_fails(tmp_path: Path, fast_kdf: KdfParams, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    with pytest.raises(PassphraseError):
        load_wallet(path, "not the passphrase")


def test_tampered_ciphertext_fails(tmp_path: Path, fast_kdf: KdfParams, passphrase: str) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    document = _document(path)
    raw = bytearray(bytes.fromhex(document["ciphertext"]))
    raw[0] ^= 0x01
    document["ciphertext"] = raw.hex()
    _rewrite(path, document)
    with pytest.raises(PassphraseError):
        load_wallet(path, passphrase)


@pytest.mark.parametrize("field", ["created_at", "address", "public_key"])
def test_tampered_header_fails(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str, vectors: Vectors, field: str
) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    document = _document(path)
    if field == "created_at":
        document[field] += 1
    elif field == "address":
        document[field] = vectors["addresses"]["testnet"]
    else:
        document[field] = vectors["public_key_hex"]
    _rewrite(path, document)
    with pytest.raises((PassphraseError, KeyfileError)):
        load_wallet(path, passphrase)


def test_network_tampering_is_rejected(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    document = _document(path)
    document["network"] = "mainnet"
    _rewrite(path, document)
    with pytest.raises(KeyfileError):
        read_header(path)


def test_malformed_keyfiles_are_rejected(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    cases: list[tuple[Any, str]] = [
        (lambda d: d.pop("created_at"), "missing field"),
        (lambda d: d.update(version=1), "version"),
        (lambda d: d.update(scheme="ed25519"), "scheme"),
        (lambda d: d.update(network="regtest"), "network"),
        (lambda d: d.update(address=123), "address"),
        (lambda d: d["kdf"].update(name="pbkdf2"), "kdf"),
        (lambda d: d.update(kdf="scrypt"), "kdf"),
        (lambda d: d["kdf"].update(salt="zz"), "not valid hex"),
        (lambda d: d["kdf"].update(salt="00" * 15), "16 bytes"),
        (lambda d: d["kdf"].update(n=1000), "power of two"),
        (lambda d: d["cipher"].update(name="chacha20"), "cipher"),
        (lambda d: d["cipher"].update(nonce="00" * 11), "12 bytes"),
        (lambda d: d["cipher"].pop("nonce"), "missing field"),
        (lambda d: d.update(created_at="now"), "created_at"),
        (lambda d: d.update(public_key="zz"), "not valid hex"),
        (lambda d: d.update(public_key="00" * 31), "public_key"),
    ]
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    pristine = _document(path)
    for mutate, message in cases:
        document = copy.deepcopy(pristine)
        mutate(document)
        _rewrite(path, document)
        with pytest.raises(KeyfileError) as excinfo:
            read_header(path)
        assert message in str(excinfo.value)


def test_missing_ciphertext_is_rejected(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    path = tmp_path / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    document = _document(path)
    document.pop("ciphertext")
    _rewrite(path, document)
    assert read_header(path).network == "testnet"
    with pytest.raises(KeyfileError):
        load_wallet(path, passphrase)


def test_invalid_json_and_missing_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.key"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeyfileError):
        read_header(broken)
    with pytest.raises(KeyfileError):
        read_header(tmp_path / "absent.key")
    array = tmp_path / "array.key"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(KeyfileError):
        read_header(array)


def test_mnemonic_matches_golden(vectors: Vectors) -> None:
    seed = _seed(vectors)
    phrase = seed_to_mnemonic(seed)
    assert phrase == vectors["mnemonic"]
    assert len(phrase.split()) == MNEMONIC_WORD_COUNT
    assert mnemonic_to_seed(phrase) == seed
    hybrid = hybrid_keypair_from_seed(mnemonic_to_seed(phrase))
    assert hybrid.public_key_hex == vectors["hybrid_public_key_hex"]


@pytest.mark.parametrize(
    "phrase",
    [
        "",
        "abandon " * 23 + "abandon",
        "abandon " * 24,
        "notaword " * 24,
    ],
)
def test_invalid_mnemonics_are_rejected(phrase: str) -> None:
    with pytest.raises(KeyEncodingError):
        mnemonic_to_seed(phrase.strip())


def test_seed_to_mnemonic_requires_32_bytes() -> None:
    with pytest.raises(KeyEncodingError):
        seed_to_mnemonic(b"short")


def test_export_and_import_mnemonic(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str, vectors: Vectors
) -> None:
    source = tmp_path / "source.key"
    create_wallet(source, "testnet", passphrase, seed=_seed(vectors), kdf=fast_kdf)
    phrase = export_mnemonic(source, passphrase)
    assert phrase == vectors["mnemonic"]

    restored = import_mnemonic(
        tmp_path / "restored.key", phrase, "testnet", passphrase, kdf=fast_kdf
    )
    assert restored.address == vectors["addresses"]["testnet"]
    assert restored.signer.seed == _seed(vectors)


def test_passphrase_file(tmp_path: Path, passphrase: str) -> None:
    secret = tmp_path / "pass.txt"
    secret.write_text(passphrase + "\n", encoding="utf-8")
    secret.chmod(0o600)
    assert read_passphrase_file(secret) == passphrase

    secret.chmod(0o644)
    with pytest.raises(PassphraseError):
        read_passphrase_file(secret)

    secret.chmod(0o600)
    secret.write_text("\n", encoding="utf-8")
    with pytest.raises(PassphraseError):
        read_passphrase_file(secret)

    with pytest.raises(PassphraseError):
        read_passphrase_file(tmp_path / "absent.txt")


def test_mnemonic_file(tmp_path: Path, fast_kdf: KdfParams, passphrase: str) -> None:
    keyfile = tmp_path / "wallet.key"
    wallet = create_wallet(keyfile, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    phrase = export_mnemonic(keyfile, passphrase)

    backup = tmp_path / "wallet.mnemonic"
    half = len(phrase) // 2
    backup.write_text(f"{phrase[:half]}\n{phrase[half:]}\n", encoding="utf-8")
    backup.chmod(0o600)
    assert read_mnemonic_file(backup) == phrase

    restored = tmp_path / "restored.key"
    imported = import_mnemonic(
        restored, read_mnemonic_file(backup), "testnet", passphrase, kdf=fast_kdf
    )
    assert imported.address == wallet.address

    backup.chmod(0o644)
    with pytest.raises(PassphraseError):
        read_mnemonic_file(backup)

    backup.chmod(0o600)
    backup.write_text(" \n", encoding="utf-8")
    with pytest.raises(PassphraseError):
        read_mnemonic_file(backup)

    with pytest.raises(PassphraseError):
        read_mnemonic_file(tmp_path / "absent.mnemonic")


def test_derive_key_is_salt_bound(fast_kdf: KdfParams) -> None:
    first = derive_key("pass", bytes(16), fast_kdf)
    second = derive_key("pass", bytes([1]) * 16, fast_kdf)
    assert len(first) == 32
    assert first != second
    assert derive_key("pass", bytes(16), fast_kdf) == first


def test_default_kdf_is_the_hard_profile() -> None:
    """One profile: 128 MiB per guess, because the key must outlive a quantum computer."""
    assert DEFAULT_KDF.n == 2**17
    assert DEFAULT_KDF.r == 8 and DEFAULT_KDF.p == 1
    assert KdfParams(n=2**17, r=8, p=1) == DEFAULT_KDF
    assert len(derive_key("pass", bytes(16))) == 32


@pytest.mark.parametrize(("n", "r", "p"), [(1, 8, 1), (1000, 8, 1), (1024, 0, 1), (1024, 8, 0)])
def test_invalid_kdf_params(n: int, r: int, p: int) -> None:
    with pytest.raises(KeyfileError):
        KdfParams(n=n, r=r, p=p)


def test_derive_key_rejects_bad_salt_length(fast_kdf: KdfParams) -> None:
    with pytest.raises(KeyfileError):
        derive_key("pass", bytes(15), fast_kdf)


def test_wallet_exposes_network_params(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    wallet = create_wallet(
        tmp_path / "wallet.key", "mainnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf
    )
    assert wallet.network_params == get_network("mainnet")
    assert wallet.address.startswith("sccp1")


def test_create_wallet_makes_parent_directories(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    path = tmp_path / "nested" / "deeper" / "wallet.key"
    create_wallet(path, "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)
    assert path.is_file()


def test_create_wallet_reports_unwritable_parent(
    tmp_path: Path, fast_kdf: KdfParams, passphrase: str
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(KeyfileError):
        create_wallet(blocker / "wallet.key", "testnet", passphrase, seed=OTHER_SEED, kdf=fast_kdf)


def test_read_header_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(KeyfileError):
        read_header(tmp_path)
