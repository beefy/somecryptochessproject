"""Verified engine downloads: digests, archives, platform selection."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from sccp.chess.fetch import (
    EngineRelease,
    _extract_member,
    detect_platform,
    fetch_engine,
    load_manifest,
    releases_from_document,
)
from sccp.errors import EngineFetchError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch_engine.py"


def _tar_gz(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _zip(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    return buffer.getvalue()


def _manifest(
    tmp_path: Path,
    payload: bytes,
    *,
    digest: str | None = None,
    size: int | None = None,
    member: str = "stockfish/stockfish-test",
    url: str = "https://example.invalid/engine.tar.gz",
    engine: str = "stockfish-test",
    platform_name: str = "macos",
) -> Path:
    import hashlib

    asset = {
        "url": url,
        "sha256": digest or hashlib.sha256(payload).hexdigest(),
        "member": member,
        "size": len(payload) if size is None else size,
    }
    document = {
        "format": 1,
        "engines": {
            engine: {"version": "test", "assets": {platform_name: asset}},
        },
    }
    path = tmp_path / "engines.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _opener(payload: bytes) -> Callable[[str], Any]:
    def open_url(url: str) -> io.BytesIO:
        return io.BytesIO(payload)

    return open_url


def test_pinned_manifest_loads() -> None:
    releases = load_manifest()
    assert {"stockfish-19", "stockfish-18"} <= set(releases)
    latest = releases["stockfish-19"]
    assert isinstance(latest, EngineRelease)
    assert "macos" in latest.assets
    assert latest.assets["macos"].url.startswith(
        "https://github.com/official-stockfish/Stockfish/releases/download/sf_19/"
    )
    for release in releases.values():
        for asset in release.assets.values():
            assert len(asset.sha256) == 64
            assert asset.sha256 == asset.sha256.lower()


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("darwin", "arm64", "macos"),
        ("darwin", "x86_64", "macos"),
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "aarch64", "linux-arm64"),
        ("win32", "AMD64", "windows-x86_64"),
    ],
)
def test_detect_platform(system: str, machine: str, expected: str) -> None:
    assert detect_platform(system, machine) == expected


def test_detect_platform_rejects_unknown_targets() -> None:
    with pytest.raises(EngineFetchError, match="no pinned engine build"):
        detect_platform("freebsd", "x86_64")
    with pytest.raises(EngineFetchError, match="no pinned engine build"):
        detect_platform("linux", "riscv64")


def test_fetch_extracts_and_verifies_a_tarball(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"#!engine", "stockfish/Copying.txt": b"gpl"})
    manifest = _manifest(tmp_path, payload)
    binary = fetch_engine(
        "stockfish-test",
        tmp_path / "bin",
        manifest_path=manifest,
        opener=_opener(payload),
        platform_name="macos",
    )
    assert binary == tmp_path / "bin" / "stockfish-test"
    assert binary.read_bytes() == b"#!engine"
    assert binary.stat().st_mode & 0o777 == 0o755
    assert not list((tmp_path / "bin").glob(".sccp-engine-*"))


def test_fetch_supports_zip_archives(tmp_path: Path) -> None:
    payload = _zip({"stockfish/stockfish-test.exe": b"exe"})
    manifest = _manifest(tmp_path, payload, member="stockfish/stockfish-test.exe")
    binary = fetch_engine(
        "stockfish-test",
        tmp_path / "bin",
        manifest_path=manifest,
        opener=_opener(payload),
        platform_name="macos",
    )
    assert binary.read_bytes() == b"exe"


def test_digest_mismatch_installs_nothing(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"bin"})
    manifest = _manifest(tmp_path, payload, digest="0" * 64)
    with pytest.raises(EngineFetchError, match="refusing to install"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )
    assert not (tmp_path / "bin" / "stockfish-test").exists()


@pytest.mark.parametrize("delta", [-1, 1])
def test_size_mismatch_is_reported(tmp_path: Path, delta: int) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"bin"})
    manifest = _manifest(tmp_path, payload, size=len(payload) + delta)
    with pytest.raises(EngineFetchError, match="bytes"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )


def test_oversized_download_is_aborted(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"bin"})
    manifest = _manifest(tmp_path, payload, size=8)
    with pytest.raises(EngineFetchError, match="larger than"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )


def test_unknown_engine_and_platform(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"bin"})
    manifest = _manifest(tmp_path, payload, engine="stockfish-test")
    with pytest.raises(EngineFetchError, match="unknown engine"):
        fetch_engine("other", tmp_path / "bin", manifest_path=manifest, opener=_opener(payload))
    with pytest.raises(EngineFetchError, match="no build for"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="linux-x86_64",
        )


def test_existing_binary_needs_force(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"one"})
    manifest = _manifest(tmp_path, payload)
    kwargs = {
        "manifest_path": manifest,
        "opener": _opener(payload),
        "platform_name": "macos",
    }
    first = fetch_engine("stockfish-test", tmp_path / "bin", **kwargs)
    with pytest.raises(EngineFetchError, match="already exists"):
        fetch_engine("stockfish-test", tmp_path / "bin", **kwargs)

    replacement = _tar_gz({"stockfish/stockfish-test": b"two"})
    manifest = _manifest(tmp_path, replacement, member="stockfish/stockfish-test")
    replaced = fetch_engine(
        "stockfish-test",
        tmp_path / "bin",
        manifest_path=manifest,
        opener=_opener(replacement),
        platform_name="macos",
        force=True,
    )
    assert replaced == first
    assert first.read_bytes() == b"two"


def test_member_hint_falls_back_to_the_stockfish_binary(tmp_path: Path) -> None:
    payload = _tar_gz(
        {
            "stockfish/Copying.txt": b"gpl",
            "stockfish/src/stockfish": b"source build",
            "stockfish/stockfish-x86": b"real binary",
        }
    )
    manifest = _manifest(tmp_path, payload, member="stockfish/missing")
    binary = fetch_engine(
        "stockfish-test",
        tmp_path / "bin",
        manifest_path=manifest,
        opener=_opener(payload),
        platform_name="macos",
    )
    assert binary.read_bytes() == b"real binary"


def test_archive_without_a_binary_is_rejected(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/Copying.txt": b"gpl"})
    manifest = _manifest(tmp_path, payload, member="stockfish/missing")
    with pytest.raises(EngineFetchError, match="no stockfish binary"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )


def test_download_oserror_is_wrapped(tmp_path: Path) -> None:
    payload = _tar_gz({"stockfish/stockfish-test": b"bin"})
    manifest = _manifest(tmp_path, payload)

    def broken(url: str) -> io.BytesIO:
        raise OSError("connection reset")

    with pytest.raises(EngineFetchError, match="cannot download"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=broken,
            platform_name="macos",
        )


def test_manifest_file_errors(tmp_path: Path) -> None:
    with pytest.raises(EngineFetchError, match="cannot read engine manifest"):
        load_manifest(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(EngineFetchError, match="not valid JSON"):
        load_manifest(bad)


def test_default_manifest_path_is_resolvable() -> None:
    from sccp.chess.fetch import default_manifest_path

    assert default_manifest_path().is_file()


def test_default_manifest_path_reports_a_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sccp.chess import fetch

    monkeypatch.setattr(fetch, "PACKAGED_MANIFEST_PATH", tmp_path / "packaged.json")
    monkeypatch.setattr(fetch, "REPOSITORY_MANIFEST_PATH", tmp_path / "repo.json")
    with pytest.raises(EngineFetchError, match="engine manifest not found"):
        fetch.default_manifest_path()


def test_corrupt_archive_is_rejected(tmp_path: Path) -> None:
    payload = b"this is not a tar or zip"
    manifest = _manifest(tmp_path, payload)
    with pytest.raises(EngineFetchError, match="cannot read engine archive"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )


def test_missing_archive_member_is_wrapped(tmp_path: Path) -> None:
    archive = tmp_path / "engine.tar.gz"
    archive.write_bytes(_tar_gz({"stockfish/stockfish-test": b"bin"}))
    target = tmp_path / "out"
    with pytest.raises(EngineFetchError, match="cannot extract"):
        _extract_member(archive, "stockfish/nope", target)
    assert not target.exists()


def test_directory_member_is_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("stockfish/stockfish-test/")
        info.type = tarfile.DIRTYPE
        bundle.addfile(info)
    payload = buffer.getvalue()
    manifest = _manifest(tmp_path, payload, member="stockfish/stockfish-test/")
    with pytest.raises(EngineFetchError, match="not a regular file"):
        fetch_engine(
            "stockfish-test",
            tmp_path / "bin",
            manifest_path=manifest,
            opener=_opener(payload),
            platform_name="macos",
        )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"format": 2, "engines": {"x": {"version": "1", "assets": {}}}},
        {"format": 1, "engines": {}},
        {"format": 1, "engines": {"": {"version": "1", "assets": {}}}},
        {"format": 1, "engines": {"x": "nope"}},
        {"format": 1, "engines": {"x": {"assets": {"a": {}}}}},
        {"format": 1, "engines": {"x": {"version": "1"}}},
        {"format": 1, "engines": {"x": {"version": "1", "assets": {}}}},
        {"format": 1, "engines": {"x": {"version": "1", "assets": {"": {}}}}},
        {
            "format": 1,
            "engines": {
                "x": {
                    "version": "1",
                    "assets": {
                        "macos": {
                            "url": "http://insecure.example/engine",
                            "sha256": "0" * 64,
                            "member": "stockfish/stockfish",
                        }
                    },
                }
            },
        },
        {
            "format": 1,
            "engines": {
                "x": {
                    "version": "1",
                    "assets": {
                        "macos": {
                            "url": "https://example.invalid/engine",
                            "sha256": "not-a-digest",
                            "member": "stockfish/stockfish",
                        }
                    },
                }
            },
        },
        {
            "format": 1,
            "engines": {
                "x": {
                    "version": "1",
                    "assets": {
                        "macos": {
                            "url": "https://example.invalid/engine",
                            "sha256": "0" * 64,
                            "member": "",
                        }
                    },
                }
            },
        },
        {
            "format": 1,
            "engines": {
                "x": {
                    "version": "1",
                    "assets": {
                        "macos": {
                            "url": "https://example.invalid/engine",
                            "sha256": "0" * 64,
                            "member": "stockfish/stockfish",
                            "size": 0,
                        }
                    },
                }
            },
        },
        {
            "format": 1,
            "engines": {
                "x": {
                    "version": "1",
                    "assets": {
                        "macos": {
                            "url": "https://example.invalid/engine",
                            "sha256": "0" * 64,
                            "member": "stockfish/stockfish",
                            "size": True,
                        }
                    },
                }
            },
        },
    ],
)
def test_invalid_manifests_are_rejected(document: object) -> None:
    with pytest.raises(EngineFetchError):
        releases_from_document(document)


def test_asset_must_be_an_object() -> None:
    document = {"format": 1, "engines": {"x": {"version": "1", "assets": {"macos": []}}}}
    with pytest.raises(EngineFetchError, match="not an object"):
        releases_from_document(document)


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("fetch_engine_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_lists_pins(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main(["--list"]) == 0
    output = capsys.readouterr().out
    assert "stockfish-19" in output
    assert "macos" in output


def test_script_reports_fetch_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main(["--engine", "not-pinned"]) == 1
    assert "unknown engine" in capsys.readouterr().err
