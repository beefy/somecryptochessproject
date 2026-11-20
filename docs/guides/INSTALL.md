# Install the CLI and point it at a node

Every guide in [`guides/`](.) starts from the same setup: an installed `sccp`, a
node URL, the network name, and a keyfile kept out of the tree. This page has it
once.

## Install

From the repository root:

```bash
git clone https://github.com/chessproofofwork/somecryptochessproject.git
cd somecryptochessproject
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # or `make install`, which does exactly this
sccp --version
```

```text
sccp 0.34.0 (protocol v1)
```

Python 3.11 or newer is required. `-e` keeps `src/sccp` in place, so an edit to
the checkout is the code you run, and `[dev]` adds pytest, hypothesis, ruff and
mypy — leave it off for the CLI alone. The HTTP examples throughout the docs pipe
JSON through `jq`; install it separately or drop the `| jq`. A plain
(non-editable) install and the Node/npm requirements for the web app are in
[`RUN_A_DEVNET.md`](../setup/RUN_A_DEVNET.md), section 1.

## Point at a node

The node serves the API on port `8645`:

```bash
export SCCP_NODE="http://127.0.0.1:8645"          # the stack you run yourself
# export SCCP_NODE="http://app.proofofchess.com:8645"   # the hosted testnet
curl -sS "$SCCP_NODE/v1/status" | jq '{network, height, tip_hash, mempool}'
```

```json
{"network": "testnet", "height": 19, "tip_hash": "...", "mempool": {"transactions": 0, "proofs": 0}}
```

The hosted deployment is plain HTTP and carries valueless test coins only, and it
resets on every deploy: addresses and balances from an earlier deploy are gone.

## Always name the network

Every command takes `--network testnet`, `--network devnet` or
`--network mainnet`. Without it the CLI falls back to `testnet` — **not** to the
keyfile's network — so a forgotten flag fails loudly rather than silently
signing for the wrong chain.

## Keep the keyfile out of the tree

A keyfile is mode `0600`, encrypted with a passphrase that is never stored, and
it lives outside the repository. Back up the 24-word phrase separately from the
keyfile: whoever holds either one holds the wallet. The full rules, including
restore, are in [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md).

## Next

- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — from nothing to a
  funded wallet.
- [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — the keyfile and mnemonic reference.
- [`SEND_COINS.md`](SEND_COINS.md) — spend the balance.
- All pages, by task: [`docs/README.md`](../README.md).
