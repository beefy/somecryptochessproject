"""Write the keys for the local testnet docker-compose stack.

``docker compose up`` runs one ``init`` container with this script, then the
bootstrap service builds the first chain blocks, followed by the node, the relay
and four miners (two standard, two Chess960), all on the test network. Everything
here is derived from fixed seeds, so the stack is reproducible: the same four
addresses on every run.

Those seeds and the passphrase are published on purpose. Testnet coins have no
value and a predictable stack is what makes ``docker compose up`` work without a
coordination step. Never reuse this file or its keys on another network.

The relay config is written by ``deploy/bootstrap_testnet_stack.py`` after it has
built the chain: the beacon and roster come from the chain itself, not from a
stand-in document here.

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
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.crypto.wallet import KdfParams, create_wallet
from sccp.protocol.beacon import commitment

PASSPHRASE_ENV = "SCCP_STACK_PASSPHRASE"
DEFAULT_PASSPHRASE = "sccp-testnet-stack"
"""The published stack passphrase; see the module docstring."""

MINER_COUNT = 4
"""Four miners: two standard-chess seats and two Chess960 seats.

Each variant needs its own pair, and matching is within a variant, so the VM
runs exactly two matches per round. The VM is sized for four miner containers;
the synthetic traffic generator no longer adds a churned fifth miner.
"""

AVAILABILITY_EPOCH = 2
"""First epoch for which availability can exist on a fresh testnet."""

WITNESS_COUNT = 3
"""Clock witnesses the stack runs: the relay and two independent auditors.

