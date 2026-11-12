"""Write the keys and relay config for the local testnet docker-compose stack.

``docker compose up`` runs one ``init`` container with this script, then starts
the node, the relay and three miners, all on the test network. Everything the
relay needs (a beacon, a roster with public keys and engine fingerprints) is
derived here from fixed seeds, so the stack is reproducible: the same three
addresses and the same beacon on every run.

Those seeds and the passphrase are published on purpose. Testnet coins have no
value and a predictable stack is what makes ``docker compose up`` work without a
coordination step. Never reuse this file or its keys on another network.

Usage::

    python deploy/init_testnet_stack.py --out /stack
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sccp.config import TESTNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed
from sccp.crypto.wallet import KdfParams, create_wallet
from sccp.protocol.beacon import commitment, compute_beacon

PASSPHRASE_ENV = "SCCP_STACK_PASSPHRASE"
DEFAULT_PASSPHRASE = "sccp-testnet-stack"
"""The published stack passphrase; see the module docstring."""

MINER_COUNT = 3
STACK_EPOCH = 1
ENGINE = "mock-v1"
"""The engine fingerprint the stack's mock engines report."""

KDF = KdfParams(n=2**14)
"""scrypt cost for the stack keyfiles: 16 MiB, fast enough for a container start."""


@dataclass(frozen=True, slots=True)
class StackMiner:
    """One deterministic stack miner."""

    name: str
    keypair: Keypair
    address: str
    keyfile: Path
    nonce_secret: str


def miner_seed(index: int) -> bytes:
    """A fixed, published seed for miner ``index`` (0-based)."""
    return bytes([0xA0 + index]) * 32


def _miner(index: int, directory: Path, params: NetworkParams) -> StackMiner:
    keypair = keypair_from_seed(miner_seed(index))
    return StackMiner(
        name=f"miner-{index + 1}",
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
        keyfile=directory / "keys" / f"miner-{index + 1}.key",
        nonce_secret=f"{index + 1:02x}" * 32,
    )


def _write_keyfile(miner: StackMiner, params: NetworkParams, passphrase: str) -> None:
    if miner.keyfile.is_file():
        return  # keep an existing keyfile: its salt and nonce are random
    create_wallet(
        miner.keyfile,
        params,
        passphrase,
        seed=miner.keypair.seed,
        kdf=KDF,
    )


def build_relay_document(miners: list[StackMiner], params: NetworkParams) -> dict[str, object]:
    """The relay config: beacon, roster, timeout and the first round to open."""
    assert params.genesis_hash is not None  # testnet pins one
    beacon = compute_beacon(
        network=params.network_id,
        epoch=STACK_EPOCH,
        block_hash=params.genesis_hash,
        reveals=(),  # the stack has no on-chain reveals yet; the beacon still derives
    )
    roster = {
        miner.address: {
            "public_key": miner.keypair.public_key_hex,
            "engine": ENGINE,
            "max_games": params.rounds_per_epoch,
        }
        for miner in miners
    }
    return {
        "network": params.network_id,
        "beacon": beacon.model_dump(mode="json"),
        "roster": roster,
        "timeout_seconds": 900,
        "open_round": 0,
    }


def write_stack(out: Path, *, passphrase: str) -> dict[str, object]:
    """Create ``out`` with keyfiles, a passphrase file and ``relay.json``."""
    params = TESTNET
    out.mkdir(parents=True, exist_ok=True)
    (out / "keys").mkdir(exist_ok=True)
    miners = [_miner(index, out, params) for index in range(MINER_COUNT)]
    for miner in miners:
        _write_keyfile(miner, params, passphrase)

    passphrase_file = out / "passphrase"
    passphrase_file.write_text(passphrase + "\n", encoding="utf-8")
    passphrase_file.chmod(0o600)

    relay_document = build_relay_document(miners, params)
    (out / "relay.json").write_text(
        json.dumps(relay_document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    summary: dict[str, object] = {
        "network": params.network_id,
        "epoch": STACK_EPOCH,
        "beacon": relay_document["beacon"]["hash"],
        "relay_config": str(out / "relay.json"),
        "passphrase_file": str(passphrase_file),
        "miners": [
            {
                "name": miner.name,
                "address": miner.address,
                "keyfile": str(miner.keyfile),
                "nonce_secret": miner.nonce_secret,
                "commitment": commitment(miner.address, STACK_EPOCH, miner.nonce_secret),
            }
            for miner in miners
        ],
    }
    (out / "stack.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    """Parse the arguments and write the stack."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("stack"), help="Directory to fill.")
    parser.add_argument(
        "--passphrase",
        default=os.environ.get(PASSPHRASE_ENV, DEFAULT_PASSPHRASE),
        help=f"Passphrase for every stack keyfile (default: ${PASSPHRASE_ENV}).",
    )
    arguments = parser.parse_args()
    summary = write_stack(arguments.out, passphrase=arguments.passphrase)
    print(f"stack for {summary['network']} in {arguments.out}")
    print(f"beacon: {summary['beacon']}")
    for miner in summary["miners"]:
        assert isinstance(miner, dict)
        print(f"  {miner['name']}: {miner['address']}")


if __name__ == "__main__":
    main()
