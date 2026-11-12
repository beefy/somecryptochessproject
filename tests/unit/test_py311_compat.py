"""Guards for the minimum supported interpreter (Python 3.11).

The local development venv usually runs the newest Python, but CI also tests
3.11. Some behaviour differs between the two, and the difference is invisible
until the older job runs. Python 3.11 rejects a dataclass field whose *default*
is unhashable (``dict``, ``list``, ``set``, ``mappingproxy``) with "mutable
default ... is not allowed: use default_factory"; 3.12+ accepts those defaults.
This test re-implements the 3.11 rule so a regression fails on every version.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil

import sccp


def test_dataclass_defaults_are_hashable() -> None:
    offenders: list[str] = []
    for info in pkgutil.walk_packages(sccp.__path__, prefix="sccp."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if not dataclasses.is_dataclass(value):
                continue
            for field in dataclasses.fields(value):
                if field.default is dataclasses.MISSING:
                    continue
                if type(field.default).__hash__ is None:
                    name = getattr(value, "__name__", repr(value))
                    offenders.append(f"{info.name}.{name}.{field.name}")
    assert not offenders, (
        "Python 3.11 rejects unhashable dataclass defaults; use "
        f"field(default_factory=...): {', '.join(offenders)}"
    )
