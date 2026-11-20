"""The whitepaper's inventories must match the code they describe.

`docs/design/WHITEPAPER.md` promises to describe the current implementation, but
every list in it is copied by hand. The ledger grew seven transaction types
(pools, liquidity, swaps and the bridge's hash locks), a variant asset, a second
network-facing subsystem and ten node routes while section 13.2 still showed
eleven types and section 15.2 showed seventeen routes; a reader of the
whitepaper could not learn that the DEX, Chess960 or the ETH bridge existed.

Nothing re-derives prose, so this file compares the parts that are mechanically
checkable against their source of truth:

* `sccp.protocol.models.TxType` against the whitepaper's transaction table *and*
  `PROTOCOL.md` section 10.2, which the whitepaper calls normative;
* the pinned OpenAPI fixture against both API tables, in both directions, so a
  removed route cannot linger in prose either;
* the header's version against `sccp.version`;
* the shipped DEX fee, Chess960 schedule and per-network bridge parameters
  against the sentences that state them;
* the stall slash's price and its 50/30/20 split against `sccp.chain.timeout`,
  because a housekeeping charge that is documented in three places is exactly
  the kind of prose that outlives its constant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final, cast

import pytest

from sccp import version
from sccp.bridge.config import bridge_environment
from sccp.chain.timeout import (
    STALL_BURN_BPS,
    STALL_CLAIMANT_BPS,
    STALL_QUORUM_BPS,
    STALL_SLASH_BPS,
    stall_slash_amount,
)
from sccp.config import CHESS960, MAINNET, TESTNET, UNITS_PER_COIN
from sccp.protocol.models import TxType

ROOT = Path(__file__).resolve().parents[2]
WHITEPAPER: Final = ROOT / "docs" / "design" / "WHITEPAPER.md"
PROTOCOL: Final = ROOT / "docs" / "design" / "PROTOCOL.md"
OPENAPI: Final = ROOT / "tests" / "fixtures" / "openapi.json"

_PLACEHOLDER: Final = re.compile(r"\{[^}]*\}")
_ROUTE: Final = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH) (/v1/[A-Za-z0-9_\-/{}]+)")
_PROTOCOL_ROUTE: Final = re.compile(r"^\| (GET|POST|PUT|DELETE|PATCH) \| `(/v1/[^`]+)`", re.M)
_TABLE_TYPE: Final = re.compile(r"^\| `([a-z_]+)` \|", re.M)


def _section(text: str, heading: str) -> str:
    """Return one numbered section, from its heading to the next same-or-higher one."""
    match = re.search(rf"^(#+) {re.escape(heading)}\b", text, re.M)
    assert match is not None, f"heading {heading!r} is missing"
    level = len(match.group(1))
    end = re.compile(rf"^#{{1,{level}}} ", re.M).search(text, match.end())
    return text[match.start() : end.start() if end else len(text)]


def _normalise(routes: str) -> str:
    """Erase placeholder names so `{reference}` and `{height-or-hash}` compare equal."""
    return _PLACEHOLDER.sub("{}", routes)


@pytest.fixture(scope="module")
def whitepaper() -> str:
    return WHITEPAPER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def protocol() -> str:
    return PROTOCOL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def openapi() -> dict[str, dict[str, object]]:
    document = cast("dict[str, object]", json.loads(OPENAPI.read_text(encoding="utf-8")))
    return cast("dict[str, dict[str, object]]", document["paths"])


def test_the_header_version_is_the_shipped_version(whitepaper: str) -> None:
    match = re.search(r"reference implementation (\d+\.\d+\.\d+)", whitepaper)
    assert match is not None, "the whitepaper header no longer names a version"
    assert match.group(1) == version.__version__, (
        f"the whitepaper header says {match.group(1)}, the package is {version.__version__}"
    )


def test_the_transaction_table_lists_exactly_the_shipped_types(whitepaper: str) -> None:
    table = _section(whitepaper, "13.2")
    documented = {match.group(1) for match in _TABLE_TYPE.finditer(table)}
    shipped = {tx.value for tx in TxType}
    assert documented == shipped, (
        f"whitepaper 13.2 is missing {sorted(shipped - documented)} and lists "
        f"{sorted(documented - shipped)}, which no longer exist"
    )


def test_protocol_lists_the_same_transaction_types(protocol: str) -> None:
    """The whitepaper defers to `PROTOCOL.md` 10.2, so that table owes the same list."""
    table = _section(protocol, "10.2")
    documented = {match.group(1) for match in _TABLE_TYPE.finditer(table)}
    shipped = {tx.value for tx in TxType}
    assert documented == shipped, (
        f"PROTOCOL.md 10.2 is missing {sorted(shipped - documented)} and lists "
        f"{sorted(documented - shipped)}, which no longer exist"
    )


def test_the_api_table_documents_every_published_route(
    whitepaper: str, openapi: dict[str, dict[str, object]]
) -> None:
    table = _normalise(_section(whitepaper, "15.2"))
    missing = sorted(
        f"{method.upper()} {_normalise(path)}"
        for path, operations in openapi.items()
        for method in operations
        if f"{method.upper()} {_normalise(path)}" not in table
    )
    assert not missing, f"whitepaper 15.2 does not document {missing}"


def test_the_api_table_publishes_no_retired_route(
    whitepaper: str, openapi: dict[str, dict[str, object]]
) -> None:
    published = {
        f"{method.upper()} {_normalise(path)}"
        for path, operations in openapi.items()
        for method in operations
    }
    table = _section(whitepaper, "15.2")
    documented = {
        f"{match.group(1)} {_normalise(match.group(2))}" for match in _ROUTE.finditer(table)
    }
    assert documented <= published, (
        f"whitepaper 15.2 documents {sorted(documented - published)}, which the "
        "node no longer serves"
    )


def test_protocol_documents_the_published_routes(
    protocol: str, openapi: dict[str, dict[str, object]]
) -> None:
    """`PROTOCOL.md` 14.1 is the normative route table, in a method-then-path layout."""
    section = _section(protocol, "14.1")
    documented = {
        f"{match.group(1)} {_normalise(match.group(2))}"
        for match in _PROTOCOL_ROUTE.finditer(section)
    }
    published = {
        f"{method.upper()} {_normalise(path)}"
        for path, operations in openapi.items()
        for method in operations
    }
    assert documented == published, (
        f"PROTOCOL.md 14.1 is missing {sorted(published - documented)} and "
        f"documents {sorted(documented - published)}, which the node does not serve"
    )


def test_the_variant_table_repeats_the_issued_schedule(whitepaper: str) -> None:
    table = _section(whitepaper, "13.6")
    assert f"{CHESS960.subsidy // UNITS_PER_COIN} per block" in table
    assert f"{CHESS960.halving_interval:,}" in table
    assert f"{CHESS960.max_supply:,}" in table
    assert f"{TESTNET.dex_fee_bps} basis points" in _section(whitepaper, "13.7")


def test_the_bridge_table_repeats_the_shipped_parameters(whitepaper: str) -> None:
    """Chain id, confirmations and margin are constants, so the table must quote them."""
    table = _section(whitepaper, "13.8")
    for network in ("devnet", "testnet", "mainnet"):
        environment = bridge_environment(network)
        row = re.search(rf"^\| `{network}` \| (.+?) \|$", table, re.M)
        assert row is not None, f"the bridge table has no {network} row"
        cells = [cell.strip() for cell in row.group(1).split("|")]
        assert f"({environment.chain_id})" in cells[0], (
            f"{network} bridges to chain {environment.chain_id}, the table says {cells[0]!r}"
        )
        assert cells[1] == str(environment.confirmations)
        assert cells[2] == f"{environment.sccp_margin} blocks"


def test_the_bridge_margin_stays_above_the_reorg_depth() -> None:
    """Section 13.8 argues safety from this inequality, so it must hold in code."""
    for name, params in (("testnet", TESTNET), ("mainnet", MAINNET)):
        environment = bridge_environment(name)
        assert environment.sccp_margin > params.reorg_depth, (
            f"{name} margin {environment.sccp_margin} does not exceed reorg_depth "
            f"{params.reorg_depth}"
        )


def test_the_stall_slash_and_its_split_are_the_documented_ones(
    whitepaper: str, protocol: str
) -> None:
    """The 10% timeout penalty and where it goes are stated in three places.

    Whitepaper 11.7 (the settlement rule), Whitepaper 16.1 (why it is priced that
    way) and `PROTOCOL.md` 10.4 (normative) all name the charge and its split, so
    a moved basis point has to move the sentences with it. The burn is defined as
    "whatever is not credited", which only equals `STALL_BURN_BPS` while the three
    shares add up to the charge: a fourth share added to the code would silently
    shrink the burn those documents promise.
    """
    slash = STALL_SLASH_BPS // 100
    claimant, witnesses, burned = (
        STALL_CLAIMANT_BPS // 100,
        STALL_QUORUM_BPS // 100,
        STALL_BURN_BPS // 100,
    )
    assert claimant + witnesses + burned == 100, "the split is not a whole charge"
    assert claimant * 2 == 100, "the documents call the claimant's share 'half'"
    # The price is a share of the *protocol* bond, not of whatever the seat
    # happened to stake, which is what makes it the same cost for every seat.
    assert stall_slash_amount(MAINNET) == MAINNET.min_bond // 10
    bond = stall_slash_amount(MAINNET) // UNITS_PER_COIN

    acceptance = " ".join(_section(whitepaper, "11.7").split())
    assert f"{slash}% of the protocol's miner bond (`STALL_SLASH_BPS`)" in acceptance
    assert f"half to the claimant, {witnesses}% in equal parts to the witnesses" in acceptance
    assert f"{burned}% burned by not crediting it" in acceptance
    assert "comes out of the seat's bond first and its balance after that" in acceptance

    economics = " ".join(_section(whitepaper, "16.1").split())
    assert f"{slash}% of the protocol's miner bond ({bond} SCCP on mainnet)" in economics
    assert f"Half goes to the claimant, {witnesses}% is split between the witnesses" in economics
    assert f"and {burned}% is burned" in economics

    settlement = " ".join(_section(protocol, "10.4").split())
    assert f"`min_bond * {STALL_SLASH_BPS} / 10000`" in settlement
    assert f"{slash}% of the protocol's miner bond, {bond} SCCP on mainnet" in settlement
    assert (
        f"split {claimant}/{witnesses}/{burned} "
        "(`STALL_CLAIMANT_BPS`, `STALL_QUORUM_BPS`, `STALL_BURN_BPS`)"
    ) in settlement
    assert f"and the last {burned}% burned" in settlement
    assert "taken from its bond first and from its spendable balance" in settlement


def test_every_table_row_has_its_header_s_column_count(whitepaper: str) -> None:
    """A row that lost a cell renders as shifted text, and this file adds rows by hand."""
    broken: list[str] = []
    header: list[str] | None = None
    fenced = False
    for number, line in enumerate(whitepaper.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            header = None
            continue
        if fenced:
            continue
        if not stripped.startswith("|"):
            header = None
            continue
        if "\\|" in stripped:
            continue
        cells = stripped.strip("|").split("|")
        if header is None:
            header = cells
        elif len(cells) != len(header):
            broken.append(f"line {number}: {len(cells)} cells, header has {len(header)}")
    assert not broken, "malformed tables: " + "; ".join(broken)


def test_the_sections_are_numbered_without_gaps_or_repeats(whitepaper: str) -> None:
    """Inserting 13.6 before 13.8 is easy to get wrong; anchors depend on the numbers."""
    seen: set[str] = set()
    chapter = 0
    for match in re.finditer(r"^(##|###) (\d+)(?:\.(\d+))?\.? ", whitepaper, re.M):
        level, first, second = match.groups()
        if level == "##":
            assert int(first) == chapter + 1, f"chapter {first} follows {chapter}"
            chapter = int(first)
            continue
        key = f"{first}.{second}"
        assert key not in seen, f"section {key} is defined twice"
        seen.add(key)
    assert chapter >= 20, "the whitepaper lost a chapter"