The testnet quorum is one, so any single witness can settle a timeout claim; three
means a claim still has evidence with two of them down, and it is the smallest
set where equivocation (two conflicting signatures) is even expressible. Witness
1 is the relay's own identity -- it signs receipts inline as it accepts moves.
Witnesses 2 and 3 are separate ``sccp witness run`` services that audit the relay
and publish evidence bundles signed with their own keys.
"""

WITNESS_SEED_BASE = 0xB0
"""First byte of the published witness seeds: 0xB0, 0xB1, 0xB2."""

KDF = KdfParams(n=2**14)
"""scrypt cost for the stack keyfiles: 16 MiB, fast enough for a container start."""


@dataclass(frozen=True, slots=True)
class StackMiner:
    """One deterministic stack miner."""

    name: str
    variant: str
    keypair: HybridKeypair
    address: str
    keyfile: Path
    nonce_secret: str


@dataclass(frozen=True, slots=True)
class StackWitness:
    """One deterministic stack clock witness."""

    name: str
    keypair: HybridKeypair
    address: str
    keyfile: Path
    public_key_file: Path

    @property
    def public_key(self) -> str:
        """The hybrid public key, in the hex form the protocol signs against."""
        return self.keypair.public_key_hex


def miner_seed(index: int) -> bytes:
    """A fixed, published seed for miner ``index`` (0-based)."""
    return bytes([0xA0 + index]) * 32


def witness_seed(index: int) -> bytes:
    """A fixed, published seed for witness ``index`` (0-based)."""
    return bytes([WITNESS_SEED_BASE + index]) * 32


def witness_endpoint(index: int) -> str:
    """Where witness ``index`` answers, as the compose network sees it.

    Witness 1 is the relay's own identity, so it serves receipts on the relay's
    API port. Witnesses 2 and 3 are separate ``sccp witness run`` services on the
    next two ports; the endpoint is what ``register_witness`` records on chain.
    """
    if index == 0:
        return "http://relay:8646"
    return f"http://witness-{index + 1}:{8645 + index + 1}"


def _witness(index: int, directory: Path, params: NetworkParams) -> StackWitness:
    keypair = hybrid_keypair_from_seed(witness_seed(index))
    name = f"witness-{index + 1}"
    return StackWitness(
        name=name,
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
        keyfile=directory / "keys" / f"{name}.key",
        public_key_file=directory / "keys" / f"{name}.pub",
    )


def _variant_for(index: int) -> str:
    """First pair standard, second pair Chess960."""
    return "standard" if index < 2 else "chess960"


def _miner(index: int, directory: Path, params: NetworkParams) -> StackMiner:
    keypair = hybrid_keypair_from_seed(miner_seed(index))
    variant = _variant_for(index)
    params.variant(variant)
    return StackMiner(
        name=f"miner-{index + 1}",
        variant=variant,
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
        keyfile=directory / "keys" / f"miner-{index + 1}.key",
        nonce_secret=f"{index + 1:02x}" * 32,
    )


def nonce_file(directory: Path, name: str) -> Path:
    """Where :func:`write_stack` leaves the availability secret for miner ``name``.

    ``sccp miner run --nonce-secret-file`` reads it so the miner commits and
    reveals its own availability every epoch instead of relying on the one-off
    bootstrap.
    """
    return directory / "keys" / f"{name}.nonce"


def _write_keyfile(
    keyfile: Path, keypair: HybridKeypair, params: NetworkParams, passphrase: str
) -> None:
    if keyfile.is_file():
        return  # keep an existing keyfile: its salt and nonce are random
    create_wallet(
        keyfile,
        params,
        passphrase,
        seed=keypair.seed,
        kdf=KDF,
    )


def write_stack(out: Path, *, passphrase: str) -> dict[str, object]:
    """Create ``out`` with keyfiles, a passphrase file and ``stack.json``."""
    params = TESTNET
    out.mkdir(parents=True, exist_ok=True)
    (out / "keys").mkdir(exist_ok=True)
    miners = [_miner(index, out, params) for index in range(MINER_COUNT)]
    for miner in miners:
        _write_keyfile(miner.keyfile, miner.keypair, params, passphrase)
        secret_file = nonce_file(out, miner.name)
        secret_file.write_text(miner.nonce_secret + "\n", encoding="utf-8")
        secret_file.chmod(0o600)

    witnesses = [_witness(index, out, params) for index in range(WITNESS_COUNT)]
    for witness in witnesses:
        _write_keyfile(witness.keyfile, witness.keypair, params, passphrase)
        # The public half travels as a file because a hybrid key is 3 970 hex
        # characters: every miner gets the path and verifies receipts against it.
        witness.public_key_file.write_text(witness.public_key + "\n", encoding="utf-8")
        witness.public_key_file.chmod(0o644)

    passphrase_file = out / "passphrase"
    passphrase_file.write_text(passphrase + "\n", encoding="utf-8")
    passphrase_file.chmod(0o600)

    summary: dict[str, object] = {
        "network": params.network_id,
        "availability_epoch": AVAILABILITY_EPOCH,
        "passphrase_file": str(passphrase_file),
        "miners": [
            {
                "name": miner.name,
                "variant": miner.variant,
                "address": miner.address,
                "keyfile": str(miner.keyfile),
                "nonce_secret": miner.nonce_secret,
                "nonce_file": str(nonce_file(out, miner.name)),
                "commitment": commitment(miner.address, AVAILABILITY_EPOCH, miner.nonce_secret),
            }
            for miner in miners
        ],
        "witnesses": [
            {
                "name": witness.name,
                "address": witness.address,
                "keyfile": str(witness.keyfile),
                "public_key_file": str(witness.public_key_file),
                "public_key": witness.public_key,
                "endpoint": witness_endpoint(index),
            }
            for index, witness in enumerate(witnesses)
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
    for miner in summary["miners"] if isinstance(summary["miners"], list) else ():
        assert isinstance(miner, dict)
        print(f"  {miner['name']}: {miner['address']}")
    witnesses_summary = summary.get("witnesses")
    for witness in witnesses_summary if isinstance(witnesses_summary, list) else ():
        assert isinstance(witness, dict)
        print(f"  {witness['name']}: {witness['address']}")


if __name__ == "__main__":
    main()
