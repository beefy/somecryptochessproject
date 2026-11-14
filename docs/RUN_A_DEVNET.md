# Run and test on the devnet

The devnet is the smallest complete SCCP network. It is not a mock: the same
node, block builder, validator, relay, wallets, faucet and explorer that run on
the public testnet run here, on parameters chosen so that everything happens
quickly and deterministically (`DEVNET` in `src/sccp/config.py`):

| Parameter | devnet | testnet |
| --- | --- | --- |
| Epoch length | 1 block | 5 blocks |
| Wins required per block | 1 | 1 |
| Miner bond | 0 coins | 10 coins |
| Difficulty retarget | off | every 10 blocks |
| Genesis timestamp | fixed (`1_700_000_000`) | fixed |
| Block subsidy | 50 coins | 50 coins |
| Coinbase maturity | 20 blocks | 20 blocks |
| Minimum fee | 0.00001 coins | 0.00001 coins |
| Proof-bearing game length | 20 to 300 plies | 20 to 300 plies |

Because epochs are one block long and the bond is zero, a devnet chain reaches a
matchable roster in four blocks instead of twenty-three, and a proof settles one
block after the game instead of five. Nothing on this network is worth anything,
and devnet addresses (`sccpt1...`) are not valid on testnet or mainnet: the
network id is part of the address.

This guide covers the whole loop on that network:

