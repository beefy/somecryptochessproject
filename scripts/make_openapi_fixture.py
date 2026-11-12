"""Write the node API contract snapshot used by ``tests/unit/test_api.py``.

Run this only when an API change is intended::

    python scripts/make_openapi_fixture.py

The projection drops prose and the package version, so a release bump alone
never touches the fixture, while a new route, field or status code does.
"""

from __future__ import annotations

import json
from pathlib import Path

from sccp.config import DEVNET
from sccp.node.app import create_app, openapi_contract
from sccp.node.service import NodeService


def main() -> None:
    """Generate ``tests/fixtures/openapi.json`` from the current app."""
    service = NodeService.open(DEVNET)
    try:
        contract = openapi_contract(create_app(service))
    finally:
        service.close()
    payload = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    target = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "openapi.json"
    target.write_text(payload, encoding="utf-8")
    print(f"wrote {target} ({len(payload)} bytes, {len(contract['paths'])} paths)")


if __name__ == "__main__":
    main()
