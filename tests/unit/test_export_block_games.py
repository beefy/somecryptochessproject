"""``scripts/export_block_games.py``: the last block's games as PGN files.

The block body already carries the co-signed PGN of every proof, so the export
is tested against a real devnet chain: ``live_chain`` bootstraps a matchable
roster, ``chain_match`` produces a signed win proof, and ``mine_block`` puts it
in the tip block. The node API is exercised in-process through the same ASGI
transport the rest of the API tests use.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from sccp.chain.mempool import Mempool
from sccp.node.app import create_app
from sccp.node.service import NodeService
from sccp.transport import client_for

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_block_games.py"
"""The script under test."""


@pytest.fixture(scope="module")
def export() -> ModuleType:
    """Import ``scripts/export_block_games.py`` as a module."""
    spec = importlib.util.spec_from_file_location("sccp_export_block_games", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _service(chain: Any) -> NodeService:
    """A node around the already-bootstrapped chain."""
    return NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )


@pytest.fixture()
def proof_block(
    live_chain: Any,
    chain_match: Any,
    mine_block: Any,
    miner_keys: dict[str, Any],
) -> tuple[Any, Any, Any]:
    """A devnet chain whose tip block contains one valid win proof."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    block = mine_block(chain, miner_keys[match.proof.winner], proofs=[match.proof])
    return chain, match, block


def test_the_tip_blocks_games_are_written_with_a_manifest(
    export: ModuleType, proof_block: tuple[Any, Any, Any], tmp_path: Path
) -> None:
    chain, match, block = proof_block
    output = tmp_path / "games"
    with client_for(create_app(_service(chain))) as client:
        result = export.export_block_games(client, output_dir=output)

    assert result.block.height == block.header.height == chain.height
    assert result.status.height == chain.height
    assert len(result.games) == 1
    game = result.games[0]
    assert game.path.name == f"game-{match.proof.game_hash}.pgn"
    assert game.path.read_text(encoding="utf-8") == match.proof.pgn

    assert result.manifest == output / export.MANIFEST_NAME
    document = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert document["network"] == "devnet"
    assert document["block"]["height"] == block.header.height
    assert document["block"]["hash"] == result.block.hash
    assert document["block"]["miner"] == block.header.miner
    assert document["games"] == [
        {
            "file": game.path.name,
            "game_hash": match.proof.game_hash,
            "proof_id": match.proof.proof_id,
            "match_id": match.proof.match_id,
            "epoch": match.proof.epoch,
            "round": match.proof.round,
            "white": match.proof.certificate.white,
            "black": match.proof.certificate.black,
            "engine_white": match.proof.certificate.engine_white,
            "engine_black": match.proof.certificate.engine_black,
            "winner": match.proof.winner,
            "loser": match.proof.loser,
            "winner_seat": match.proof.winner_seat,
            "result": match.proof.result,
            "termination": match.proof.termination,
            "plies": match.proof.plies,
            "low_ply": match.proof.low_ply,
            "opening": match.proof.opening.model_dump(mode="json"),
            "time_control": match.proof.certificate.time_control.model_dump(mode="json"),
        }
    ]


def test_a_block_without_games_exports_an_empty_manifest(
    export: ModuleType, proof_block: tuple[Any, Any, Any], tmp_path: Path
) -> None:
    chain, _, _ = proof_block
    output = tmp_path / "genesis"
    with client_for(create_app(_service(chain))) as client:
        result = export.export_block_games(client, output_dir=output, block_reference="0")

    assert result.block.height == 0
    assert result.games == ()
    assert json.loads(result.manifest.read_text(encoding="utf-8"))["games"] == []
    assert list(output.iterdir()) == [result.manifest]


def test_the_report_names_the_node_and_flags_a_genesis_tip(
    export: ModuleType, devnet_chain: Any, tmp_path: Path
) -> None:
    output = tmp_path / "genesis"
    with client_for(create_app(_service(devnet_chain))) as client:
        result = export.export_block_games(client, output_dir=output)

    assert result.node == str(client.base_url)
    lines: list[str] = []
    export._report(result, lines.append)

    assert lines[0].startswith(f"node     {result.node} devnet tip 0 ")
    assert lines[1].startswith("block    0 ")
    assert any("tip is genesis" in line for line in lines)


def test_an_unknown_block_is_reported(
    export: ModuleType, proof_block: tuple[Any, Any, Any], tmp_path: Path
) -> None:
    chain, _, _ = proof_block
    with (
        client_for(create_app(_service(chain))) as client,
        pytest.raises(export.ExportError, match="HTTP 404"),
    ):
        export.export_block_games(
            client,
            output_dir=tmp_path / "missing",
            block_reference="999",
        )
    assert not (tmp_path / "missing").exists()


def test_a_corrupt_pgn_is_refused_before_it_is_written(
    export: ModuleType,
    proof_block: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chain, match, block = proof_block
    proof = match.proof
    corrupt = proof.model_copy(
        update={
            "pgn": proof.pgn.replace('[Termination "checkmate"]', '[Termination "resignation"]')
        }
    )
    assert corrupt.pgn != proof.pgn, "the fixture game should end in checkmate"

    view = _service(chain).block(str(block.header.height))
    tampered = view.model_copy(
        update={"block": view.block.model_copy(update={"proofs": (corrupt,)})}
    )
    monkeypatch.setattr(export, "fetch_block", lambda client, reference: tampered)
    monkeypatch.setattr(export, "fetch_params", lambda client: chain.params)

    output = tmp_path / "refused"
    with (
        client_for(create_app(_service(chain))) as client,
        pytest.raises(export.ExportError, match="game " + proof.game_hash),
    ):
        export.export_block_games(client, output_dir=output)
    assert not output.exists()


def test_the_parser_names_the_tip_as_the_default_block(export: ModuleType) -> None:
    args = export._parser().parse_args([])
    assert args.node == export.DEFAULT_NODE
    assert args.out == export.DEFAULT_OUT
    assert args.block is None
    assert args.timeout == export.DEFAULT_TIMEOUT


def test_a_stopped_node_is_explained(export: ModuleType, tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    output = tmp_path / "games"
    with (
        httpx.Client(base_url=export.DEFAULT_NODE, transport=httpx.MockTransport(refuse)) as client,
        pytest.raises(export.ExportError, match="start one first") as caught,
    ):
        export.export_block_games(client, output_dir=output)

    assert " --port 8645" in str(caught.value)
    assert not output.exists()
