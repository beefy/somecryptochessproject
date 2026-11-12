#!/usr/bin/env python3
"""Download a pinned chess engine build and verify its SHA-256.

The pins live in ``data/engines.json`` (URL, archive digest, archive member).
Nothing is installed unless the streamed digest matches, so a compromised or
truncated download fails loudly instead of producing a silently different
engine fingerprint.

Examples::

    python scripts/fetch_engine.py --list
    python scripts/fetch_engine.py --engine stockfish-19 --dest ./bin
    python scripts/fetch_engine.py --engine stockfish-18 --platform macos

Engine binaries are GPL-3.0; this script downloads them from the upstream
project but does not redistribute them with sccp.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sccp.chess.engines import engine_fingerprint  # noqa: E402 - after sys.path setup
from sccp.chess.fetch import (  # noqa: E402 - after sys.path setup
    DEFAULT_MANIFEST_PATH,
    detect_platform,
    fetch_engine,
    load_manifest,
)
from sccp.errors import EngineFetchError  # noqa: E402 - after sys.path setup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download a pinned Stockfish build and verify its SHA-256.",
    )
    parser.add_argument(
        "--engine",
        default="stockfish-19",
        help="manifest engine name (default: stockfish-19)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("bin"),
        help="directory for the executable (default: ./bin)",
    )
    parser.add_argument(
        "--platform",
        dest="platform_name",
        default=None,
        help="override the detected platform key (see --list)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="engine manifest path (default: data/engines.json)",
    )
    parser.add_argument("--list", action="store_true", help="print pinned engines and exit")
    parser.add_argument("--force", action="store_true", help="replace an existing binary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        releases = load_manifest(args.manifest)
        if args.list:
            for release in releases.values():
                platforms = ", ".join(sorted(release.assets))
                print(f"{release.name} ({release.version}): {platforms}")
            return 0
        binary = fetch_engine(
            args.engine,
            args.dest,
            platform_name=args.platform_name,
            manifest_path=args.manifest,
            force=args.force,
        )
        fingerprint = engine_fingerprint(binary)
    except EngineFetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"platform: {args.platform_name or detect_platform()}")
    print(f"binary: {binary}")
    print(f"sha256: {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
