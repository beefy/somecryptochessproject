"""Write the keys for a local devnet relay + miner stack.

This is the devnet counterpart of ``deploy/init_testnet_stack.py``: two
deterministic keyfiles, one availability secret each, a passphrase file and
``stack.json``. ``deploy/bootstrap_devnet_stack.py`` reads the result, builds the
devnet chain those keys can mine, and writes the relay config the chain implies.

Devnet coins have no value and a predictable stack is the point: the same two
addresses on every run, so a relay config, a script or a test can name them.
Never reuse this file or its keys on another network.

Usage::

    python deploy/init_devnet_stack.py --out .devnet-stack
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sccp.config import DEVNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.pq import HybridKeypair, hybrid_keypair_from_seed
from sccp.crypto.wallet import KdfParams, create_wallet
from sccp.protocol.beacon import commitment

PASSPHRASE_ENV = "SCCP_DEVNET_STACK_PASSPHRASE"
DEFAULT_PASSPHRASE = "sccp-devnet-stack"
"""The published stack passphrase; see the module docstring."""

MINER_COUNT = 2
"""Two miners are the minimum for one match."""

AVAILABILITY_EPOCH = 3
"""First epoch for which a fresh devnet can hold a roster."""

KDF = KdfParams(n=2**14)
"""scrypt cost for the stack keyfiles: 16 MiB, fast enough for a scripted start."""


@dataclass(frozen=True, slots=True)
class StackMiner:
    """One deterministic stack miner."""

    name: str
    keypair: HybridKeypair
    address: str
    keyfile: Path
    nonce_secret: str


def miner_seed(index: int) -> bytes:
    """A fixed, published seed for miner ``index`` (0-based)."""
    return bytes([0xD0 + index]) * 32


def _miner(index: int, directory: Path, params: NetworkParams) -> StackMiner:
    keypair = hybrid_keypair_from_seed(miner_seed(index))
    return StackMiner(
        name=f"miner-{index + 1}",
        keypair=keypair,
        address=encode_address(keypair.public_key_bytes, params),
        keyfile=directory / "keys" / f"miner-{index + 1}.key",
        nonce_secret=f"{index + 1:02x}" * 32,
    )


def nonce_file(directory: Path, name: str) -> Path:
    """Where :func:`write_stack` leaves the availability secret for miner ``name``.

    ``sccp miner run --nonce-secret-file`` reads it so the miner commits and
    reveals its own availability every epoch instead of relying on the bootstrap.
    """
    return directory / "keys" / f"{name}.nonce"


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


def write_stack(out: Path, *, passphrase: str) -> dict[str, object]:
    """Create ``out`` with keyfiles, a passphrase file and ``stack.json``."""
    params = DEVNET
    out.mkdir(parents=True, exist_ok=True)
    (out / "keys").mkdir(exist_ok=True)
    miners = [_miner(index, out, params) for index in range(MINER_COUNT)]
    for miner in miners:
        _write_keyfile(miner, params, passphrase)
        secret_file = nonce_file(out, miner.name)
        secret_file.write_text(miner.nonce_secret + "\n", encoding="utf-8")
        secret_file.chmod(0o600)

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
                "address": miner.address,
                "keyfile": str(miner.keyfile),
                "nonce_secret": miner.nonce_secret,
                "nonce_file": str(nonce_file(out, miner.name)),
                "commitment": commitment(miner.address, AVAILABILITY_EPOCH, miner.nonce_secret),
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
    parser.add_argument(
        "--out", type=Path, default=Path(".devnet-stack"), help="Directory to fill."
    )
    parser.add_argument(
        "--passphrase",
        default=os.environ.get(PASSPHRASE_ENV, DEFAULT_PASSPHRASE),
        help=f"Passphrase for every stack keyfile (default: ${PASSPHRASE_ENV}).",
    )
    arguments = parser.parse_args()
    summary = write_stack(arguments.out, passphrase=arguments.passphrase)
    print(f"stack for {summary['network']} in {arguments.out}")
    for miner in summary["miners"]:
        assert isinstance(miner, dict)
        print(f"  {miner['name']}: {miner['address']}")


if __name__ == "__main__":
    main()
