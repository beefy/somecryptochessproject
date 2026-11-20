"""Set up a complete local devnet stack: keys, bootstrap chain and relay config.

This is the single entry point for the devnet counterpart of
``deploy/init_testnet_stack.py``. One command writes the deterministic keyfiles,
builds the three-block bootstrap chain those keys can mine, and writes the relay
config the chain implies:

    python deploy/setup_devnet_stack.py --out .devnet-stack

The pieces still live in importable modules -- ``init_devnet_stack`` owns the
keyfiles and ``bootstrap_devnet_stack`` owns the chain -- so tests and scripts can
use them without a command line. This module is what documentation points at,
and what runs both in the right order with one set of paths.

``--fresh`` deletes the bootstrap chain but never the keys. Stop the node before
pointing ``--data-dir`` at a chain it has open: two processes writing blocks to
the same SQLite file would disagree about the tip.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

try:  # the tests import this module as ``deploy.setup_devnet_stack``
    from deploy import bootstrap_devnet_stack as bootstrap
    from deploy import init_devnet_stack as init
except ImportError:  # pragma: no cover - a direct ``python deploy/...`` run
    import bootstrap_devnet_stack as bootstrap  # type: ignore[no-redef]
    import init_devnet_stack as init  # type: ignore[no-redef]


def _parser() -> argparse.ArgumentParser:
    """The command line documented in the devnet guide."""
    parser = argparse.ArgumentParser(
        description="Write a devnet key stack, bootstrap its chain and write relay.json.",
    )
    parser.add_argument(
        "--out",
        "--stack",
        dest="out",
        type=Path,
        default=Path(".devnet-stack"),
        help="stack directory to write (default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="chain directory (default: <out>/chain)",
    )
    parser.add_argument(
        "--relay-out",
        type=Path,
        default=None,
        help="relay config path (default: <out>/relay.json)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="delete the chain database (never the keys) before bootstrapping",
    )
    parser.add_argument(
        "--passphrase",
        default=None,
        help=(
            "keyfile passphrase (default: $SCCP_DEVNET_STACK_PASSPHRASE or "
            f"{init.DEFAULT_PASSPHRASE!r})"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the stack, bootstrap the chain and report the commands to run."""
    arguments = _parser().parse_args(argv)
    out: Path = arguments.out
    data_dir = out / "chain" if arguments.data_dir is None else arguments.data_dir
    relay_out = out / "relay.json" if arguments.relay_out is None else arguments.relay_out
    passphrase = (
        arguments.passphrase or os.environ.get(init.PASSPHRASE_ENV) or init.DEFAULT_PASSPHRASE
    )

    summary = init.write_stack(out, passphrase=passphrase)
    record = summary["miners"]
    assert isinstance(record, list)
    print(f"wrote {len(record)} devnet keyfiles and {out / 'stack.json'}")

    miners = bootstrap.load_stack(out)
    if arguments.fresh:
        bootstrap.remove_chain(data_dir / "chain.sqlite")
    service, blocks, relay = bootstrap.build_chain(miners, data_dir=data_dir)
    try:
        epoch, _record = bootstrap.relay_epoch(service)
        bootstrap.write_relay(relay, relay_out)
        if blocks:
            print(
                f"built {len(blocks)} bootstrap blocks: height {service.height}, "
                f"epoch {epoch} matchable"
            )
        else:
            print(f"kept the existing chain at height {service.height}, epoch {epoch}")
        print(f"relay config for epoch {epoch} written to {relay_out}")
        bootstrap.print_next_steps(out, data_dir, relay_out, miners, epoch)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
