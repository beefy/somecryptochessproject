"""Verified engine downloads (see ``scripts/fetch_engine.py``).

``data/engines.json`` pins, for every supported engine and platform, the
download URL, the SHA-256 of the archive and the archive member that holds the
binary. Nothing is executed before the digest matches, and an unknown or
unpinned engine is an error rather than a "best effort" download.

Older Stockfish releases do not publish digests through the GitHub API, so the
pins below were computed from the official release asset (trust on first use
over HTTPS). Regenerating a pin is a deliberate, reviewed change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from urllib.request import urlopen

from sccp.errors import EngineFetchError

MANIFEST_FORMAT: Final = 1
CHUNK_SIZE: Final = 1 << 20
DEFAULT_MANIFEST_PATH: Final[Path] = Path(__file__).resolve().parents[3] / "data" / "engines.json"
"""Repository manifest, used as the download script's default."""

PACKAGED_MANIFEST_PATH: Final[Path] = Path(__file__).resolve().parent / "data" / "engines.json"
REPOSITORY_MANIFEST_PATH: Final[Path] = DEFAULT_MANIFEST_PATH

Opener = Callable[[str], Any]
"""``urlopen``-shaped callable; tests inject an in-memory response."""


@dataclass(frozen=True, slots=True)
class EngineAsset:
    """One platform build of an engine."""

    platform: str
    url: str
    sha256: str
    member: str
    size: int | None = None


@dataclass(frozen=True, slots=True)
class EngineRelease:
    """All platform builds of one pinned engine version."""

    name: str
    version: str
    assets: Mapping[str, EngineAsset]


