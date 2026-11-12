#!/usr/bin/env python3
"""Verify that all recorded versions agree before a release is published.

Compares the static version in ``pyproject.toml`` with ``__version__`` in
``src/sccp/version.py`` and, when ``RELEASE_TAG`` is set (as it is for GitHub
``release`` events), with the tag itself. A leading ``v`` is ignored, so
``v0.4.0`` matches version ``0.4.0``.

Exits with status 1 and a message on the first mismatch, which makes it usable
both as a CI gate and as a local pre-tag check::

    RELEASE_TAG=v0.4.0 python scripts/check_release_version.py
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
VERSION_MODULE = REPO_ROOT / "src" / "sccp" / "version.py"
VERSION_PATTERN = re.compile(r'^__version__: Final = "([^"]+)"', re.MULTILINE)


def project_version() -> str:
    """Return ``project.version`` from ``pyproject.toml``."""
    document = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    version = document["project"]["version"]
    if not isinstance(version, str):
        raise SystemExit(f"{PYPROJECT} has a non-string project.version: {version!r}")
    return version


def module_version() -> str:
    """Return ``__version__`` from ``src/sccp/version.py``."""
    source = VERSION_MODULE.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(source)
    if match is None:
        raise SystemExit(f"could not find __version__ in {VERSION_MODULE}")
    return match.group(1)


def tag_version(tag: str) -> str:
    """Strip the conventional ``v`` prefix from a release tag."""
    return tag.removeprefix("v")


def main() -> int:
    declared = project_version()
    module = module_version()
    if declared != module:
        print(
            f"error: pyproject.toml says {declared} but src/sccp/version.py says {module}; "
            "keep them in sync before releasing",
            file=sys.stderr,
        )
        return 1

    tag = os.environ.get("RELEASE_TAG", "")
    if tag and tag_version(tag) != declared:
        print(
            f"error: release tag {tag!r} does not match package version {declared!r}",
            file=sys.stderr,
        )
        return 1

    print(f"version {declared} consistent (tag: {tag or 'not a release event'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
