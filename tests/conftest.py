"""Shared fixtures for the sccp test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sccp.crypto.wallet import FAST_KDF, KdfParams

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    """Golden vectors from tests/fixtures/crypto_vectors.json."""
    data: object = json.loads((FIXTURES / "crypto_vectors.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def fast_kdf() -> KdfParams:
    """Cheap scrypt parameters; production keyfiles use n=2**15."""
    return FAST_KDF


@pytest.fixture()
def passphrase() -> str:
    """A passphrase with spaces and mixed case."""
    return "correct horse battery staple"
