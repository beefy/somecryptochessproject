"""The synchronous in-process ASGI transport."""

from __future__ import annotations

import pytest

from sccp.errors import SccpError, SccpTypeError
from sccp.transport import client_for


def test_a_non_callable_app_is_rejected() -> None:
    with pytest.raises(SccpTypeError, match="ASGI callable"):
        client_for(object())


def test_sccp_type_error_keeps_the_builtin_hierarchy() -> None:
    assert issubclass(SccpTypeError, SccpError)
    assert issubclass(SccpTypeError, TypeError)
