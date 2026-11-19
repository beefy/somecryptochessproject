"""The release number lives in three files and two guides; none may drift.

A bump touches `pyproject.toml`, `src/sccp/version.py`, `src/web/package.json`
(and its lock) and the sample output in the operator guides. It is an easy edit
to half-finish, and nothing else notices: `sccp --version` and the deployed web
bundle would disagree with the tag.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from sccp import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_the_package_metadata_and_the_constant_agree() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == __version__


def test_the_browser_package_carries_the_same_release() -> None:
    """The web bundle ships with the node, so one bump moves both."""
    package = json.loads((ROOT / "src" / "web" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "src" / "web" / "package-lock.json").read_text(encoding="utf-8"))

    assert package["version"] == __version__
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__


def test_the_operator_guides_show_the_release_in_their_sample_output() -> None:
    for name in ("docs/guides/INSTALL.md", "docs/setup/RUN_A_DEVNET.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert f"sccp {__version__} (protocol v1)" in text, name


def test_the_changelog_documents_the_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## [{__version__}]" in changelog
