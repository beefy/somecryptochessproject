# somecryptochessproject

**Chess as proof of work.**

A cryptocurrency prototype in which the scarce resource is *winning chess games
against another miner*, not SHA-256 hashing. Two adversarial chess engines are
paired by a public randomness beacon, play one game, and only the winner gets
credit. A miner must collect several verified wins - from different opponents and
featuring never-before-mined games - before it may produce a block and claim the
reward.

> **Status: protocol v1 is implemented and deployed on testnet.** The repository
> contains the complete path from wallet to block: PGN validation and canonical
> game hashing; commit/reveal beacons, deterministic pairing, signed match
> certificates and the timed relay; the account ledger, block validation, fork
> choice, difficulty, SQLite storage and block builder; the node API, gossip and
> pull-sync; the unattended engine miner; quality sampling and bonded
> challenges; and a funded two-miner testnet with a public faucet, automated
> deployment and operator runbooks. Normative formats are in
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md); next steps and improvement areas are
> in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md). Mainnet
> parameters exist, but no mainnet has launched or been audited; testnet and
> devnet coins have no value.

> **Public testnet:** <http://testnet.somecryptochessproject.com:8645>. The
> `GET /v1/status` endpoint is the health check. The chain is reset on every
> deployment, so wallets, balances and faucet grants do not survive a redeploy.
> This endpoint is plain HTTP and carries worthless test coins only.

> **The relay runs timed sessions.** Every session is a real **30+0** clock game:
> the second start signature starts both clocks, the relay's monotonic clock is
> authoritative (a move's `clock_ms` is display data, never trusted), each
> accepted move is charged to the mover and answered with a signed receipt, and a
> clock that reaches zero auto-flags the match even if the losing seat never
> answers. Those receipts are exactly the evidence a bonded `timeout_claim`
> settles on chain (`src/sccp/protocol/clocks.py`, `src/sccp/chain/timeout.py`):
> the winner pulls `GET /v1/matches/{id}/receipts`, verifies it against the
> relay's witness key it was configured with, and the miner refuses to build a
> claim it cannot reproduce. `sccp matchmaker run --witness-key` signs the
> receipts, `--round-seconds` adds the opt-in round scheduler, and
> `sccp miner run --relay-witness-key` trusts that witness. The formats are
> normative in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) section 8.4 and 10.4. The
> ledger rules, clock evidence and local claim verification are implemented; the
> remaining integration gap is automatic submission of a timeout claim from the
> demo/CLI path.

> **Naming.** The repository and project name is `somecryptochessproject`. The
> Python package and console command use the short form `sccp` (import `sccp`,
> run `sccp ...`), because the full name is impractical for imports and shell
> commands. If you prefer the full name everywhere, that is a mechanical rename.

---

## 1. The idea

Hash-based proof of work is a lottery: miners spend electricity on a computation
that has no use outside of securing the chain. somecryptochessproject keeps the lottery but
makes the lottery ticket a *game of chess*.

- A **miner** runs a real UCI chess engine (Stockfish by default).
- A **matchmaker** pairs two miners using a randomness beacon that nobody can
  predict or bias.
- The two engines play a normal, legal game of chess from a beacon-selected
  opening.
- The **winner** earns one "win credit". The loser earns nothing.
- A miner that accumulates `k` win credits (from distinct opponents, all games
  unique and unexpired) can assemble them into a **block** and claim the block
  reward.