def detect_platform(system: str | None = None, machine: str | None = None) -> str:
    """Map ``sys.platform``/``platform.machine()`` to a manifest key."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system == "darwin":
        return "macos"
    if system.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "linux-x86_64"
        if machine in {"aarch64", "arm64"}:
            return "linux-arm64"
    if system.startswith("win") and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    raise EngineFetchError(f"no pinned engine build for {system}/{machine}")


def _parse_asset(engine: str, platform_name: str, raw: object) -> EngineAsset:
    """Validate one manifest asset, including a well-formed digest."""
    if not isinstance(raw, dict):
        raise EngineFetchError(f"engine {engine!r} asset {platform_name!r} is not an object")
    fields: Mapping[object, object] = raw
    url = fields.get("url")
    digest = fields.get("sha256")
    member = fields.get("member")
    size = fields.get("size")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise EngineFetchError(f"engine {engine!r} asset {platform_name!r} needs an HTTPS url")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise EngineFetchError(
            f"engine {engine!r} asset {platform_name!r} needs a 64-char lowercase sha256"
        )
    if not isinstance(member, str) or not member:
        raise EngineFetchError(f"engine {engine!r} asset {platform_name!r} needs a member")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise EngineFetchError(f"engine {engine!r} asset {platform_name!r} has a bad size")
    return EngineAsset(platform=platform_name, url=url, sha256=digest, member=member, size=size)


def releases_from_document(document: object) -> Mapping[str, EngineRelease]:
    """Validate a decoded ``engines.json`` document."""
    if not isinstance(document, dict):
        raise EngineFetchError("engine manifest must be a JSON object")
    fields: Mapping[object, object] = document
    if fields.get("format") != MANIFEST_FORMAT:
        raise EngineFetchError(
            f"unsupported engine manifest format {fields.get('format')!r}; "
            f"expected {MANIFEST_FORMAT}"
        )
    raw_engines = fields.get("engines")
    if not isinstance(raw_engines, dict) or not raw_engines:
        raise EngineFetchError("engine manifest needs a non-empty 'engines' object")
    releases: dict[str, EngineRelease] = {}
    for name, raw_release in raw_engines.items():
        if not isinstance(name, str) or not name:
            raise EngineFetchError("engine names must be non-empty strings")
        if not isinstance(raw_release, dict):
            raise EngineFetchError(f"engine {name!r} is not an object")
        release_fields: Mapping[object, object] = raw_release
        version = release_fields.get("version")
        raw_assets = release_fields.get("assets")
        if not isinstance(version, str) or not version:
            raise EngineFetchError(f"engine {name!r} needs a version")
        if not isinstance(raw_assets, dict) or not raw_assets:
            raise EngineFetchError(f"engine {name!r} needs at least one asset")
        assets: dict[str, EngineAsset] = {}
        for platform_name, raw_asset in raw_assets.items():
            if not isinstance(platform_name, str) or not platform_name:
                raise EngineFetchError(f"engine {name!r} has an empty platform name")
            assets[platform_name] = _parse_asset(name, platform_name, raw_asset)
        releases[name] = EngineRelease(name=name, version=version, assets=MappingProxyType(assets))
    return MappingProxyType(releases)


def default_manifest_path() -> Path:
    """The packaged manifest when installed, else the repository copy."""
    for candidate in (PACKAGED_MANIFEST_PATH, REPOSITORY_MANIFEST_PATH):
        if candidate.is_file():
            return candidate
    raise EngineFetchError(
        f"engine manifest not found; looked in {PACKAGED_MANIFEST_PATH} and "
        f"{REPOSITORY_MANIFEST_PATH}"
    )


def load_manifest(path: Path | None = None) -> Mapping[str, EngineRelease]:
    """Load and validate the engine manifest."""
    manifest_path = default_manifest_path() if path is None else path
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineFetchError(f"cannot read engine manifest {manifest_path}: {exc}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EngineFetchError(
            f"engine manifest {manifest_path} is not valid JSON: {exc}"
        ) from None
    return releases_from_document(document)


def fetch_engine(
    name: str,
    dest: Path = Path("./bin"),
    *,
    platform_name: str | None = None,
    manifest_path: Path | None = None,
    opener: Opener = urlopen,
    force: bool = False,
) -> Path:
    """Download, verify and unpack one pinned engine build.

    Returns the path to the executable. The archive is streamed to a temporary
    file inside ``dest``; ``dest`` never sees an unverified binary.
    """
    releases = load_manifest(manifest_path)
    try:
        release = releases[name]
    except KeyError:
        known = ", ".join(sorted(releases))
        raise EngineFetchError(f"unknown engine {name!r}; pinned engines: {known}") from None
    selected_platform = platform_name or detect_platform()
    try:
        asset = release.assets[selected_platform]
    except KeyError:
        available = ", ".join(sorted(release.assets))
        raise EngineFetchError(
            f"engine {name!r} has no build for {selected_platform!r}; available: {available}"
        ) from None

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(asset.member).name
    if target.exists() and not force:
        raise EngineFetchError(f"{target} already exists; pass force=True to replace it")

    with tempfile.TemporaryDirectory(dir=dest, prefix=".sccp-engine-") as workspace:
        archive = Path(workspace) / "download"
        digest = _download(asset.url, archive, opener=opener, expected_size=asset.size)
        if not hmac.compare_digest(digest, asset.sha256):
            raise EngineFetchError(
                f"{asset.url} has sha256 {digest}, but the manifest pins {asset.sha256}; "
                f"refusing to install it"
            )
        member = _resolve_member(archive, asset.member)
        staged = Path(workspace) / target.name
        _extract_member(archive, member, staged)
        os.replace(staged, target)
    return target


def _download(url: str, target: Path, *, opener: Opener, expected_size: int | None) -> str:
    """Stream ``url`` into ``target``, returning the SHA-256 of the bytes."""
    digest = hashlib.sha256()
    written = 0
    try:
        with opener(url) as response, target.open("wb") as handle:
            while True:
                chunk: bytes = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if expected_size is not None and written > expected_size:
                    raise EngineFetchError(
                        f"{url} is larger than the {expected_size} bytes in the manifest"
                    )
                digest.update(chunk)
                handle.write(chunk)
    except EngineFetchError:
        raise
    except OSError as exc:
        raise EngineFetchError(f"cannot download {url}: {exc}") from None
    if expected_size is not None and written != expected_size:
        raise EngineFetchError(f"{url} is {written} bytes, but the manifest pins {expected_size}")
    return digest.hexdigest()


def _member_names(archive: Path) -> list[str]:
    """List archive members for tarballs and zips."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            return bundle.namelist()
    try:
        with tarfile.open(archive, "r:*") as bundle:
            return bundle.getnames()
    except tarfile.TarError as exc:
        raise EngineFetchError(f"cannot read engine archive {archive}: {exc}") from None


def _resolve_member(archive: Path, hint: str, *, system: str | None = None) -> str:
    """Pick the binary member, falling back to the first sorted ``stockfish*`` file."""
    names = _member_names(archive)
    if hint in names:
        return hint
    executable_suffix = ".exe" if (system or sys.platform).startswith("win") else ""
    candidates = [
        name
        for name in names
        if not name.endswith("/")
        and os.path.basename(name).startswith("stockfish")
        and (name.endswith(".exe") if executable_suffix else not name.endswith(".exe"))
        and "/src/" not in name
    ]
    if not candidates:
        raise EngineFetchError(f"{archive} contains no stockfish binary (looked for {hint!r})")
    return sorted(candidates)[0]


def _extract_member(archive: Path, member: str, target: Path) -> None:
    """Write one archive member to ``target`` and mark it executable."""
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                payload = bundle.read(member)
        else:
            with tarfile.open(archive, "r:*") as bundle:
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise EngineFetchError(f"{member!r} in {archive} is not a regular file")
                payload = extracted.read()
    except (KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise EngineFetchError(f"cannot extract {member!r} from {archive}: {exc}") from None
    target.write_bytes(payload)
    target.chmod(0o755)
