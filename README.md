# somecryptochessproject

**Chess as proof of work.**

A cryptocurrency prototype in which the scarce resource is *winning chess games
against another miner*, not SHA-256 hashing. Two adversarial chess engines are
paired by a public randomness beacon, play one game, and only the winner gets
credit. A miner must collect several verified wins - from different opponents and
featuring never-before-mined games - before it may produce a block and claim the
reward.

> **Status: design phase.** There is no implementation yet. The engineering plan
> lives in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md). The CLI
> commands shown below are the intended UX, not working software.

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
- The epoch beacon is
  `B_e = SHA-256("sccp/v1/beacon" || network || e || block_hash(epoch_start(e)-1)
  || sorted(commitment || nonce) || drand_signature(round(e)))`
  (commit-reveal only on testnet).
- Pairings are a **deterministic function** of the active roster and `B_e`:
  every miner is ranked by `SHA-256(B_e || "pair" || address)`, the sorted list
  is paired adjacent, and colour plus opening line are drawn from the same
  beacon.
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
| Beacon | commit-reveal | drand + commit-reveal |
| Minimum fee | 1,000 base units | 1,000 base units |

`devnet` (in-process, mock engines, zero seal difficulty) exists for tests and CI.
All values above are provisional and live in `src/sccp/config.py`.

---

## 5. Repository layout (target)

```text
.
├── README.md
├── LICENSE                       # Unlicense (public domain)
├── pyproject.toml                # packaging, deps, ruff/mypy/pytest config
├── Makefile                      # test / lint / typecheck / devnet
├── docker-compose.yml            # node + relay + miners (M6)
├── docs/
│   ├── IMPLEMENTATION_PLAN.md    # this project's plan
│   ├── PROTOCOL.md               # normative wire formats (written in M1)
│   └── THREAT_MODEL.md           # security analysis (written in M5)
├── data/openings.json            # curated, balanced opening lines
├── genesis/{testnet,mainnet}.json
├── scripts/
│   ├── fetch_engine.py           # download + SHA-256 pin Stockfish builds
│   └── make_genesis.py
├── src/sccp/
│   ├── config.py                 # network parameters, settings
│   ├── serialization.py          # canonical JSON, domain-separated hashing
│   ├── crypto/                   # keys, bech32 addresses, encrypted wallets
│   ├── chess/                    # PGN validation, openings, engines, quality
│   ├── protocol/                 # models, beacon, pairing, transcripts, verification
│   ├── chain/                    # tx, blocks, state, mempool, fork choice, storage
│   ├── node/                     # FastAPI app, gossip, services
│   ├── miner/                    # engine miner, matchmaker/relay, block builder
│   ├── wallet/
│   ├── devnet.py
│   └── cli.py                    # Typer CLI
└── tests/
    ├── unit/
    ├── property/                 # hypothesis
    ├── integration/
    ├── e2e/
    ├── adversarial/                # cheat / self-play / tamper suites (M5)
    └── fixtures/                 # PGNs, key vectors, mock engines
```

---

## 6. Quickstart (target UX)

```bash
git clone git@github.com:beefy/somecryptochessproject.git
cd somecryptochessproject
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create a wallet (writes an encrypted keyfile)
sccp wallet new --network testnet --out alice.key

# Run a full node / API server on the testnet
sccp node run --network testnet --port 8645

# Run a relay (matchmaker) on the same node
sccp matchmaker run --network testnet --node http://127.0.0.1:8645

# Run an engine miner with a real Stockfish
sccp miner run --network testnet \
    --key alice.key \
    --engine-path /opt/homebrew/bin/stockfish \
    --relay http://127.0.0.1:8646

# Run a second miner with an older Stockfish, for testing
sccp miner run --network testnet \
    --key bob.key \
    --engine-path ./bin/stockfish-11 \
    --relay http://127.0.0.1:8646

# Spin up everything locally with mock engines
sccp devnet up --miners 4 --blocks 10

# Verify a PGN or a proof offline
sccp verify pgn game.pgn --network testnet
sccp verify proof proof.json --network testnet
```

---

## 7. Roadmap

| Milestone | Deliverable | Status |
| --- | --- | --- |
| **M0** | Foundations: packaging, config, crypto, CLI skeleton, CI | not started |
| **M1** | Chess core: PGN validation, canonical hashing, opening book, engine abstraction, game runner | not started |
| **M2** | Matchmaking: beacon, deterministic pairing, certificates, relay | not started |
| **M3** | Chain: transactions, wallet, blocks, state, mempool, fork choice, difficulty, block builder | not started |
| **M4** | Node + P2P + miner daemon | not started |
| **M5** | Quality verification, challenges, slashing, threat-model tests | not started |
| **M6** | Testnet launch: genesis, faucet, docker-compose, observability | not started |
| **M7** | Hardening and mainnet readiness review | not started |

Detailed tasks, file-level deliverables and acceptance criteria are in
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
- **Sybil resistance is economic.** Bonds, diversity constraints and detection
  raise the cost of self-play; they do not make it impossible.
- **Reward variance is high.** Winner-take-all with ~50% draws means individual
  miners may wait a long time between payouts.
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

make test          # pytest (unit + property; mock engines only)
make test-all      # adds integration/e2e; skips if Stockfish is absent
make lint          # ruff check + format --check
make typecheck     # mypy
make devnet        # in-process node + relay + mock miners
```

Requirements: Python 3.11+ (developed against 3.13), `python-chess`, `pydantic`,
`fastapi`, `uvicorn`, `httpx`, `typer`, `cryptography`, `bech32`, `mnemonic`,
`rich`, with `pytest`, `pytest-asyncio`, `hypothesis`, `ruff` and `mypy` for
development. Real-engine tests need a Stockfish binary; see
`scripts/fetch_engine.py`.

## 10. License

Public domain, via the [Unlicense](LICENSE).