No new chess logic is written for this project. Board state, move generation,
legality, SAN/PGN parsing and UCI engine control all come from
[`python-chess`](https://python-chess.readthedocs.io/). The novel parts are the
matchmaking protocol, the proof format, the anti-collusion rules and the chain.

### What this is

- A research prototype exploring whether a useful, adversarial computation can
  secure a chain.
- A testbed for verifiable random matchmaking and commit-reveal beacons.
- A source of real, human-reviewable chess games.

### What this is not

- A drop-in replacement for Bitcoin, or a claim that chess is a cryptographic
  verifiable delay function.
- A chess GUI, a chess engine, or a chess rating system.
- Anonymous: games and payouts are public and linkable.
- Audited. Do not put real money anywhere near it.

---

## 2. Roles and moving parts

| Role | What it does | Trust required |
| --- | --- | --- |
| **Wallet** | Ed25519 keys, bech32 addresses, signs transactions and match transcripts | none |
| **Engine miner** | Registers, commits availability, connects to a relay, plays assigned games with a UCI engine, submits win proofs | none for correctness; it only hurts itself by cheating |
| **Matchmaker / relay** | Runs epoch rounds, derives pairings from the beacon, relays moves, collects co-signatures, stores match transcripts | **untrusted**: any node can recompute the pairing and verify the transcript |
| **Full node** | Gossips blocks and transactions, validates the chain, serves the HTTP API | none |
| **Verifier** | Part of every full node: checks signatures, pairing, PGN legality, uniqueness, claim rules - and samples game quality | none; quality checks are fraud-proof based, not consensus-critical |

A matchmaker is *not* a trusted third party. It is a convenience service: it
cannot invent a pairing, change an opening, forge a move or fabricate a result
without the resulting proof failing verification.

---

## 3. The protocol at a glance

```mermaid
sequenceDiagram
    participant A as Miner A (engine)
    participant R as Relay / matchmaker
    participant B as Miner B (engine)
    participant N as Full nodes

    A->>N: RegisterMiner(bond, endpoint, engine fingerprint)
    B->>N: RegisterMiner(bond, endpoint, engine fingerprint)
    A->>N: Availability(epoch e, commit = H(nonce))
    B->>N: Availability(epoch e, commit = H(nonce))
    Note over N: Beacon B_e = H(prev block hash, valid reveals, drand)
    R->>A: Pairing for round r (derived from B_e)
    R->>B: Pairing for round r (derived from B_e)
    R->>A: Seat + opening line (derived from B_e)
    R->>B: Seat + opening line
    A<->R<->B: Moves relayed, both sides sign the transcript
    A->>N: WinProof(winner=A, signed game + match certificate)
    Note over A: Repeat until A holds k wins from distinct opponents
    A->>N: Block(k proofs, transactions, seal)
    N->>N: Verify signatures, pairing, PGN legality, uniqueness, claim window
    N->>N: Award subsidy + fees to A (coinbase, mature after N blocks)
```

### 3.1 Unique games

Every proof carries a **canonical game hash** computed over
`(network, start position, SAN move list, result)` - deliberately *excluding*
PGN headers, comments, clocks and formatting. Nodes maintain a registry of every
game hash that has ever appeared in the canonical chain and reject a proof whose
hash is already known. The same moves with different metadata are the same game;
a transposition with different moves is allowed. The registry is rebuilt on
reorganisation, so games orphaned by a reorg can be mined again.

### 3.2 Unpredictable, unbiasable matchmaking

- Miners commit to a random nonce in epoch `e-2` and reveal it in epoch `e-1`.
- The epoch beacon is derived from the last block of `e-1` (`H_e`) and the valid
  reveals, and optionally mixed with one drand signature:
  `digest = SHA-256("sccp/v1/beacon" || network || e || block_hash(H_e)
  || sorted(commitment || nonce))`, then `B_e = SHA-256(digest || drand_sig)`
  when `drand_used = 1`, else `B_e = digest`. The first block of epoch `e`
  declares `B_e` and (on mainnet) carries the signature; `drand_used = 0` is
  always valid, so a drand outage cannot halt the chain.
- Pairings are a **deterministic function** of the active roster and `B_e`:
  every miner is ranked by `H("sccp/v1/pair", {network, epoch, round, beacon,
  address})`, the sorted list is paired adjacent, and colour plus opening line
  are drawn from the same beacon.
- Because the beacon is unknown when a miner commits, nobody can choose an
  opponent. Because pairing is deterministic, any node can verify that a claimed
  match was the one the protocol assigned. A relay that deviates produces proofs
  that fail validation.

### 3.3 Self-play, Sybils and collusion

Self-play is the central economic attack: a miner that controls both seats always
wins. The project does not pretend to solve Sybil resistance cryptographically;
it uses layered economic and structural defences:

1. **Registration bonds.** Each identity locks a bond larger than the expected
   value of a claim. Proven equivocation or a successful collusion fraud proof
   slashes it.
2. **Pairing constraints.** The pairing algorithm refuses (where possible) to
   match two identities that share an endpoint, /24 prefix, ASN or registration
   funder; it also forbids rematches inside a window.
3. **Quality requirements.** Both sides' moves are checked against a pinned
   reference engine. Playing a deliberately weak "loser" is detected as
   centipawn loss, so a self-playing miner must run two strong engines - roughly
   the same cost as playing honestly with one engine.
4. **Co-signatures and bonds.** The loser must sign the finished transcript to
   complete a proof. Refusing to co-sign forfeits the match bond and
   availability slots.
5. **Sampling and challenges.** Quality verification is expensive, so it is
   sampled and backed by a bonded challenge window rather than run inline by
   every node.

The honest summary: chess PoW proves that *a strong engine played a legal,
previously unseen game against another signing identity*. It does not
cryptographically prove that the two identities are independent. The gap is
closed by cost, bonding and detection, not by mathematics.

### 3.4 A legal PGN is not proof of work

Anyone can write down a legal 4-move checkmate. Legality alone is worthless as
work. A valid proof therefore also requires:

- a **match certificate** signed by both seats, binding the game to a pairing and
  opening that the beacon actually produced;
- a **quality report**: every move is replayed against a pinned reference engine
  and must stay within a centipawn-loss budget (with exemptions for already
  decided positions);
- a decisive result: checkmate, or a resignation where the reference engine
  agrees the position is lost;
- registration, bond and claim-window checks.

Quality checking costs roughly the same as playing the game, so it is sampled
(plus challengeable) rather than repeated by every node on every proof.

### 3.5 Draws

Most games between equal strong engines are draws. Draws earn no credit and do
not produce a proof. The matchmaker simply schedules another round with a new
opening against a new opponent. Difficulty retargeting accounts for the observed
draw rate, and a miner's reward variance is expected to be high. Pools are
future work.

### 3.6 Difficulty

Two knobs, both consensus parameters:

- **`k`, required wins per claim** - coarse adjustment, retargeted from observed
  block intervals.
- **Seal difficulty** - a conventional hash target on the block header, used as
  a fine-grained rate limiter and spam guard, retargeted like Bitcoin.

`nodes_per_move` is pinned by the network's engine profile and the opening book
version is fixed per epoch, so every miner's expected work is identical.

---

## 4. Networks

Two isolated networks are built from day one. Network id is mixed into every
hash and signature, so testnet transactions and proofs are invalid on mainnet.

| Parameter | `testnet` | `mainnet` |
| --- | --- | --- |
| Network id / address HRP | `sccpt` | `sccp` |
| Genesis | `genesis/testnet.json` | `genesis/mainnet.json` |
| Target block time | 60 s | 600 s |
| Wins per claim (`k`) | 1 | 3 (retargeted 1..10) |
| Header seal difficulty | 0 bits | 16 bits (retargeted) |
| Block subsidy | 50 coins | 50 coins, halves every 210,000 blocks |
| Max supply | none (test coins) | 21,000,000 coins |
| Coinbase maturity | 20 blocks | 100 blocks |
| Registration bond | 10 coins | 500 coins |
| Claim window | 64 blocks | 144 blocks |
| Game length | 20-300 plies | 30-400 plies |
| Engines allowed | any UCI engine, incl. mock | pinned Stockfish builds |
| Quality checks | off (fast local iteration) | sampled + challengeable |
| Beacon | commit-reveal | commit-reveal; optional drand mix-in (pinned verifier pending) |
| Minimum fee | 1,000 base units | 1,000 base units |

`devnet` (in-process, mock engines, zero seal difficulty) exists for tests and CI.
All values above are provisional and live in `src/sccp/config.py`.

---

## 5. Repository layout

Every module below ships in the 0.13.0 tree. Comments name the module's main
responsibility rather than a construction phase.

```text
.
├── README.md
├── LICENSE                       # Unlicense (public domain)
├── pyproject.toml                # packaging, deps, ruff/mypy/pytest config
├── Makefile                      # test / lint / typecheck / devnet / stack / dist
├── Dockerfile                    # image used by every stack service
├── docker-compose.yml            # node + relay + bootstrap + 2 Stockfish miners
├── .github/workflows/ci.yml      # ruff + mypy + pytest (3.11–3.13); web (22, 24)
├── .github/workflows/deploy-testnet-miners.yml  # image + hosted testnet deploy
├── docs/
│   ├── IMPLEMENTATION_PLAN.md    # next steps and improvement areas
│   ├── CREATE_A_TESTNET_WALLET.md # create a keyfile, call the faucet, check it
│   ├── EXPLORE_THE_CHAIN.md      # build the web explorer and play a real seat
│   ├── GCP_RESOURCE_SETUP.md     # project, VM, firewall and deploy wiring
│   ├── OPEN_A_WALLET.md          # create, back up and inspect a keyfile
│   ├── PLAY_AND_EXPORT.md        # play on-chain, then export the block's games
│   ├── PROTOCOL.md               # normative wire formats and consensus rules
│   ├── RUNBOOK.md                # restart, faucet, mining, incidents
│   ├── TESTNET_DEPLOYMENT.md     # CI deployment and monthly cost estimate
│   ├── RUN_A_TESTNET_MINER.md    # operator guide: relay, miner and registration
│   ├── SEND_COINS.md             # fund a wallet and send a transfer
│   └── THREAT_MODEL.md           # adversary model and residual risk
├── data/
│   ├── openings.json             # curated opening lines, shipped in the wheel
│   └── engines.json              # pinned Stockfish URLs + SHA-256 per platform
├── deploy/
│   ├── README.md                 # what the compose stack does and does not do
│   └── init_testnet_stack.py     # deterministic stack keys + relay config
├── genesis/{devnet,testnet,mainnet}.json
├── scripts/
│   ├── fetch_engine.py           # verified Stockfish download
│   ├── make_genesis.py
│   ├── make_openapi_fixture.py   # regenerate the API contract snapshot
│   ├── play_miner.py             # play a timed relay match by hand
│   ├── export_block_games.py     # export a block's games as PGN + manifest
│   └── check_release_version.py  # pyproject/version.py/tag agreement
├── src/sccp/
│   ├── config.py                 # network parameters, settings
│   ├── serialization.py          # canonical JSON, domain-separated hashing
│   ├── crypto/                   # keys, bech32 addresses, encrypted wallets
│   ├── chess/                    # pgn, openings, engines, gameplay, quality
│   ├── protocol/                 # models, beacon, pairing, transcripts,
│   │                             # verification, challenges and panels
│   ├── chain/                    # tx, blocks, state, mempool, fork choice,
│   │                             # storage, difficulty, timeouts, genesis
│   ├── node/                     # FastAPI app, gossip, services, faucet,
│   │                             # proof settler, quality sampler
│   ├── miner/                    # matchmaker/relay, block builder, engine miner
│   ├── wallet/                   # key files and wallet service
│   ├── transport.py              # synchronous ASGI transport for httpx
│   ├── devnet.py                 # in-process local network
│   └── cli.py                    # Typer CLI
├── src/web/                      # explorer: React Native for Web + Material
│   ├── README.md                 # build it, serve it, run the relay, play a seat
│   ├── src/views/                # overview, blocks, games, accounts, table
│   └── src/lib/                  # node/relay clients, protocol ports, tx signing,
│                                 # certificates, proofs, verifier, hash vectors
└── tests/
    ├── unit/                     # per-module tests, including test_api.py,
    │                             # test_gossip.py, test_sync.py, test_miner_loop.py
    ├── property/                 # hypothesis
    ├── integration/              # Stockfish smoke test (-m engine)
    ├── e2e/                      # test_local_network.py (real loopback ports)
    ├── adversarial/              # cheat / self-play / tamper suites
    └── fixtures/                 # PGN/hash vectors, crypto vectors, openapi.json
```

---

## 6. Quickstart

Every step below is a thin wrapper over a library call, so the CLI, tests and
services exercise the same code path.

The hosted two-miner testnet is available at
`http://testnet.somecryptochessproject.com:8645`. The quickstart below starts
local processes so you can change code without depending on it; use the hosted
URL as `--node` or `SCCP_NODE` when you want the shared deployment instead.

```bash
git clone git@github.com:beefy/somecryptochessproject.git
cd somecryptochessproject
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create a wallet (writes an encrypted keyfile)
sccp wallet new --network testnet --out alice.key

# Download a pinned Stockfish (verifies the archive SHA-256 first)
python scripts/fetch_engine.py --list
python scripts/fetch_engine.py --engine stockfish-19 --dest ./bin

# Verify a PGN offline: replays every move and prints the canonical game hash
sccp verify pgn game.pgn --network testnet

# Run the matchmaking relay (beacon + roster + engine fingerprints in relay.json).
# --witness-key makes it sign the clock receipts; --round-seconds opens a round
# on that interval (0 or omitted disables the scheduler); --node makes it move
# onto each later epoch's beacon/roster as the chain opens it.
sccp matchmaker run --config relay.json --store ./relay-state.json \
    --witness-key relay.key --round-seconds 900 --node http://127.0.0.1:8645

# Verify a win proof offline against a public context document
sccp verify proof proof.json --context context.json --network testnet

# Spin up a node, a relay and four mock miners, and mine ten blocks
sccp devnet up --miners 4 --blocks 10 --engines mock
sccp --json devnet up --miners 2 --blocks 4

# Serve a full node (chain in ./data/chain.sqlite, API on port 8645)
sccp --data-dir ./data node run --network testnet --port 8645

# Read that node's chain from the same data directory
sccp --data-dir ./data chain info --network testnet
sccp --data-dir ./data chain blocks --limit 5 --network testnet
sccp --data-dir ./data chain account sccpt1... --network testnet
sccp --data-dir ./data chain verify --network testnet

# Fund this wallet from the public faucet and check its balance. Both read the
# account from the node: no sync and no --data-dir needed.
sccp faucet send --network testnet --to sccpt1... --amount 1000
sccp wallet balance --network testnet --key alice.key

# history walks the adopted chain, so it still reads the data directory
sccp --data-dir ./data wallet history --network testnet --key alice.key

# Register the wallet as a miner and commit availability for the epoch ahead
sccp --data-dir ./data miner register --network testnet \
    --key alice.key --bond 10 --endpoint tcp://alice.example:8646
sccp --data-dir ./data miner availability --network testnet \
    --key alice.key --max-games 3
sccp --data-dir ./data miner reveal --network testnet \
    --key alice.key --nonce-secret <the-hex-from-the-previous-step>

# Run an engine miner against a relay (repeat for bob.key, with another engine).
# --relay-witness-key is the relay's witness public key (hex): with it the miner
# verifies the relay's clock receipts and can settle a flag as a timeout claim.
# --nonce-secret-file makes the miner commit and reveal its own availability each
# epoch, so the roster keeps growing without a manual `miner availability` run.
sccp miner run --network testnet \
    --key alice.key \
    --nonce-secret-file alice.nonce \
    --engine-path ./bin/stockfish-macos-universal \
    --relay http://127.0.0.1:8646 \
    --node http://127.0.0.1:8645 \
    --relay-witness-key <relay-witness-public-key-hex>

# The whole testnet stack (node + relay + bootstrap + two Stockfish miners)
docker compose up --build

# Play a timed relay match yourself, by hand: solo mode builds the relay and one
# mock miner in-process, so the clocks, receipts and auto-flag are the real ones.
# Solo is an off-chain protocol demo: no node, no proof, no block.
python scripts/play_miner.py                     # you are white; SAN or UCI moves
python scripts/play_miner.py --pgn-out game.pgn   # also keep the relay's PGN
python scripts/play_miner.py --color black --engine stockfish-19 \
    --engine-path ./bin/stockfish-macos-universal
# …or build/reuse a devnet chain, play on it and mine the win proof into a block.
# The game is written to ./data-play/chain.sqlite; serve it afterwards with
# `sccp --data-dir ./data-play node run --network devnet`.
python scripts/play_miner.py --on-chain
python scripts/play_miner.py --on-chain                 # re-run: resumes that chain
python scripts/play_miner.py --on-chain --games 3       # three games, three blocks
python scripts/play_miner.py --on-chain --chain-dir ./my-play-data --color auto
python scripts/play_miner.py --on-chain --fresh         # discard that chain
# …or take a seat on a relay that is already serving, with your own keyfile
python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key

# Export every game in a node's latest block as PGN, with a manifest.json.
# The exporter reads the node API, so start `sccp node run` in another terminal
# first; it must serve the same chain you played on (`--data-dir ./data-play
# --network devnet`). Without `--block` it asks that node for its tip, so a
# node left on another chain exports that chain's genesis instead. `--block`
# may name any other height or hash, e.g. block 5 for the second game.
python scripts/export_block_games.py --node http://127.0.0.1:8645 --out ./games
python scripts/export_block_games.py --block 5 --out ./games   # game 2 on a resumed chain
```

For a step-by-step walkthrough — including the settlement report, resuming the
chain, starting the node on the right data directory and troubleshooting a
`block 0 (0 games)` export — see
[`docs/PLAY_AND_EXPORT.md`](docs/PLAY_AND_EXPORT.md).

The wallet and miner guides are separate:

- [`docs/CREATE_A_TESTNET_WALLET.md`](docs/CREATE_A_TESTNET_WALLET.md) — create a
  testnet keyfile, fund it from the faucet and check the balance.
- [`docs/OPEN_A_WALLET.md`](docs/OPEN_A_WALLET.md) — create a keyfile, back up the
  24-word phrase, restore it and read the address.
- [`docs/SEND_COINS.md`](docs/SEND_COINS.md) — fund a test wallet from the
  faucet, send a transfer and watch it settle.
- [`docs/RUN_A_TESTNET_MINER.md`](docs/RUN_A_TESTNET_MINER.md) — run the relay and
  an engine miner, register on chain and commit/reveal availability.
- [`docs/EXPLORE_THE_CHAIN.md`](docs/EXPLORE_THE_CHAIN.md) — build the web
  explorer, serve it from `sccp node run --web`, and play a signed table.

To put the two-miner testnet on a GCP VM and connect the deploy workflow, see
[`docs/GCP_RESOURCE_SETUP.md`](docs/GCP_RESOURCE_SETUP.md). The current
deployment is published at
`http://testnet.somecryptochessproject.com:8645`; the workflow connects to the
same name over SSH (`sccp@testnet.somecryptochessproject.com`).

A session never starts at move 1: the relay plays the book line the beacon draws
for the match and hands you the position after it, so the script opens by
printing that line (`opening  1. e4 c6 2. d4 d5 3. exd5 cxd5 4. c4 Nf6`), the
board it left and the move number you are on, and it numbers every move it
reports (`you played 5.Nc3`, `opponent played 5...g5`). `--seed` derives the
beacon, so a different seed is a different opening; `--engine-seed` makes a
given mock opponent replay the same moves.

`--on-chain` is devnet-only. The first run bootstraps `--chain-dir` (default
`./data-play`); later runs **resume that chain** at its next epoch, and
`--games N` plays several games in one sitting. `--fresh` is the explicit reset.
The chain has three funded opponent miners, so the demo keeps the shape of a
mainnet roster; test networks disable the `opponent_cooldown`, so consecutive
proofs are never blocked on a rematch. `--color auto` takes whichever seat the
pairing deals you. When the game ends the script co-signs the result with the
engine, turns the relay transcript into a `WinProof` and mines it with the
production block builder; the final report prints the block height, proof id and
the `node run` command that serves the chain. The mined block also carries the
availability and reveal transactions that make the *next* game's epoch
matchable. A draw, a time forfeit (the claim-transaction path is not wired into
this demo), a game under devnet's 20-ply minimum, or a result the engine will not
co-sign produces no block, and the report says why. Opponent strength is
irrelevant to that rule: a loss to a strong engine still mines the winner's
proof, while a draw or a flag has no ordinary co-signed winner.

`--json` works on every command (`sccp --json chain info`, `sccp --json devnet
up`, ...) and prints one machine-readable document; `miner run --iterations N`
stops after `N` relay polls, which is how the CLI tests drive it.

---

## 7. Current status and next work

Protocol v1 and the testnet release are complete. The reference implementation
includes the wallet and keyfile tooling, the chess proof pipeline, the timed
relay, the ledger and consensus rules, the node and gossip layer, the unattended
miner, quality sampling and bonded challenges, the funded two-miner testnet with
an automated deploy path, and a web explorer with a playable, protocol-signed
table (`src/web`).

The work that remains is operational and economic rather than foundational:

- run a persistent public testnet and publish a 72-hour soak report;
- add metrics and alerting;
- automate timeout-claim submission and pin the drand quicknet verifier;
- move challenge-panel votes on chain;
- harden storage, peer networking, faucet limits and miner/relay resilience;
- complete the audit, economic review and mainnet go/no-go decision.

The prioritized list, acceptance criteria and improvement areas are in
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

---

## 8. Security model and caveats

- **Chess is not a VDF.** Game outcomes depend on engine strength, opening
  randomness and hardware. The cost model is economic, not cryptographic.
- **Legality is cheap.** A legal PGN can be fabricated in microseconds. The
  security comes from co-signed match certificates, beacon-derived pairings,
  quality checks and bonds - in that order.
- **Quality checking is expensive and hardware-sensitive.** It is therefore
  sampled and challengeable, not consensus-critical, so heterogeneous hardware
  cannot fork the chain over a 3-centipawn disagreement.
- **Challenge panels are derived, not yet voted on chain.** Panel selection and
  verdict aggregation are deterministic and pure, but there is no `panel_vote`
  transaction yet, so adjudication is operator-triggered and its honesty is a
  documented assumption in [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).
- **Sybil resistance is economic.** Bonds, diversity constraints and detection
  raise the cost of self-play; they do not make it impossible.
- **Reward variance is high.** Draws earn nothing, so a miner wins maybe one
  game in five against a peer; individual miners may wait a long time between
  payouts, and pooling is an open question.
- **Testnet coins have no value.**
- **License.** This repository is released under the Unlicense. Stockfish is
  GPLv3; this project invokes engine binaries as separate processes and never
  links or redistributes them. `scripts/fetch_engine.py` downloads pinned builds
  at the user's request.

---

## 9. Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

make check         # ruff check + format --check + mypy + pytest
make test          # pytest (unit + property; mock engines only)
make cov           # whole-package report with the 85% gate
make cov-gate      # crypto/ and serialization.py must stay >= 95% covered
make test-all      # adds integration/e2e; skips if Stockfish is absent
make dist-check    # build sdist+wheel and run `twine check --strict` (twine>=7)
make devnet        # in-process node + relay + mock miners
make stack         # write the docker-compose stack and validate the file
```

Real-engine tests are marked `engine` and deselected by default:

```bash
python scripts/fetch_engine.py --engine stockfish-19 --dest ./bin
pytest -m engine tests/integration          # or SCCP_STOCKFISH=/path/to/stockfish
```

Requirements: Python 3.11+ (developed against 3.13), `python-chess`, `pydantic`,
`fastapi`, `uvicorn`, `httpx`, `typer`, `cryptography`, `bech32`, `mnemonic`,
`rich`, with `pytest`, `pytest-asyncio`, `hypothesis`, `ruff` and `mypy` for
development. Real-engine tests need a Stockfish binary; see
`scripts/fetch_engine.py`.

### Chess core

The chess pipeline is import-light and engine-free until you ask for one:

```python
from pathlib import Path

from sccp.chess.engines import MockEngine
from sccp.chess.gameplay import play_game
from sccp.chess.openings import book_for
from sccp.chess.pgn import validate_pgn
from sccp.config import TESTNET

game = validate_pgn(Path("game.pgn").read_bytes(), params=TESTNET, require_win=True)
print(game.game_hash, game.result, game.termination, game.plies)

opening = book_for(TESTNET).select(beacon=bytes(32), match_id="demo")
raw = play_game(MockEngine(seed=1), MockEngine(seed=2), params=TESTNET, opening=opening)
print(raw.pgn)
```

`validate_pgn` enforces the 18 required sccp headers, replays every move with
`Board.is_legal`, checks the result and termination against the board, and hashes
only `(network, start position, SAN list, result)`. The opening book and engine
pins are described in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

### Matchmaking and proofs

Beacons and pairings are pure functions, so a relay cannot invent either:

```python
from sccp.protocol.beacon import compute_beacon
from sccp.protocol.pairing import RosterEntry, pair

beacon = compute_beacon(
    network="testnet",
    epoch=3,
    block_hash="ab" * 32,
    reveals=revealed_beacon_entries,
)
matchups = pair(
    [RosterEntry(address="sccpt1...", max_games=3)],
    bytes.fromhex(beacon.hash),
    network="testnet",
    epoch=3,
    round_number=0,
    rounds_per_epoch=3,
)
```

Certificates commit both seats, colours, opening and engine fingerprints; both
seats sign before the first move and again on the result. `sccp matchmaker run`
serves the relay (`POST /v1/matches/{id}/join|move|resign|result`,
`GET /v1/matches/{id}/receipts`), keeps the 30+0 clock, signs a receipt for every
accepted move and flags a clock that reaches zero, and a JSON store survives
restarts. `sccp verify proof` re-checks the certificate, pairing,
beacon, signatures and PGN offline. drand quicknet verification is a pluggable
`DrandVerifier` (BLS12-381 is not in the stdlib crypto stack); `drand_used = 0`
needs no verifier and is always valid.

### The ledger, wallets and blocks

The chain is a library, not a daemon yet: genesis, transactions, blocks, fork
choice and storage all work in-process.

```python
from sccp.chain.chain import Chain
from sccp.chain.mempool import Mempool
from sccp.config import DEVNET
from sccp.miner.block_builder import BlockBuilder
from sccp.wallet.service import WalletService

chain = Chain.from_genesis_file("devnet", "genesis/devnet.json")
wallet = WalletService(DEVNET, chain.state, chain=chain)
print(wallet.balance(miner_address).total)

mempool = Mempool(params=DEVNET, tip_state=chain.state, book=chain.book)
builder = BlockBuilder(params=DEVNET, chain=chain, mempool=mempool, keypair=miner_keys)
built = builder.build_and_append(timestamp=chain.tip.block.header.timestamp + 1)
```

Every state transition is a signed transaction validated in a fixed order
(schema, network, signature, nonce, balance, type rules, size) and applied by one
function, so the mempool and block connection cannot disagree. Blocks carry one
coinbase, ordinary transactions and exactly `wins_required` win proofs; their
`state_root` is recomputed from the state the block produces, and fork choice
adopts the heaviest chain by cumulative work, returning orphaned payloads to the
mempool on a reorg. SQLite storage keeps canonical block JSON plus periodic
snapshots, and a restart replays from the last snapshot rather than trusting it.
Bonded `timeout_claim`/`timeout_dispute` settlement and clock-witness slashing
live in `src/sccp/chain/timeout.py`; genesis documents in `genesis/*.json` are
regenerated by `scripts/make_genesis.py`. Wire formats are in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

### The node, gossip and the miner daemon

The node and the miner are libraries with thin HTTP/CLI edges, so a whole
network can run in one process without opening a port.

```python
from sccp.devnet import run_devnet

summary = run_devnet(miners=4, blocks=10)
print(summary.height, summary.tip_hash, len(summary.proofs))
```

A node is `sccp.node.service.NodeService`: a `Chain`, a `Mempool`, a peer table
and a transcript cache. It serves the protocol API through
`sccp.node.app.create_app(service)` (blocks by height or hash, accounts, epochs
and beacons, difficulty history, games, matches, transcripts, mempool,
transaction/proof submission, gossip and peers) and speaks gossip through
`sccp.node.p2p`: envelopes are signed in the `sccp/v1/gossip` domain over
network, kind, payload and sender, deduplicated by content id, fanned out to a
random subset of peers and never back to the sender. A peer that serves garbage
collects failures and is banned for five minutes; a block whose parent is
unknown waits in the orphan pool without penalty. `sync_from` pulls missing
heights in order and adopts the heavier branch.

`sccp.miner.engine_miner.EngineMiner` is the unattended half: one `run_once()`
performs at most one relay-visible action per match, so a caller owns the
pacing. It joins certified matches, replays the opponent's moves from the
relay's transcript view, signs its own moves with a receipt for the clock,
co-signs results once it can rebuild them, announces proofs only for games it
won and only after the transcript matches its board, and refuses anything it
cannot reproduce — a changed certificate, an unreproducible result, a transcript
with another game hash, or a match it was not seated in. Timeout claims are
assembled only from witness receipts plus a verified flag, and drawn clocks are
never traded for a signature.

`sccp devnet up` wires the three together with devnet parameters: coins are
funded from the faucet, registration/availability/reveal are signed
transactions, the relay is the production `Matchmaker` behind its real HTTP API,
miners are `EngineMiner`s with seeded `MockEngine`s, and the node mines with the
production `BlockBuilder`. Two seeded engines can shuffle forever, so the devnet
resigns the side to move after 60 plies; resignation is a consensus-valid result
both seats co-sign, so a run always ends with real proofs. Bootstrap is
consensus-defined: blocks up to `bootstrap_end_height` may be empty, which is
exactly the window funding, registration and availability need, so the first
matchable epoch is 3; past it a block needs `wins_required` proofs or, failing
that, one transaction -- a proofless *heartbeat* block mints no subsidy and the
builder puts exactly one transaction in it, so pending ledger work turns into
one block per transaction instead of one block for the lot, which is what lets an
epoch end without proofs. `tests/unit/test_devnet.py` asserts the run reaches
the requested height and that two runs produce the same tip hash.

The CLI is the whole operator surface: `node run` serves the API and
pull-syncs from `--peer` URLs at startup, `miner run` is the unattended loop
(with `--iterations` for scripted runs), `miner register|availability|reveal`
sign the on-chain lifecycle, `wallet balance|send` ask `--node` for the account
(falling back to `--data-dir`), `wallet history` and
`chain info|blocks|account|verify` read the ledger in `--data-dir`, and
`faucet send` is the public test faucet. Every command takes `--json` and is a
thin wrapper over a library call, so the same behaviour is exercised in-process.

Two tests pin the parts a unit test could otherwise drift from:

- `tests/unit/test_api.py` compares the OpenAPI schema against
  `tests/fixtures/openapi.json` (prose and the package version are stripped), so
  a new route, field or status code has to be accepted on purpose with
  `scripts/make_openapi_fixture.py`;
- `tests/e2e/test_local_network.py` runs two nodes under uvicorn on real
  loopback ports, partitions them, and checks they converge on the same tip, and
  that a signed envelope crosses the wire.

`docker-compose.yml` (with `Dockerfile` and `deploy/`) runs the same software as
one node, one relay, a deterministic bootstrap and two Stockfish miners on
testnet. `deploy/README.md` explains the stack and its published keys,
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) covers operations and
[`docs/TESTNET_DEPLOYMENT.md`](docs/TESTNET_DEPLOYMENT.md) documents the merge-to-
deploy workflow and its monthly cost.

### Releasing

Publishing is automated by [`.github/workflows/publish.yml`](.github/workflows/publish.yml):

1. Bump the version in `pyproject.toml` **and** `src/sccp/version.py`; the
   workflow refuses to publish if the two disagree or if the tag does not match.
2. Add a `CHANGELOG.md` entry, run `make check`, commit, then tag `vX.Y.Z` and
   push the tag.
3. Create a GitHub **release** for that tag. Publishing the release builds the
   sdist and wheel, re-runs lint/typecheck/tests, runs `twine check --strict`
   (twine 7+ — current hatchling emits `Metadata-Version: 2.5`, which twine 6
   cannot read), smoke-tests the wheel in a clean venv, and uploads to PyPI with
   [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no
   API token is stored in the repository). Releases marked as pre-release are
   never uploaded to PyPI.
4. TestPyPI dry run: Actions → **publish** → *Run workflow* with
   `repository: testpypi`.

One-time setup: on PyPI (and TestPyPI) add this repository as a trusted
publisher for `sccp` with workflow name `publish.yml` and environment name
`pypi` (`testpypi`).

## 10. License

Public domain, via the [Unlicense](LICENSE).