1. [Install the `sccp` package from this repository](#1-install-the-package-from-this-repository)
2. [Smoke-test the network in one command](#2-smoke-test-devnet-up-in-one-command)
3. [Build a devnet chain you can keep](#3-build-a-devnet-chain-you-can-keep)
4. [Serve it with a node](#4-serve-the-chain-with-a-node)
5. [Run the web UI against it](#5-run-the-web-ui-against-the-devnet)
6. [Open a wallet on the devnet](#6-open-a-wallet-on-the-devnet)
7. [Fund it from the devnet faucet](#7-fund-a-wallet-from-the-devnet-faucet)
8. [Send coins](#8-send-coins-on-the-devnet)
9. [Run a miner on the devnet](#9-run-a-miner-on-the-devnet)
10. [Reset, and what to do when something looks wrong](#10-reset-and-troubleshooting)
11. [Where to go next](#11-where-to-go-next)

Every command below was run against this checkout; the outputs are pasted from
those runs (long hashes shortened with `...`). Where a number is specific to
your machine (a keyfile address, a tip hash) it is marked as an example.

---

## 1. Install the package from this repository

You need **Python 3.11 or newer** and, for the explorer, **Node.js 20.19+ or
22.12+** with npm. A C toolchain is not needed: the only native-ish dependency
is `cryptography`, which ships wheels.

### 1.1 Editable install (what development uses)

```bash
git clone https://github.com/beefy/somecryptochessproject.git
cd somecryptochessproject
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # or `make install`, which does exactly this
sccp --version
```

```text
sccp 0.17.1 (protocol v1)
```

`-e` keeps `src/sccp` in place, so an edit to the checkout is the code you run.
`[dev]` adds pytest, hypothesis, ruff and mypy; leave it off if you only want
the CLI. `make test`, `make lint` and `make typecheck` all expect the dev
extras.

### 1.2 A plain install from source (no checkout needed afterwards)

To install the command for real, from a source tree:

```bash
python3 -m venv ~/.venvs/sccp
~/.venvs/sccp/bin/pip install /path/to/somecryptochessproject
~/.venvs/sccp/bin/sccp --version
```

Verified in a clean virtual environment: `pip install .` builds the wheel with
hatchling and the resulting `sccp node run --network devnet` works from any
directory, because the package ships the opening book, the engine manifest and
all three genesis documents:

```text
sccp/chess/data/openings.json
sccp/chess/data/engines.json
sccp/chain/data/genesis/{testnet,mainnet,devnet}.json
```

That is what the `force-include` rules in `pyproject.toml` are for: an
installed wheel is self-contained. `pipx install .` and `uv tool install .`
work the same way if you have them.

To build distributables instead, `make dist` (needs `build`) writes
`sccp-<version>-py3-none-any.whl` and an sdist into `dist/`; `make dist-check`
validates them with twine.

### 1.3 If you would rather not install at all

From the repository root, with the dev dependencies installed,
`python -m sccp.cli <args>` is the same entry point as `sccp <args>`. The
`Makefile` uses that form throughout.

> **Always pass `--network devnet`.** The CLI's default network is `testnet`,
> and it is the default for every command, including wallet commands. A keyfile
> records the network it was created for, and a command with a mismatched
> `--network` refuses to run.

---

## 2. Smoke test: `devnet up` in one command

```bash
sccp devnet up --miners 2 --blocks 4
```

```text
devnet up: height 4 after 2.060s
tip: 3f423dba23a95c326b2bd54d369a13226732de3d2599f31277404d2d788c9d6f
genesis: 5e3465524fbf12874f77384baae18eec299068d96aa85ef8a34a3932b0b5f8a4
epochs: 3; 3 matches; 1 proofs mined
  sccpt1q9ejahp48496x4zw7d6qsj020kq3l9tly9tgt5el4km824nnjpzmwddqavq 99.99990000
  sccpt1q8c97qs6ehcm6c6aw6pwlqjt9jlmqgdjxfsvq4vu7k6jsysfkwme79lm4gn 99.99990000
```

That is a complete network in one process: it creates the genesis chain, funds
the miners from the devnet faucet, registers them, commits and reveals
availability, pairs them by beacon, plays the matches through the relay, mines
the win proofs and reports final balances. The relay and the node are FastAPI
apps reached over `httpx` ASGI transports, so **no port is opened**, and nothing
is written to `--data-dir` — the run is a demonstration, not a chain you can
query afterwards.

- `--miners N` (default 4, minimum 2) and `--blocks N` (default 10) are the two
  knobs that matter day to day. `--blocks` is the height the run must reach,
  including the bootstrap and heartbeat blocks between proofs.
- `--engines mock` is the only supported engine set, and it is the default: the
  games are played by `MockEngine`, which is deterministic, needs no binary, and
  exists so a test can assert an exact chain.
- `make devnet` is the same command with four miners and ten blocks.
- `sccp devnet up --json` prints the run as an object: `network`, `miners`,
  `blocks`, `height`, `tip_hash`, `genesis_hash`, `epochs`, `matches`, `proofs`,
  `balances`, `seconds`.

If this fails, fix the install before going further: nothing below is more
likely to work than this command is.

---

## 3. Build a devnet chain you can keep

`devnet up` throws its chain away, as it should: a chain you want to browse,
point a wallet at and mine onto has to live in SQLite. Two ways to get one.

### 3.1 With a real game in it (recommended)

`scripts/play_miner.py --on-chain` bootstraps a devnet chain in `--chain-dir` and
then seats *you* at the board against a real `EngineMiner`, through the relay's
HTTP API. When the game ends it co-signs the result, turns the transcript into a
win proof and mines it with the production block builder.

```bash
python scripts/play_miner.py --on-chain --network devnet --chain-dir .devnet \
    --engine mock
```

```text
chain    devnet at .devnet (height 3)
         another run resumes this chain; --fresh resets it
...      (board, clocks and move receipts)
chain    co-signing the result and mining the proof...
chain    mined block 4 74452877a725...
proof    65e34b4ffec6978bea8a9fdaa9d3a25ababf70f918e9bfb70d2379530f50bca9
         (winner black: sccpt1q95flp5avfy0a6qf68eyy7puqptjqj44f2rgtq2l7n9sajfjlm2zujkrutk,
         network devnet)
```

The height is 3 before the game and 4 after: the bootstrap occupied blocks 1 to
3, and a proof settles in the first block after the epoch it was won in. Moves
are accepted as SAN (`Nf3`, `O-O`, `exd5`) or UCI (`g1f3`); type `help` at the
prompt for the commands (`resign`, `quit`, `board`, `moves`, `receipts`).

Two rules bite here:

- **A game must last at least 20 plies to carry a proof.** Resign on move two
  and the run says so — `not mined: 4 plies is below devnet's 20-ply proof
  minimum` — and the chain stays where it was.
- **A draw produces no block.** A decisive result is required, whichever seat
  won: losing is fine, drawing is not.

Useful flags: `--color auto` takes the pairing instead of always playing white,
`--seed <64 hex chars>` changes your seat (the default is a public demo seed),
`--games 3` plays several games on the same chain, `--fresh` discards an existing
chain and bootstraps again, `--engine stockfish-19 --engine-path ./bin/stockfish`
plays a real engine (`python scripts/fetch_engine.py` downloads the pinned build
into `./bin`), and `--pgn-out game.pgn` writes the game record. A second run
without `--fresh` resumes the chain at its next epoch.

### 3.2 Empty, if you only need a target

```bash
sccp --network devnet --data-dir .devnet node run --port 8645 \
     --mine --miner-address <your-address>
```

A fresh data directory is a genesis-only chain (height 0). Everything in
sections 4 to 8 works on it; it just has no games to look at. With `--mine` the
chain climbs to height 3 on its own — the end of the empty bootstrap window —
and then waits for something to carry.

> One data directory per network. The SQLite file records the network it was
> created for and the node refuses the wrong one:
> `error: database /tmp/devnet-e2e/chain.sqlite holds devnet, not testnet`.

---

## 4. Serve the chain with a node

```bash
sccp --network devnet --data-dir .devnet node run --port 8645 --web \
     --mine --miner-address sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr
```

```text
node on 127.0.0.1:8645 for devnet height 4 (0 txs, 0 proofs pending)
tip: 74452877a72510a3db5707af28250df30b84572bfa4cf9a681f89e5d14343a67
genesis: 5e3465524fbf12874f77384baae18eec299068d96aa85ef8a34a3932b0b5f8a4
mining proof blocks for sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr
INFO:     Uvicorn running on http://127.0.0.1:8645 (Press Ctrl+C to quit)
```

One process is the JSON API, the explorer, the faucet and — because of `--mine` —
a settlement miner. What each part does:

- **The API.** `GET /v1/status` is the health check; `/v1/blocks`, `/v1/games`,
  `/v1/accounts/{address}`, `/v1/epochs/{epoch}`, `/v1/mempool` and the rest are
  documented in [`PROTOCOL.md`](PROTOCOL.md) section 13. The OpenAPI document is
  served at `/docs` and `/openapi.json`.
- **`--web`.** Mounts the built explorer (`src/web/dist`) at `/`. Section 5.
- **The faucet.** Enabled by default on any test network whose genesis funds a
  faucet key — both `testnet` and `devnet`. `--no-faucet` turns the HTTP
  endpoint off. Section 7.
- **`--mine`.** A devnet node is not a chess miner: it does not search for games
  or own a bond. It settles proofs already in its mempool and, when the pool has
  nothing to settle, emits a **heartbeat block**: a proof-less block carrying at
  most one ledger transaction and minting nothing. That is what lets a faucet
  grant or a transfer settle on a devnet with no miners at all:

  ```text
  $ sccp --data-dir .devnet chain blocks --network devnet
       4 74452877a72510a3... ts=1700000004 epoch=4 8tx 1proofs
       5 647060da92b66a64... ts=1794617235 epoch=5 1tx 0proofs
       6 c06bbe2254dd4d98... ts=1794617250 epoch=6 1tx 0proofs
       7 4f0eba0523504161... ts=1794617260 epoch=7 1tx 0proofs
  ```

  One ledger transaction per proof-less block, so five pending transfers need
  five blocks. `--mine-interval-seconds N` sets a floor of N seconds between
  blocks (0, the default, is "as fast as the pool allows" — what you want on
  devnet); `--mine-poll-seconds` is how often each attempt runs.
  `--no-mine-bootstrap` stops `--mine` from extending the empty bootstrap
  window, which is what you want on a chain that was bootstrapped elsewhere.
- **`--miner-address`.** Where the coinbase goes: the 50-coin subsidy plus the
  fees of the transactions in the block. The whole coinbase is credited as
  `immature` and becomes spendable 20 blocks later, fees included — so a miner
  address earns slowly, not instantly.
- **`--peer http://...`** pull-syncs another node at startup, `--host 0.0.0.0`
  exposes the API beyond loopback, and `--genesis <file>` overrides the genesis
  document (tests only).

The status document, verbatim:

```bash
curl -s http://127.0.0.1:8645/v1/status
```

```json
{"network":"devnet","protocol_version":1,"height":4,
 "tip_hash":"74452877a72510a3db5707af28250df30b84572bfa4cf9a681f89e5d14343a67",
 "genesis_hash":"5e3465524fbf12874f77384baae18eec299068d96aa85ef8a34a3932b0b5f8a4",
 "peers":0,"syncing":false,"mempool":{"transactions":0,"proofs":0}}
```

> **Ports 8645 and 8646 are often already taken.** The compose stack in `deploy/`
> publishes both. If `docker compose ps` shows a running `sccp-testnet-*`, use
> `--port 8650` and give the web dev server `SCCP_NODE=http://127.0.0.1:8650`
> (section 5.2) rather than stopping somebody's testnet. A busy port at least
> fails loudly; a *stale* listener is worse — an older image answering on 8645
> or 8646 returns pre-hybrid documents this build cannot parse.

---

## 5. Run the web UI against the devnet

The explorer is a React Native for Web app in `src/web`. It reads the node's
`/v1` API and, from `#/play`, the relay. There is no fixtures mode: with no node
reachable the pages say so instead of inventing data.

### 5.1 Let the node serve it (one origin, no CORS)

```bash
cd src/web
npm ci
npm run build            # tsc --noEmit && vite build -> src/web/dist
cd ../..

sccp --network devnet --data-dir .devnet node run --port 8645 --web --mine \
     --miner-address <your-address>
```

Open <http://127.0.0.1:8645/>. The header prints the chain's network id, height
and tip, so "the page says devnet" means the node behind it is a devnet node.
Point it at a build somewhere else with `SCCP_WEB_DIST=/path/to/dist` — that
variable is an override, not a hint: no `index.html` there is an error (exit 2),
not a silent fallback to a stale bundle.

### 5.2 Vite dev server with hot reload (two terminals)

```bash
# terminal 1
sccp --network devnet --data-dir .devnet node run --port 8645 --mine \
     --miner-address <your-address>

# terminal 2
cd src/web
npm ci
npm run dev              # http://localhost:5173
```

The dev server proxies `/v1` to `SCCP_NODE` (default `http://127.0.0.1:8645`),
so the browser only ever talks to `localhost:5173` and nothing needs CORS:

```bash
SCCP_NODE=http://127.0.0.1:8650 npm run dev   # node somewhere else
npm run dev -- --port 5273                    # 5173 busy
npm run preview                               # serve the built dist on :4173
```

`npm test` runs the vitest suite and `npm run typecheck` the compiler; both are
CI gates, so run them before changing anything under `src/web`.

### 5.3 What there is to look at

| Route | Page |
| --- | --- |
| `#/` | Dashboard: height, tip, supply, recent blocks, the difficulty view |
| `#/blocks`, `#/blocks/<height\|hash>` | Block list and one block, including its transactions and proofs |
| `#/games`, `#/games/<game_hash>` | Games, with the board replay and the proof behind each one |
| `#/accounts/<address>` | Balance, nonce, bond, registration and transaction history |
| `#/miners/<address>`, `#/ratings` | Per-miner detail and the Glicko-2 table |
| `#/play` | A browser seat: derive a key from a seed, get funded, register, play |

On the chain from section 3.1 the game is at `#/games`, the block that settled it
is `#/blocks/4`, and every address in the report is a live account page.

### 5.4 `#/play` needs a relay, not just a node

The hand-play tab is the one page that talks to two services: the node for
balances, the faucet and registration, and the matchmaking relay for the seat
("no session lists this seat yet" means the relay has no epoch for it). Two
things are required:

- `sccp matchmaker run --config relay.json --origin http://127.0.0.1:8645`
  (repeat `--origin` for the Vite port too), because the relay sends no CORS
  headers by default;
- a build that knows where the relay is: `VITE_SCCP_RELAY=http://127.0.0.1:8646
  npm run build` (the default); a hosted build can proxy it under a path, e.g.
  `VITE_SCCP_RELAY=/relay`.

The relay the tab talks to must be on the same chain as the node. The repo
generates a relay config together with a testnet stack (`deploy/README.md`);
there is no scripted devnet relay config yet, which is why the rest of this
guide drives devnet play through `scripts/play_miner.py` instead.

The tab's **Fund** button is the node's faucet (`POST /v1/faucet`), the same
grant section 7 describes. Its seed is 32 bytes of hex, kept in memory only: a
refresh forgets it, which is the point. It derives both halves of the hybrid key,
so the tab registers the seat, commits and reveals availability, joins with a
hybrid start signature, signs moves with the Ed25519 half and co-signs the result
with both halves — every signature is accepted by the node and the relay as-is.
[`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) section 2 has the key details, and
`sccp wallet sign-result` is still there for a key that lives outside a browser.

---

## 6. Open a wallet on the devnet

A wallet is a hybrid keyfile: one 32-byte seed controls an Ed25519 key and an
ML-DSA-65 key, encrypted with scrypt (`N=2**17`) under your passphrase. That one
key signs moves, certificates, results and transactions, and the devnet address
is derived from its public half under the `devnet` network id.

### 6.1 Create one

```bash
export SCCP_WALLET_PASSPHRASE='devnet-passphrase'
sccp --network devnet --data-dir .devnet wallet new --out alice.key
```

```text
sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr
```

The command prints the address and writes the keyfile; nothing else is stored.
Notes that save time:

- **The passphrase must be at least 12 characters** — it protects both schemes.
  Without `SCCP_WALLET_PASSPHRASE` (or `--passphrase-file`, mode 0600, first line
  read) the CLI prompts for it twice and hides the input. Too short is a hard
  error: `error: a keyfile passphrase must be at least 12 characters`.
- `--force` overwrites an existing keyfile; there is no other way to.
- The address depends only on the seed and the network, so the same phrase on
  `testnet` gives a different address. That is why every command takes
  `--network devnet` explicitly.

Read it back without a passphrase (`wallet address` only decrypts the header):

```bash
sccp --network devnet wallet address --key alice.key
sccp --json --network devnet wallet address --key alice.key
```

```json
{"address": "sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr",
 "keyfile": "alice.key", "network": "devnet", "public_key": "015ae3a3...",
 "scheme": "ed25519-mldsa65"}
```

Back up and restore with the 24-word BIP-39 phrase from the same seed:

```bash
sccp --network devnet wallet export-mnemonic --key alice.key > alice.mnemonic
chmod 600 alice.mnemonic    # import-mnemonic refuses a group/other-readable file

sccp --network devnet wallet import-mnemonic --out alice-restored.key \
     --mnemonic-file alice.mnemonic
# sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr
```

`export-mnemonic` takes the passphrase the usual way and prints the phrase on
stdout (`--json` wraps it with the address, network and scheme).
`import-mnemonic` reads the phrase from `--mnemonic-file` — mode 0600, a trailing
newline or wrapped lines are fine — and asks only for the new passphrase, which
`--passphrase-file` or `SCCP_WALLET_PASSPHRASE` supplies too, so the whole
recovery can run unattended. Without the flag it prompts for the words with
hidden input, which needs a terminal: a piped phrase is not read, because the
hidden prompt goes to the controlling terminal. The restored keyfile holds the
same seed, so it is the same address.

[`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) is the long version of this section, and
[`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) the testnet-specific
one.

### 6.2 Read its balance from the node

```bash
sccp --network devnet wallet balance --key alice.key --node http://127.0.0.1:8645
```

```text
sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr 1000.00000000 (spendable 1000.00000000, bonded 0.00000000, immature 0.00000000)
```

`--json` names the source it used, which is the fastest way to prove which chain
answered:

```json
{"address": "sccpt1q82...", "bonded": 0, "immature": 0, "network": "devnet",
 "source": "http://127.0.0.1:8645", "spendable": 100000000000,
 "total": 100000000000}
```

Three balances, and they are not interchangeable: `spendable` is what a transfer
can use, `bonded` is locked by a miner registration, and `immature` is coinbase —
the 50-coin block subsidy matures after 20 blocks. An address that also mines
shows all three: `total 400.00002000 (spendable 250.00000000, bonded 0.00000000,
immature 150.00002000)` is three bootstrap subsidies plus two block fees still
maturing, and one 250-coin transfer already spendable.

Without `--node` the command falls back to `--data-dir`'s `chain.sqlite`, which is
what makes an offline build possible; if that file holds another chain, the
answer is another chain's answer. Always pass `--node` when a node is running.

### 6.3 Transaction history is local

`wallet history` has no `--node`: it reads the chain in `--data-dir`. Point it at
the node's data directory:

```bash
sccp --network devnet --data-dir .devnet wallet history --key alice.key
```

```text
     5 transfer        1000.00000000 sccpt1q9qn5m002xdltlvrkvargp8rampnufd0l35z3vlkzuxkajqphdjd6s5yqjt ae6c0fd4c5ab...
     6 transfer         -42.00001000 sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r 9a1cf6424e92...
```

Height, type, signed delta (fee included for the sender), counterparty, txid. Or
just open `#/accounts/<address>` in the explorer.

---

## 7. Fund a wallet from the devnet faucet

Devnet genesis allocates one million coins to a public, deterministic faucet key
— the `allocations` entry in `genesis/devnet.json`, and the same seed
`faucet_keypair("devnet")` derives in `sccp/chain/genesis.py`. Anyone can fund
any devnet address, and every node derives the same faucet.

### 7.1 The HTTP endpoint

```bash
curl -s http://127.0.0.1:8645/v1/faucet
```

```json
{"network":"devnet",
 "address":"sccpt1q9qn5m002xdltlvrkvargp8rampnufd0l35z3vlkzuxkajqphdjd6s5yqjt",
 "amount":1000,"max_amount":1000,"cooldown_seconds":3600,
 "client_window_seconds":3600,"client_grants":5}
```

```bash
curl -s -X POST http://127.0.0.1:8645/v1/faucet \
     -H 'content-type: application/json' \
     -d '{"address":"sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr"}'
```

```json
{"network":"devnet","address":"sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr",
 "amount":1000,"txid":"ae6c0fd4c5ab4c9c89a968d739ebd5c020eb0deeff2281d6cc48d0e9051992a6"}
```

An `amount` in whole coins (1 to `max_amount`) is optional; the default is 1000.
The limits are in memory, per node process:

- one grant per recipient address per hour, and
- five grants per client address per hour.

Asking twice from the same client is a 429 with a countdown:

```json
{"detail":"sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr already received a grant; try again in 3596 seconds"}
```

A restart forgets the counters, and the client limit is keyed on the TCP peer,
so a container, a reverse proxy or a VPN can all look like one client. This is a
test faucet, not a policy.

### 7.2 The operator path: `sccp faucet send`

The CLI holds the same public faucet key, so it can sign without any limits. It
is the escape hatch when the endpoint says "already received a grant" and you do
not want to wait, and it is how a script funds many addresses at once:

```bash
sccp --network devnet faucet send \
     --node http://127.0.0.1:8645 \
     --to sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r \
     --amount 5
```

```text
faucet sent 5.00000000 to sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r
txid: 76d0d4b5799398425661dc5fb1261cb2c8ce61605aed9d9c621591761e864120
```

`--amount` is in whole coins and defaults to 1000. There is no amount cap here —
the cap belongs to the HTTP endpoint.

### 7.3 The grant is not the balance

Both paths only *admit* the transfer to the node's mempool. The balance changes
when a block includes it:

```bash
curl -s http://127.0.0.1:8645/v1/mempool                 # waiting
curl -s http://127.0.0.1:8645/v1/accounts/<address>      # settled
```

```json
{"network":"devnet","address":"sccpt1q82apcjl8txcz7qkf2s966p6s4thg7kjf736cd37acxra828sa757r6geqr",
 "balance":100000000000,"immature":0,"bonded":0,"total":100000000000,"nonce":0,
 "registered_until_epoch":null,"endpoint":null,"engine":null,"public_key":null,
 "banned_until":0}
```

> **Nothing settles without a miner.** On a node started without `--mine` a grant
> sits in the mempool and every balance stays zero. The devnet answer is the
> heartbeat block from section 4: `--mine` on the node makes one per pending
> ledger transaction. If a grant seems to vanish, check `/v1/mempool` first.

---

## 8. Send coins on the devnet

Create a second wallet so there is somebody to pay:

```bash
export SCCP_WALLET_PASSPHRASE='devnet-passphrase'
sccp --network devnet --data-dir .devnet wallet new --out bob.key
# sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r
```

Fund it (section 7), then send 42 coins from `alice.key`:

```bash
sccp --network devnet wallet send --key alice.key \
     --to sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r \
     --amount 42 --node http://127.0.0.1:8645
```

```text
sent 42.00000000 to sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r (fee 0.00001000)
txid: 9a1cf6424e92d656cd05cb02a3c7631f4e1cb8c92ca984b2e8d8bd18d8c7b885
```

What happens under the hood, in the order the CLI does it:

1. it reads the sender's balance and nonce **from the node** (`--node`), because
   a transfer is signed against the account state, not against bytes on disk;
2. it builds the transfer — `--amount` in whole coins, `--fee` in base units
   (default: the network minimum, 0.00001 coins) — and signs it with the keyfile;
3. it `POST`s it to `/v1/transactions`, which answers `{"accepted": true, ...}`
   and holds it in the mempool. With `--json` you get one object:

   ```json
   {"txid": "9a1cf6424e92...", "network": "devnet", "type": "transfer",
    "from": "sccpt1q82...", "to": "sccpt1q9jyt...", "amount": 4200000000,
    "fee": 1000, "nonce": 0, "accepted": true, "source": "http://127.0.0.1:8645"}
   ```

Watch it settle — the recipient's balance appears one block later:

```bash
sccp --network devnet wallet balance --key bob.key --node http://127.0.0.1:8645
# sccpt1q9jyt7pkyua8hvuqn3y5nfhl4cxxq3wdk7w8elmkjd5nldeqnl9pvtxs58r 42.00000000 (spendable 42.00000000, bonded 0.00000000, immature 0.00000000)

sccp --network devnet --data-dir .devnet wallet history --key alice.key
#      6 transfer         -42.00001000 sccpt1q9jyt... 9a1cf6424e92...
```

and the same transfer is a row on `#/accounts/<alice-address>`, with the block
that carried it linked from there.

Practical notes:

- An address that also mines sees most of its coins as `immature` for 20 blocks
  and cannot spend them yet. `--amount` larger than `spendable` fails before
  anything is signed, and the error names the two numbers.
- A transfer pays from one nonce, so two `wallet send` commands fired at the
  same instant can collide on it. Send them one after another and the second
  reads the fresh nonce from the node.
- `--fee` above the minimum matters only when several transactions are waiting:
  a heartbeat block takes exactly one, and the mempool picks by fee. Fees go to
  the block's miner — the `--miner-address` on this chain — while a proofless
  block mints no subsidy at all; the 50 coins come with proofs and with the
  bootstrap blocks. Either way the credit lands in `immature` for 20 blocks.
- The keyfile must belong to `devnet`. A testnet keyfile plus `--network devnet`
  is refused with the two network ids in the message.

---

## 9. Run a miner on the devnet

A miner is a keyfile, an engine, an endpoint and an availability commitment. On
devnet the bond is zero and an epoch is one block, so the whole
register → commit → reveal → pair → play → settle cycle fits in a handful of
blocks instead of a few minutes.

### 9.1 The whole loop in one process

```bash
sccp devnet up --miners 4 --blocks 10
```

This is the reference run for miner behaviour: four keyfiles are funded from the
faucet, registered with `MockEngine` fingerprints, committed and revealed for the
epochs that will pair, matched by beacon through the relay, played to a decisive
result, co-signed, and the winning proofs are mined into blocks by the production
`BlockBuilder`. Every step is the same code path the CLI uses; nothing is stubbed
except the engine.

Use it as a regression test: the run is deterministic, and `--json` gives
`height`, `tip_hash`, `epochs`, `matches`, `proofs` and `balances` to assert on.

### 9.2 You against a real engine miner, on a chain you keep

[Section 3.1](#31-with-a-real-game-in-it-recommended) is the miner guide for
most purposes: `play_miner --on-chain` runs a real `EngineMiner` (bond,
availability, certificate, moves, result co-signature) in the same process as
your seat, and mines the finished game's proof into `.devnet`. To make it a
*strong* opponent:

```bash
python scripts/fetch_engine.py                     # ./bin/stockfish, pinned + hashed
python scripts/play_miner.py --on-chain --network devnet --chain-dir .devnet \
    --engine stockfish-19 --engine-path ./bin/stockfish --color auto
```

`--engine-seed 7` makes the mock opponent reproducible, `--games 3` plays a
series against the same miner, and `--ascii` prints the board with ASCII
instead of box-drawing characters.

### 9.3 A relay and a miner as separate processes

Both services exist as standalone commands:

```bash
# 1. a relay for one devnet epoch
sccp --network devnet matchmaker run --config relay.json --port 8646 \
     --origin http://127.0.0.1:8645 --node http://127.0.0.1:8645

# 2. a miner that joins whatever the relay assigns and keeps its own availability
#    committed for every epoch
sccp --network devnet miner run --key miner.key \
     --relay http://127.0.0.1:8646 --node http://127.0.0.1:8645 \
     --nonce-secret <64 hex chars> \
     --engine-profile stockfish-19 --engine-path ./bin/stockfish
```

`relay.json` is a small document — `network`, a `beacon`, a `roster` of addresses
with their public keys and engine fingerprints, and optional `timeout_seconds` /
`round_seconds` / `open_round` — and a running node can hand you all of it for
the epoch you want:

```bash
curl -s http://127.0.0.1:8645/v1/epochs/4 | python -c '
import json, sys
view = json.load(sys.stdin)
json.dump({"network": view["network"], "beacon": view["beacon"],
           "roster": view["roster"]}, sys.stdout, indent=2)' > relay.json
```

That is enough to start a relay against a devnet chain:

```text
matchmaker on 127.0.0.1:8646 for devnet epoch 4 (0 matches, round None, witness none)
```

With `--node` the relay follows the chain from there: each epoch the chain opens
replaces the beacon and the roster, so matches always reference the epoch whose
proofs the chain will accept. `--witness-key` gives the relay a keyfile so its
clock flags are witnessed on chain; `--round-seconds` and `--open-round` add the
round schedule. Passing `--network devnet` is not cosmetic: the config's network
id and the flag must agree or the relay refuses to start.

What is still missing for a *two-process* devnet match is the roster: pairing
needs two miners whose availability was committed and revealed for that epoch,
and the keys that did it. `deploy/init_devnet_stack.py` and
`deploy/bootstrap_devnet_stack.py` write exactly that — the devnet counterpart of
the testnet pair — and they do not need a running node:

```bash
python deploy/init_devnet_stack.py --out .devnet-stack
# stack for devnet in .devnet-stack
#   miner-1: sccpt1q8exuw4cdf06c5d79ma9kcxv3l3ullgrlzgj77hw3lye3xcqr4ex5ulc77z
#   miner-2: sccpt1q8f7qfeke79pnzyhe5g58jrry0dttzt2h5qkmzzvsddcdqjgjsmezkx6u32

python deploy/bootstrap_devnet_stack.py --stack .devnet-stack
# built 3 bootstrap blocks: height 3, tip 32c7b1b90f08...
# relay config for epoch 3 written to .devnet-stack/relay.json
```

The second command prints the node, relay and miner commands for those files; it
is the same three-block bootstrap `sccp devnet up` runs in process (fund,
register, commit, reveal), written to `.devnet-stack/chain/chain.sqlite` and
validated by the same `NodeService`. The chain stops at height 3, the last block
of the bootstrap window: epoch 3 is matchable and epoch 4's commit and reveal are
already on chain. Re-running the script leaves an existing chain alone; `--fresh`
rebuilds it and never touches the keys. The keyfiles, the nonce secrets and the
passphrase are fixed and published, so `docker compose`-style reproducibility
applies here too — devnet coins have no value, and none of these keys may ever be
reused on another network.

With no bootstrap script, register your own keys with the pipeline below and
give the relay a roster that has them.

### 9.4 The pipeline, step by step

Each command builds a transaction from the local chain in `--data-dir` and
submits it to `--node`, so the two must be the same chain:

```bash
export SCCP_WALLET_PASSPHRASE='devnet-passphrase'

# 1. announce the miner: the endpoint the relay dials, the engine fingerprint
#    and the bond. Devnet's minimum bond is zero, so --bond 0 is enough
#    (testnet wants 10).
sccp --network devnet --data-dir .devnet miner register --key miner.key \
     --endpoint tcp://127.0.0.1:7000 --engine mock-v1:0123456789abcdef --bond 0 \
     --node http://127.0.0.1:8645
# registered sccpt1q82apc... with a 0.00000000 bond
# txid: 8230ceb3f3dc...

# 2. commit availability; keep the printed secret.
sccp --network devnet --data-dir .devnet miner availability --key miner.key \
     --max-games 3 --node http://127.0.0.1:8645
# committed availability for epoch 14 (3 games)
# txid: a9b8fe5d9c92...
# nonce secret (needed for the reveal): 8cb2bea4c3c0...
# reveal by tip 12 (block 13 is the last of epoch 13), or the commitment is void

# 3. once that commit is in a block, reveal with the same secret.
sccp --network devnet --data-dir .devnet miner reveal --key miner.key \
     --nonce-secret 8cb2bea4c3c0... --node http://127.0.0.1:8645
# revealed for epoch 14
# txid: 03aae20bb635...
```

The fingerprint is the engine's identity, not a label you invent: for a UCI
binary it is the SHA-256 of the executable, and for the mock engine it is
`mock-v1:<16 hex>` (PROTOCOL.md section 5). Whatever is registered must match the
fingerprint the miner later claims in its match certificates.

> **The reveal window is one block wide on devnet.** `miner availability` targets
> `epoch(next block) + availability_lead` (2), while `miner reveal` targets
> `epoch(next block) + 1`. With one-block epochs that means the reveal has to be
> submitted after the commit's block and before the next one — there is exactly
> one chance. The commit prints that deadline (`reveal by tip 12` above, and
> `reveal_by_tip` in `--json`), so send the reveal as soon as the commit is in a
> block, or let a long-running `sccp miner run --nonce-secret` do the pair of
> transactions for you every epoch. The failure looks like this, and means the
> chain has moved past the epoch:
> `transaction rejected (registration): <address> committed no availability for
> epoch N`.

Two more limits worth knowing: `--max-games` cannot exceed the epoch quota, which
is `rounds_per_epoch` — 3 on devnet and testnet, 5 on mainnet — so
`--max-games 4` is rejected with `max_games 4 exceeds the 3-game epoch quota`; and
a registration is refused for a bond below `min_bond` (`register needs a bond of
at least <min_bond>, got <bond>`), for an address banned until a later height, or
for a balance that cannot cover the bond plus the fee.

`sccp miner run` does not register anything: given `--nonce-secret` (or
`--nonce-secret-file`) it commits and reveals availability by itself every epoch,
which is what a long-running miner wants; the other flags worth knowing are
`--max-games-per-epoch`, `--nodes-per-move` and `--depth` for search policy,
`--resign-after-plies` for a weak engine that would otherwise lose on time, and
`--relay-witness-key` to co-sign relay clock flags.

[`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) walks the same pipeline with
the testnet numbers and a relay config written by the deploy scripts.

---

## 10. Reset, and troubleshooting

### 10.1 Start over

A devnet is cheap to rebuild: the genesis document is fixed, so a fresh directory
produces the same chain from the same commands.

```bash
# stop the node, the relay, and anything else you started (Ctrl+C, or:)
pkill -f 'sccp .*node run'
pkill -f 'matchmaker run'

# throw the chain away — chain.sqlite is the whole state
rm -rf .devnet

# and bootstrap again
sccp devnet up
```

`.devnet/` is **not** in the repository's `.gitignore` (it ignores `data/*.sqlite*`
and `games/`), so either add it to your local ignore file or keep the data
directory outside the checkout. Keyfiles are only as safe as the directory you
put them in.

Neither bootstrap has to be reset by hand: `sccp devnet up` never writes a data
directory, and `scripts/play_miner.py --on-chain --fresh` discards whatever chain
is in `--chain-dir` and starts again from genesis. The stack of 9.3 does the same
with its own flag: `--fresh` on `deploy/bootstrap_devnet_stack.py` deletes
`.devnet-stack/chain/chain.sqlite` (never the keyfiles) and rebuilds the same
three blocks, so the relay config it rewrites matches.

### 10.2 When something looks wrong

| What you see | Why | What to do |
| --- | --- | --- |
| `sccp: command not found` | the virtualenv is not active in this shell | `source .venv/bin/activate`, or call `.venv/bin/sccp` |
| `error: a keyfile passphrase must be at least 12 characters` | passphrase policy for hybrid keys | use 12+ characters in `SCCP_WALLET_PASSPHRASE` or `--passphrase-file` |
| `keyfile belongs to network 'testnet', but --network 'devnet' was requested` | the keyfile records its network | use the keyfile's network, or create a devnet key |
| `error: database .../chain.sqlite holds devnet, not testnet` | `--network` and `--data-dir` disagree | pass `--network devnet`, or point at a devnet directory |
| height stuck at 3, balances 0, `/v1/mempool` not empty | nothing mines: bootstrap is over and no block builder is running | start the node with `--mine` |
| `cannot read <address> from the node at <url>: it did not answer` | nothing is listening on that port — the node stopped, or never started | start `sccp node run --port <port>`, or fix `--node` |
| `429 ... already received a grant; try again in N seconds` | one grant per address per hour | wait, fund another address, or use `sccp faucet send` |
| `not mined: 4 plies is below devnet's 20-ply proof minimum` | the game was too short to prove | play at least 20 plies; a draw is never provable |
| `no built explorer found; run npm install && npm run build in src/web or point SCCP_WEB_DIST at a directory holding index.html` | `--web` with no bundle (exit 2) | build the explorer, or point `SCCP_WEB_DIST` at a `dist` with `index.html` |
| `Address already in use` on 8645/8646 | the `deploy/` compose stack is running | `--port 8650` and `SCCP_NODE=http://127.0.0.1:8650 npm run dev`, or `docker compose down` |
| the explorer loads but every panel errors | a node or relay from an older build is answering | `curl /v1/status`; rebuild the container image (`docker compose up --build`) |
| `sccp miner run` fails parsing a relay reply (`string_pattern_mismatch`, missing `move_commitment`) | the relay predates hybrid signatures | point `--relay` at a current relay; rebuild the old one |
| `No such option: --node` on `wallet history` | that command reads the local chain, not a node | pass `--data-dir <node data dir>` instead |
| `import-mnemonic` waits forever with no prompt on screen | the hidden prompt reads the controlling terminal, so a piped phrase is never read | pass `--mnemonic-file <file>` (mode 0600), plus `--passphrase-file` for the new passphrase |
| `committed no availability for epoch N` on a reveal | the reveal missed its window (`reveal by tip` in the commit output) | reveal in the next block, or let `sccp miner run --nonce-secret-file` commit and reveal both |
| `the chain is at height N, below the bootstrap tip 3` from the devnet bootstrap | a chain directory holds a partial or foreign chain | `python deploy/bootstrap_devnet_stack.py --stack .devnet-stack --fresh` |

Global options go **before** the subcommand: `sccp --json chain info`,
`sccp --data-dir .devnet wallet balance ...`, `sccp --network devnet miner run
...`. Typer accepts them after a subcommand only where the command redeclares
them, which is why the examples above look inconsistent at a glance.

---

## 11. Where to go next

- [`PLAY_AND_EXPORT.md`](PLAY_AND_EXPORT.md) — play on the devnet chain, then
  export every game in a block as PGN with `scripts/export_block_games.py`.
- [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — keyfiles, the 24-word backup, the
  hybrid signing rules and `sccp wallet sign-result`.
- [`SEND_COINS.md`](SEND_COINS.md) and
  [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — the same flows with
  testnet addresses and numbers.
- [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — the two-process stack: a
  relay config, two engine miners, availability over many epochs.
- [`EXPLORE_THE_CHAIN.md`](EXPLORE_THE_CHAIN.md) — the explorer, the API and the
  deploy story behind it.
- [`PROTOCOL.md`](PROTOCOL.md) — what the blocks, proofs, beacons and rosters in
  all of the above actually mean; section 13 is the API reference.
