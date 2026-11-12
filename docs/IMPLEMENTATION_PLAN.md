# somecryptochessproject — Implementation Plan

> Project name: **somecryptochessproject**. Python package and CLI: **`sccp`**
> (short form; the full name is impractical for imports and shell commands).
> Status: design phase, no code yet. This document is the build order and the
> normative design reference. Update it in the same PR as any protocol change.
>
> Validated environment: Python 3.13.5 on macOS (arm64), 2026-11-11.
> [`python-chess`](https://python-chess.readthedocs.io/) 1.11.x, `cryptography`
> 50.x, `pydantic` 2.x, `fastapi` 0.16x, `typer` 0.29, `bech32` 1.2,
> `mnemonic` 0.21, `pytest` 9.x, `hypothesis` 6.x, `ruff` 0.17, `mypy` 2.x.
> All of the above were installed and smoke-tested while writing this plan.

**Contents:** [1 Scope](#1-scope) · [2 Locked decisions](#2-locked-design-decisions)
· [3 Dependencies](#3-dependencies) · [4 Layout](#4-target-repository-layout) ·
[5 Protocol](#5-protocol-specification) · [6 Milestones](#6-milestones) ·
[7 Testing](#7-testing-strategy) · [8 Threat model](#8-threat-model-outline) ·
[9 Cost model](#9-cost-model-first-estimates-to-be-measured-at-m5) ·
[10 Open questions](#10-open-questions) · [11 Definition of done](#11-definition-of-done)
· [12 Next steps](#12-immediate-next-steps)

---

## 1. Scope

### 1.1 In scope

- A Python package and CLI implementing a small account-based blockchain with
  Ed25519 wallets, signed transactions, blocks, mempool, fork choice and
  persistence.
- A chess proof-of-work protocol: registration, bonds, availability
  commit-reveal, beacon-derived matchmaking, signed match transcripts, win
  proofs, claim rules, and unique-game enforcement.
- An engine miner that drives any UCI engine (Stockfish, older Stockfish,
  mock engines for tests).
- A matchmaker/relay service and a full node with an HTTP/JSON API and simple
  gossip.
- `testnet`, `mainnet` and `devnet` parameter sets.
- Unit, property, integration and end-to-end tests, plus opt-in real-engine
  tests.

### 1.2 Out of scope (v1)

- A GUI, a chess engine, or chess analysis features.
- Anonymity, privacy, or confidential transactions.
- Smart contracts, NFTs, or a general VM.
- Light clients / SPV proofs (header and state roots are reserved for later).
- Pools, delegated mining, or reward sharing.
- A production peer-to-peer stack (v1 uses HTTP gossip; no NAT traversal).
- Monetary policy research beyond fixed subsidy + halving.
- Audited security.

### 1.3 Design principles

1. **Do not reinvent chess.** `python-chess` owns board state, SAN, PGN, UCI,
   legality, and outcome detection. We write zero move-generation code.
2. **Deterministic where it matters.** Serialization, hashing, addresses,
   pairing, openings and claim validation are exact functions of public data.
3. **Cheap checks before expensive checks.** Signature/legality/uniqueness
   checks are consensus-critical; engine-based quality analysis is sampled,
   bonded and non-consensus so hardware differences cannot fork the chain.
4. **No trusted matchmaker.** The relay is a courier. Pairings are verifiable
   from the beacon and roster by anyone.
5. **Testnet first.** Every feature ships on `testnet`/`devnet` before mainnet
   parameters are enabled.

---

## 2. Locked design decisions

These are decided; changing them is a protocol change and needs a doc update
plus tests.

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | Account model, not UTXO | Simpler balances, bonds, nonces, slashing; sufficient for a prototype |
| D2 | Ed25519 + bech32 addresses | Fast, small signatures, well-supported by `cryptography`, human-readable addresses |
| D3 | SHA-256 with domain tags | Ubiquitous; domain separation prevents cross-protocol replay |
| D4 | Blocks contain exactly `k` win proofs, all earned by the block's miner (plus a header seal) | Matches the "multiple wins = more work" requirement; proofs are non-transferable |
| D5 | `k` and header seal bits are the difficulty knobs | Coarse (wins) + fine (hash target) control |
| D6 | Matchmaking via commit-reveal + drand, deterministic pairing | No trusted third party; verifiable after the fact |
| D7 | Match = one game; rounds inside epochs provide replay after draws | Avoids infinite same-pair series; keeps transcripts simple |
| D8 | Both seats co-sign the final transcript | Prevents the winner from fabricating a game alone; gives slashing evidence |
| D9 | Quality checks are non-consensus (sampled + bonded challenges) | Engine eval is hardware-sensitive; consensus must stay deterministic |
| D10 | Draws earn nothing | Winner-take-all, per the project premise; replay with a new seed |
| D11 | SQLite for storage | Zero-ops, transactional, adequate for testnet; abstracted behind `storage.py` |
| D12 | HTTP/JSON API + simple gossip | Debuggable; enough for a testnet; P2P stack is a later milestone |
| D13 | Pinned engine profiles per network | Makes quality checks reproducible and prevents "unknown engine" ambiguity |
| D14 | Canonical game hash excludes PGN metadata | Same moves = same game, regardless of formatting/comments/clocks |

---

## 3. Dependencies

Runtime (declared in `pyproject.toml`):

| Package | Use | Notes |
| --- | --- | --- |
| `python-chess` | Board, legal moves, SAN/PGN, outcomes, UCI engine control, polyglot books | The only chess dependency; no custom rules code |
| `cryptography` | Ed25519 keys/signatures, scrypt, AES-GCM keyfile encryption | Smoke-tested in the target environment |
| `bech32` | Address encoding/decoding | BIP-173 style; HRP `sccp` / `sccpt` |
| `mnemonic` | Optional BIP-39 seed phrases for wallets | 12-word generation smoke-tested; 24 words used for 256-bit seeds |
| `pydantic` / `pydantic-settings` | Wire models, validation, config | Schemas double as protocol documentation |
| `fastapi` / `uvicorn` | Node and relay HTTP API | ASGI test client for tests |
| `httpx` | Relay client, gossip, drand fetches | Works with ASGI transport |
| `typer` / `rich` | CLI and console output | Subcommands mirror this plan |
| `orjson` (optional) | Faster canonical JSON encoding | Fallback to stdlib `json` |

Development: `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`,
`mypy`, and `types-*` packages as needed.

Deliberately not used:

- The PyPI `stockfish` wrapper: `python-chess` already provides a complete UCI
  client (`chess.engine.SimpleEngine.popen_uci`).
- Any hand-written chess rules library: explicitly forbidden by the project
  premise.
- `python-chess`'s optional native speedups: not required; avoid build friction.

Engine binaries are **not** vendored. `scripts/fetch_engine.py` downloads pinned
Stockfish builds (current and older, e.g. 11/12 for compatibility tests),
verifies SHA-256, and writes them to `./bin/` (gitignored). Stockfish is GPLv3;
it is invoked as a separate process, never linked or redistributed.

---

## 4. Target repository layout

```text
somecryptochessproject/
├── README.md
├── LICENSE                        # Unlicense
├── pyproject.toml                 # project metadata, deps, tool config
├── Makefile                       # test, lint, typecheck, devnet
├── docker-compose.yml             # node + relay + 3 miners (M4)
├── .github/workflows/ci.yml
├── docs/
│   ├── IMPLEMENTATION_PLAN.md
│   ├── PROTOCOL.md                # normative wire formats (M1 deliverable)
│   └── THREAT_MODEL.md            # security analysis (M5 deliverable)
├── data/openings.json             # curated balanced opening lines
├── genesis/testnet.json
├── genesis/mainnet.json
├── scripts/
│   ├── fetch_engine.py
│   └── make_genesis.py
├── src/sccp/
│   ├── __init__.py
│   ├── version.py
│   ├── config.py                  # pydantic network parameters
│   ├── errors.py                  # typed protocol errors
│   ├── serialization.py           # canonical JSON + domain-separated hashes
│   ├── crypto/
│   │   ├── keys.py                # Ed25519 keypairs, sign/verify
│   │   ├── address.py             # bech32 encode/decode
│   │   └── wallet.py              # keyfiles, scrypt + AES-GCM, lock/unlock
│   ├── chess/
│   │   ├── pgn.py                 # parse, validate, canonicalize, hash
│   │   ├── openings.py            # seeded book selection, book validation
│   │   ├── engines.py             # UCI adapter + mock/scripted engines
│   │   ├── quality.py             # reference-engine CPL policy
│   │   └── gameplay.py            # move loop, clocks, transcript capture
│   ├── protocol/
│   │   ├── models.py              # tx, block, proof, certificate, beacon
│   │   ├── beacon.py              # commit-reveal + drand mixing
│   │   ├── pairing.py             # deterministic roster pairing
│   │   ├── transcript.py          # match certificate construction/signing
│   │   ├── challenges.py          # bonded quality challenges (M5)
│   │   └── verification.py        # proof/block verification pipeline
│   ├── chain/
│   │   ├── tx.py                  # tx validation and application
│   │   ├── block.py               # header/body, merkle roots, sealing
│   │   ├── state.py               # accounts, bonds, proof registry
│   │   ├── chain.py               # fork choice, reorg handling
│   │   ├── mempool.py             # txs + proof announcements
│   │   ├── difficulty.py          # k and seal-bit retargeting
│   │   └── storage.py             # SQLite schema and snapshots
│   ├── node/
│   │   ├── app.py                 # FastAPI application factory
│   │   ├── routes.py              # REST endpoints
│   │   ├── p2p.py                 # gossip and sync
│   │   ├── quality_sampler.py     # deterministic sampling + audit jobs (M5)
│   │   └── service.py             # wiring: state, mempool, validator
│   ├── miner/
│   │   ├── engine_miner.py        # availability, play, sign, submit
│   │   ├── matchmaker.py          # relay/rounds/transcript store
│   │   └── block_builder.py       # assemble a claim into a block
│   ├── wallet/
│   │   └── service.py             # balance, send, stake, history
│   ├── devnet.py                  # in-process node + relay + mock miners
│   └── cli.py                     # Typer entry point
└── tests/
    ├── conftest.py
    ├── unit/
    ├── property/
    ├── integration/
    ├── e2e/
    ├── adversarial/               # cheat/self-play/tamper suites (M5)
    └── fixtures/                  # PGNs, key vectors, mock engines
```



---

## 5. Protocol specification

### 5.1 Network constants

`src/sccp/config.py` defines immutable `NetworkParams` objects. The network id
is part of every hash and signature.

| Constant | Testnet | Mainnet | Meaning |
| --- | --- | --- | --- |
| `network_id` | `testnet` | `mainnet` | String mixed into all domains |
| `address_hrp` | `sccpt` | `sccp` | bech32 human-readable prefix |
| `epoch_length` | 5 blocks | 20 blocks | Blocks per matchmaking epoch |
| `rounds_per_epoch` | 3 | 5 | Games a miner may play per epoch |
| `availability_lead` | 2 epochs | 2 epochs | Commit in `e-2`, reveal in `e-1` |
| `wins_required` (`k`) | 1 | 3 (range 1..10) | Wins per block claim |
| `seal_bits` | 0 | 16 (range 0..32) | Header hash target |
| `claim_window` | 64 blocks | 144 blocks | Max age of a win when claimed |
| `opponent_cooldown` | 1 epoch | 4 epochs | Min gap before rematching a miner |
| `subsidy` | 50 coins | 50 coins | Coinbase subsidy per block |
| `halving_interval` | — | 210,000 blocks | Subsidy halves |
| `max_supply` | — | 21,000,000 coins | Cap |
| `coinbase_maturity` | 20 blocks | 100 blocks | Spendable after |
| `min_bond` | 10 coins | 500 coins | Registration bond |
| `registration_epochs` | 10 | 30 | Registration lifetime unless extended |
| `unbond_delay` | 50 blocks | 1,000 blocks | Bond withdrawal delay |
| `min_fee` | 1,000 units | 1,000 units | Per transaction |
| `min_plies` / `max_plies` | 20 / 300 | 30 / 400 | Game length bounds |
| `nodes_per_move` | 50,000 | 250,000 | Engine effort per move |
| `book_version` | `dev-1` | `v1` | Opening book identifier |
| `quality_mode` | `off` | `sampled` | Quality policy |
| `challenge_window` | 30 blocks | 100 blocks | Reward vesting / challenge period |

Amounts are integers in base units: `1 coin = 100,000,000 units`. **No floating
point value ever touches consensus code.**

Additional constants (defaults, all overridable per network):

| Constant | Testnet | Mainnet | Meaning |
| --- | --- | --- | --- |
| `target_block_seconds` | 60 | 600 | Retarget goal |
| `retarget_interval` | 10 blocks | 40 blocks | Difficulty adjustment period |
| `k_min` / `k_max` | 1 / 5 | 1 / 10 | Bounds for `wins_required` |
| `beacon_grace_blocks` | 2 | 6 | Wait before commit-reveal fallback |
| `drand_chain` | — | `quicknet` | Beacon source on mainnet |
| `quality_sample_rate` | 0 | 50 | Per-mille of proofs re-analysed |
| `max_block_bytes` | 1 MiB | 1 MiB | Hard block size |
| `max_pgn_bytes` | 256 KiB | 256 KiB | PGN size cap |
| `max_tx_bytes` | 64 KiB | 64 KiB | Transaction size cap |

`devnet` reuses the testnet parameter set with `epoch_length = 1`,
`target_block_seconds = 1`, `seal_bits = 0`, `k = 1`, `min_bond = 0`,
`quality_mode = off` and a fixed genesis timestamp, so the whole loop runs in
seconds inside tests and `sccp devnet up`.

### 5.2 Canonical encoding and hashing

`src/sccp/serialization.py` is the single source of truth:

```python
def canonical_json(obj) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
```

Rules:

- Only `dict[str, ...]`, `list`, `str`, `int`, `bool`, `None`. Floats are
  rejected at serialization time (use integer base units and integer seconds).
  Integers must fit in `[-2**63, 2**64 - 1]`; larger values are rejected.
- Strings are UTF-8 and normalized to NFC before encoding.
- Optional fields are omitted rather than set to `null` when unused.
- Every signature and hash covers
  `sha256(domain.encode() + b"\x00" + canonical_json(payload))`, where `domain`
  is one of:
  - ledger: `sccp/v1/tx`, `sccp/v1/header`, `sccp/v1/block`, `sccp/v1/challenge`
  - chess: `sccp/v1/game`, `sccp/v1/proof`, `sccp/v1/certificate`,
    `sccp/v1/result`, `sccp/v1/match`
  - matchmaking: `sccp/v1/beacon`, `sccp/v1/beacon-commit`, `sccp/v1/pair`
  - `sccp/v1/address` for address derivation (see 5.3)
- Hex digests are lowercase, 32 bytes; signatures are lowercase hex, 64 bytes.
- `network_id` and protocol `version` are always inside the signed payload.

Merkle commitments (`state_root`, `roster_root`, block body) use one shared
construction in `serialization.py`:

```text
leaf(key, value)   = sha256(b"\x00" + utf8(key) + b"\x00" + canonical_json(value))
node(left, right)  = sha256(b"\x01" + left + right)
empty_root         = b"\x00" * 32
```

Leaves are sorted by `key` (byte order). An odd node at any level is promoted
unchanged to the next level. `value = None` means "absent" and is never encoded
as a leaf. This is intentionally simple; it is not an SMT and has no
non-membership proofs.

This prevents cross-network and cross-message replay, and gives one obvious place
to audit encoding bugs.

### 5.3 Keys, addresses and wallets

- **Keypair:** Ed25519 via `cryptography.hazmat.primitives.asymmetric.ed25519`.
  32-byte public key, 64-byte signature.
- **Address payload:** `version_byte || H("sccp/v1/address", {network, public_key})[:20]`
  with `version_byte = 0` reserved for Ed25519. Including the network in the
  derivation means one key yields different addresses on testnet and mainnet.
- **Text address:** bech32 with HRP `sccp` (mainnet) or `sccpt` (testnet), e.g.
  `sccp1q...`. Lowercase canonical form; mixed case is rejected per BIP-173.
- **Signing:** `sign(sk, H(domain, body))`; verification is pure and never
  raises on malformed input (returns `False`).
- **Keyfile:** JSON with `version`, `network`, `address`, `public_key`,
  `kdf` (`scrypt`, `n=2**15`, `r=8`, `p=1`, 16-byte salt), `cipher`
  (`aes-256-gcm`, 12-byte nonce), `ciphertext`, and a `created_at` field. The
  canonical header is the AEAD associated data. Files are written `0600`;
  `sccp wallet new` refuses to overwrite unless `--force`.
- **Backup:** `sccp wallet export-mnemonic` encodes the 32-byte private seed as
  24 BIP-39 words using the `mnemonic` package. This is a lossless backup
  encoding, **not** BIP-32 derivation; document that distinction prominently.
- **Wallet UX:** `wallet new`, `wallet address`, `wallet balance`, `wallet send`,
  `wallet history`, `wallet export-mnemonic`, `wallet import-mnemonic`.
- **Passphrase delivery for daemons:** interactive prompt by default; for
  unattended miners use `--passphrase-file` (mode-checked, not the keyfile) or
  `SCCP_WALLET_PASSPHRASE`. The keyfile is never decrypted to disk, and the
  in-memory private key is zeroised on shutdown best-effort.

### 5.4 Transactions

All transactions share:

```json
{
  "network": "testnet",
  "type": "transfer",
  "from": "sccpt1...",
  "nonce": 7,
  "fee": 1000,
  "payload": {},
  "signature": "hex"
}
```

`txid = H("sccp/v1/tx", body_without_signature)`. The signature covers the
`txid`. Validation order: schema → network → signature → nonce → balance →
type-specific rules → size limits. Mempool admission runs the same checks plus
a fee floor; blocks rerun everything.

| Type | Payload | State effect |
| --- | --- | --- |
| `transfer` | `to`, `amount` | Move balance, increment nonce, pay fee |
| `register` | `public_key`, `endpoint`, `engine`, `bond` | Create/lock bond, set `expires_epoch`, store engine fingerprint |
| `update_registration` | `endpoint?`, `engine?`, `extend_epochs` | Update metadata, extend expiry, adjust bond |
| `unregister` | — | Start unbonding at `height + unbond_delay`; leave roster next epoch |
| `withdraw_bond` | — | Release bond after the unbonding height |
| `availability` | `epoch`, `max_games`, `commitment` | Record availability + commit hash |
| `reveal` | `epoch`, `nonce` | Record reveal; must match commitment |
| `challenge` | `proof_id`, `reason`, `evidence`, `bond` | Open a quality/challenge case |
| `slash_evidence` | `proof_id`, `evidence` | Auto-slash on cryptographic equivocation |

Rules:

- Exactly one transaction per `(from, nonce)` in the mempool and per chain.
- Fees are debited from spendable balance, never from bonded balance.
- `register` requires `bond >= min_bond` and a reserved balance; the bond stays
  owned by the account but is unspendable until unbonding completes.
- Registration expires after `registration_epochs` unless extended; expired
  miners leave the roster but keep their bond.
- Endpoints are informational; consensus never fetches them.
- The engine fingerprint is `{name, version, net_sha256, options}` and must
  match a pinned profile on mainnet.

### 5.5 Epochs, availability and the beacon

Timeline for epoch `e`:

1. During epoch `e-2`, a miner submits
   `availability(e, max_games, commitment)` where
   `commitment = H("sccp/v1/beacon-commit", {address, epoch, nonce})`.
2. During epoch `e-1`, the miner submits `reveal(e, nonce)`. A reveal is valid
   only if the matching availability tx is in an ancestor block.
3. At the last block of epoch `e-1`, the beacon for `e` is fixed:
   `B_e = SHA-256("sccp/v1/beacon" || network || e
   || block_hash(epoch_start_height(e) - 1)
   || concat(sorted(commitment || nonce) for valid reveals))`.
4. On mainnet, mix in drand:
   `B_e = SHA-256(B_e || drand_signature(round(e)))`. If drand is unavailable,
   the network falls back to commit-reveal only after `beacon_grace_blocks`; the
   fallback is logged and exposed in the API.
5. Matches in epoch `e` use `B_e`. The roster is the set of registered, bonded,
   non-expired miners at the last block of `e-1` with a valid reveal for `e`.
6. The drand round is fixed deterministically from the median-time-past of block
   `epoch_start_height(e) - 1` and stored in the epoch record, so every node
   fetches and verifies the same signature offline.

Properties and limits:

- Commitments are hidden until the reveal phase, so nobody can pick an opponent
  after seeing the roster outcome.
- The last revealer can choose to reveal or withhold, giving at most one bit of
  influence per epoch. Withholding forfeits that epoch's availability and is
  recorded; repeated withholding blocks future registrations (policy, not
  consensus). Mainnet mixing with drand removes the bias entirely.
- Reveals are cheap txs; the mempool prioritises them near epoch boundaries.
- Beacons are stored per epoch and exposed at `GET /v1/epochs/{e}/beacon` with
  the reveal set and the block hash used.

### 5.6 Pairing algorithm

`src/sccp/protocol/pairing.py`, pure and deterministic:

```text
pair(roster, beacon, round, constraints) -> list[Matchup]
  candidates = [m for m in roster if available(round, m) and quota(m, round) > 0]
  for m in candidates:
      score[m] = SHA256("sccp/v1/pair" || beacon || round || m.address)
  ordered = sorted(candidates, key=lambda m: (score[m], m.address))
  i = 0
  while i + 1 < len(ordered):
      a, b = ordered[i], ordered[i+1]
      if violates(a, b):            # same entity/funder/subnet/ASN, recent opponent
          j = first index > i+1 such that swapping b with ordered[j] fixes it
          if j is None: accept the soft violation (liveness over diversity)
          else: ordered[i+1], ordered[j] = ordered[j], ordered[i+1]
      emit Matchup(a, b)
      i += 2
  if odd count: last miner sits out (logged)
```

- Colour: `SHA256(beacon || "color" || match_id) & 1`.
- Opening line: `int(SHA256(beacon || "opening" || match_id)) % len(book)`.
- `match_id = H("sccp/v1/match", {epoch, round, seats_sorted, opening_hash})`.
- Hard constraints: distinct addresses, both in the epoch roster, both with
  available quota, no repeated pair in the same round. Soft constraints: the
  diversity heuristics in section 8.3.
- Any node can recompute the matchup list from public data; the relay only
  delivers it. A proof whose certificate does not recompute to the same matchup
  is invalid.
- Property tests must check determinism, no duplicate seats, quota compliance,
  graceful odd rosters, and that changing the beacon reshuffles pairings.


### 5.7 Match protocol and transcript

A match is one game. The relay orchestrates rounds, but every step is signed so
the transcript is self-contained.

1. **Start commitment.** The relay sends each seat the matchup, colours, opening
   line and `match_id`. Each seat signs
   `H("sccp/v1/certificate", {match_id, epoch, round, seats, colors,
   opening_hash, engine_fingerprints})` and sends it to the relay and the peer.
2. **Play.** The relay forwards moves. The engine miner validates every incoming
   move with `board.parse_san`/`board.push_san` before replying; an illegal move
   aborts the match and is evidence of misbehaviour.
3. **Finish.** When `board.outcome()` is non-null or a seat resigns, both seats
   sign `H("sccp/v1/result", {match_id, game_hash, result, termination,
   plies})`. The winner stores both signatures; either seat may publish the
   transcript.
4. **Proof.** The winner wraps the transcript, PGN and opening into a `WinProof`
   and submits it to nodes / its block builder.
5. **No-shows.** A seat that disconnects or refuses to co-sign loses its
   availability slot for the rest of the epoch, is deprioritised in future
   pairings, and is recorded on the relay's public reputation endpoint. There is
   **no on-chain match deposit in v1**; on-chain slashing applies only to
   cryptographic evidence (section 8.3). A refused match costs the honest winner
   nothing but also earns nothing, so griefing burns availability, not coins.

Transcript hashes are **not** the game hash: the game hash covers only
`(network, start position, SAN moves, result)` for uniqueness; certificates
cover identity, pairing and engine metadata.

### 5.8 PGN validation pipeline

`src/sccp/chess/pgn.py` returns a `ParsedGame` or raises a typed
`PgnValidationError`. It never mutates global state and has no engine
dependency, so it is fully unit-testable.

```text
validate_pgn(pgn_text, expected: MatchCertificate, params) -> ParsedGame
  1. Reject empty input, > max_pgn_bytes, multiple games, non-UTF-8.
  2. game = chess.pgn.read_game(io.StringIO(pgn_text))
     if game is None or game.errors: reject with the error list.
  3. Required tags: Event, Site, Date, Round, White, Black, Result,
     Termination, MatchID, Epoch, RoundNumber, SeatWhite, SeatBlack,
     EngineWhite, EngineBlack, OpeningID, OpeningHash, Rules.
  4. Tags must match `expected` exactly: match id, epoch, round, seat
     addresses, engine fingerprints, opening hash, Rules == "chess".
  5. Replay the mainline from chess.Board():
       board = chess.Board()
       for move in game.mainline_moves():
           if board.status() != chess.STATUS_VALID: reject
           board.push(move)                  # illegal moves raise here
           if board.ply() > params.max_plies: reject
     Variations/comments/clocks are ignored for hashing but preserved in the
     stored PGN. Proofs reject variations for simplicity; the offline verifier
     accepts them.
  6. Opening check: the first len(opening_moves) plies equal the book line
     selected by the beacon; OpeningHash matches.
  7. Termination:
       - Result header must be 1-0, 0-1 or 1/2-1/2; "*" is rejected for proofs.
       - computed = board.outcome(claim_draw=False)
       - CHECKMATE: Result must name the winner; Termination == "checkmate".
       - STALEMATE / INSUFFICIENT_MATERIAL / SEVENTYFIVE_MOVES /
         FIVEFOLD_REPETITION: Result must be 1/2-1/2; Termination == "draw".
       - computed is None: only Termination == "resignation" is allowed, with a
         win result, and the quality policy must agree the position is lost.
       - Everything else (time forfeit, abandoned, unterminated) is rejected.
  8. For proofs: require a decisive result and plies >= params.min_plies.
     Draws are valid PGNs but earn no credit.
  9. Canonical hash:
       san_list = [board.san(m) for m in game.mainline_moves()]  # regenerate
       game_hash = H("sccp/v1/game", {network, start_fen, san_list, result})
     Raw PGN text, headers, comments, clocks and spacing never enter the hash.
 10. Return ParsedGame(game_hash, pgn_text, san_list, final_board, result,
     termination, tags).
```

Implementation gotchas verified against `python-chess` 1.11.x:

- `game.board()` returns the **root** board, not the final position. Replay
  `mainline_moves()` and keep your own board.
- `game.errors` catches illegal SAN, but a game can end legally while its
  `Result` header lies; always compare the header with `board.outcome()`.
- `board.outcome()` ignores PGN headers entirely.
- `board.variation_san()` cannot be called after the game is over; generate SAN
  from the starting board instead.
- Castling rights, en passant, promotions and dead positions are handled by
  `python-chess`; do not duplicate those rules.


### 5.9 Quality policy (anti-fabrication)

The quality policy answers "did both sides actually play strong chess?" It is
the difference between a chess proof of work and a proof that someone can type.

Reference profile (mainnet, versioned as `quality-v1`):

```json
{
  "engine": "stockfish-17",
  "binary_sha256": "<pinned>",
  "net_sha256": "<pinned>",
  "options": {"Threads": 1, "Hash": 16, "MultiPV": 5, "Contempt": 0},
  "syzygy": null,
  "depth": 14,
  "max_cpl": 150,
  "decided_cp": 600,
  "max_blunders_per_side": 2,
  "resign_cp": 300,
  "min_plies": 30
}
```

Algorithm (`src/sccp/chess/quality.py`):

```text
analyse_game(parsed, engine) -> QualityReport
  board = start
  for each (san, move) in parsed.moves:
      info = engine.analyse(board, Limit(depth=depth), multipv=5)
      best_cp = info[0].score.pov(board.turn).score(mate_score=100000)
      if move in info: played_cp = that line's score
      else:
          board.push(move)
          reply = engine.analyse(board, Limit(depth=depth))
          played_cp = -reply.score.pov(board.turn).score(mate_score=100000)
          board.pop()
      cpl = max(0, best_cp - played_cp)
      if abs(best_cp) < decided_cp and cpl > max_cpl: blunder[side] += 1
      board.push(move)
  verdict = blunders[white] <= max and blunders[black] <= max
            and resignation_is_hopeless(parsed, engine, resign_cp)
```

Policy decisions:

- **Exempt decided positions.** Once the reference eval is beyond
  `decided_cp`, blunders are not counted; a lost side cannot "blunder more".
- **Both sides are checked.** A self-playing miner that weakens the loser is
  caught; to pass, it must run two strong engines, which removes the economic
  advantage of self-play.
- **Resignation proof.** A resignation is accepted only if the final position is
  evaluated at worse than `-resign_cp` for the resigning side (or mate is
  forced). Checkmates are always accepted.
- **Hardware tolerance.** Evals vary slightly across CPUs, so thresholds are
  generous and the policy is not consensus-critical. Never compare exact eval
  strings across machines.
- **Determinism where possible.** `Threads=1`, fixed depth, fixed net, no
  Syzygy, no opening book, no contempt. This makes disagreement rare but not
  impossible, which is why the policy is enforced by challenges, not by block
  validity.

Enforcement:

- `testnet`: `quality_mode = off`; the report is recorded but not required. Fast
  local iteration.
- `mainnet`: `quality_mode = sampled`.
  - The proof must include a `quality_summary` (policy id, engine fingerprint,
    per-side blunder counts, worst CPL, verdict, and a hash of the full trace).
  - The full trace stays with the relay and is retrievable by hash.
  - On testnet and devnet (`quality_mode = off`) `quality_summary` is optional
    and ignored; the field exists so the wire format does not change at launch.
  - Every validator deterministically samples proofs with
    `H(block_hash || proof_id) % 1000 < sample_rate` (default 50 = 5%) and
    re-runs the analysis asynchronously. A mismatch opens an automatic
    challenge.
  - Any bonded party may challenge within `challenge_window` blocks by posting a
    `challenge` tx with a bond.
- Adjudication (M5): a bonded panel selected from the beacon re-runs the pinned
  profile. Success slashes the miner's bond and burns the unvested coinbase;
  failure slashes the challenger. Until the panel exists, mainnet stays disabled
  (testnet uses an operator key for the same flow).

Cost note: at depth 14 a 100-ply game takes single-digit seconds to a few tens
of seconds per core. A block with `k=3` wins and ~5 games per win needs roughly
15 games of play and, for a full audit, 15 games of analysis. Full inline
verification is therefore infeasible; sampling plus bonds is the design.

### 5.10 Win proofs and claim rules

```json
{
  "network": "testnet",
  "proof_id": "hex",
  "game_hash": "hex",
  "match_id": "hex",
  "epoch": 42,
  "round": 3,
  "winner": "sccpt1...",
  "loser": "sccpt1...",
  "winner_seat": "white",
  "result": "1-0",
  "termination": "checkmate",
  "plies": 87,
  "opening": {"book_version": "dev-1", "line_id": 17, "hash": "hex"},
  "pgn": "[Event ...] ...",
  "certificate": {"start_signatures": {"white": "hex", "black": "hex"},
                  "result_signatures": {"white": "hex", "black": "hex"}},
  "quality_summary": {"policy": "quality-v1", "verdict": "pass", "trace_hash": "hex"}
}
```

`proof_id = H("sccp/v1/proof", {game_hash, match_id, winner})`.

A block is valid only if all of the following hold for its proof set:

1. `len(proofs) == k` for the block's declared `wins_required` (genesis is
   exempt, see 5.11).
2. `block.miner == proof.winner` for every proof; proofs are non-transferable.
3. Each `proof_id` and `game_hash` is unused in the ancestor chain, the mempool,
   and the block itself.
4. Both signatures in `start_signatures` and `result_signatures` verify against
   the certificate seats; seats are distinct addresses.
5. The certificate recomputes: roster at `epoch_start_height(e)-1`, beacon
   `B_e`, pairing for `(e, round)`, colours, opening hash, engine fingerprints.
6. The PGN passes section 5.8, its tags match the certificate, and the declared
   winner is the actual winner.
7. `epoch_end_height(e) + claim_window >= block.height`; at least `k` distinct
   opponents across the claim; no opponent repeated inside `opponent_cooldown`
   epochs (checked against `MinerStats`).
8. The winner is registered, bonded and not banned at the proof epoch.
9. The coinbase output equals `subsidy(height) + sum(fees)` and pays
   `block.miner`; coinbase value is not spendable until
   `height + coinbase_maturity`.
10. Proofs are ordered deterministically (by `proof_id`) to make block hashes
    canonical.

Duplicate handling on reorg: the proof registry is rebuilt from the new
canonical chain; proofs from orphaned blocks return to the mempool unless their
game hashes are already used on the winning chain.

**Roster requirement.** A claim needs `k` distinct opponents inside the claim
window and the pairing rules forbid immediate rematches, so `k = 3` requires at
least four active miners (testnet `k = 1` requires two). Below that no valid
blocks can be produced. The protocol stalls rather than lowering `k`
automatically; the operator must add miners or reset a testnet. Mainnet launch
therefore depends on demonstrated roster depth (M7).


### 5.11 Blocks, state, fork choice and difficulty

**Header**

```text
version, network, height, prev_hash, merkle_root, state_root, roster_root,
timestamp, epoch, beacon_hash, wins_required (k), seal_bits, seal_nonce, miner
```

- `merkle_root` covers `[coinbase_txid, txids..., proof_ids...]` in that order.
- `state_root` is a sorted SHA-256 Merkle tree over `address -> account` leaves;
  `roster_root` is the same construction over the epoch's active roster. Both
  are commitments for future light clients and are cheap at prototype scale.
- `block_hash = H("sccp/v1/header", header)`.
- Seal: `int(block_hash, 16) < 2 ** (256 - seal_bits)`. With `seal_bits = 0`
  every hash is valid (devnet/testnet speed).
- Timestamp must be greater than the median of the last 11 blocks and at most
  two hours in the future (for heights 1–10 the median covers the ancestors that
  exist; height 0 is exempt).
- `seal_bits` and `wins_required` are **declared** in the header but must equal
  the values scheduled by the parent state; a miner cannot lower its own
  difficulty.
- Genesis (height 0) is identified by the pinned `genesis_hash` in the network
  config, carries no proofs and no coinbase, and its allocations are the only
  premine (testnet faucet only; mainnet genesis has no allocations).

**State**

```text
Account:   balance, bonded, unbonding_height, nonce, registered_until_epoch,
           endpoint, engine_fingerprint, banned_until
ProofReg:  game_hash -> (proof_id, height, winner)
MatchReg:  match_id -> (epoch, round, seats)
MinerStat: address -> (wins, games, last_opponent_epoch[address])
Epoch:     epoch -> (beacon, roster_root, reveal_set, ended_at_height)
```

The state is a plain Python object with `apply_tx`, `apply_proof`, `apply_block`,
`snapshot` and `restore`. `storage.py` persists blocks/txs and keeps state
snapshots every `snapshot_interval` blocks plus the last `reorg_depth` blocks'
undo data.

**Fork choice**

- `block_work = (1 << seal_bits) + k * WIN_UNIT`, `WIN_UNIT = 1 << 16`.
- Heaviest cumulative work wins; ties break on the lower block hash.
- Reorgs rebroadcast orphaned txs and proofs, rebuild the proof registry from the
  new chain, and reset the mempool dedupe set.
- Start with a simple "sync all headers, fetch missing blocks, apply in order"
  strategy. No headers-first optimisations in v1.

**Difficulty**

- Retarget every `retarget_interval` (testnet 10, mainnet 40 blocks) using the
  median time past of the interval.
- Adjust `seal_bits` by ±1 for fine control, clamped to the network range.
- Adjust `k` by ±1 for coarse control only at epoch boundaries, clamped to
  `k_min..k_max`. Never increase `k` while a claim window is open without a
  grace period of one epoch; proofs already accepted remain valid.
- Store the retarget history for debugging; expose at `GET /v1/difficulty`.

**Subsidy and fees**

- `subsidy(height) = 50 coins >> (height // halving_interval)` (mainnet), capped
  at `max_supply`; testnet uses a flat 50 coins.
- Coinbase = subsidy + all fees in the block; one output to the block miner.
- Maturity prevents spending until `height + coinbase_maturity`.

### 5.12 Node API and gossip

`src/sccp/node/routes.py` (all JSON, versioned):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/status` | Height, tip hash, peers, network, mempool sizes |
| GET | `/v1/params` | Network constants (public) |
| GET | `/v1/blocks/{height_or_hash}` | Block + proofs |
| GET | `/v1/accounts/{address}` | Balance, bond, nonce, registration |
| GET | `/v1/epochs/{e}` | Beacon, roster, reveals |
| GET | `/v1/epochs/{e}/beacon` | Beacon + reveal set + block hash used |
| GET | `/v1/difficulty` | Current `k`, `seal_bits`, retarget history |
| GET | `/v1/games/{game_hash}` | PGN, proof status, challenge status |
| GET | `/v1/matches/{match_id}` | Transcript metadata |
| GET | `/v1/mempool` | Pending txids/proof ids (not full bodies) |
| POST | `/v1/tx` | Submit a signed transaction |
| POST | `/v1/proofs` | Announce a win proof |
| GET | `/v1/challenges` | Open quality challenges |
| POST | `/v1/gossip` | Signed gossip envelope (tx/proof/block/peer) |
| GET | `/v1/peers` | Known peers |

Gossip:

- Envelope: `{network, kind, payload, sender, signature}`; signed by the sender's
  wallet. Blocks and proofs are also gossiped by relayers, so sender identity is
  only for abuse scoring.
- Deduplicate by object id; fan out to a random subset of peers (default 4);
  never fan out back to the sender.
- Pull-sync on connect: `status` → headers → missing blocks.
- Size caps: 1 MB block, 256 KB PGN, 64 KB tx, 100 proofs in the mempool per
  miner per epoch (anti-spam policy).
- No authentication, TLS optional, rate limiting by IP and identity.

### 5.13 CLI surface

```text
sccp wallet new|address|balance|send|history|export-mnemonic|import-mnemonic
sccp node run --network testnet --port 8645 --data-dir ./data
sccp matchmaker run --network testnet --node http://127.0.0.1:8645 --port 8646
sccp miner run --network testnet --key alice.key --relay URL
             [--engine-path PATH | --engine-profile stockfish-17|stockfish-11|mock]
             [--nodes-per-move N] [--depth D] [--max-games-per-epoch N]
sccp miner register --key alice.key --bond 10 --endpoint https://...
sccp miner availability --key alice.key --epochs 3 --max-games 3
sccp chain info | blocks | account ADDRESS | verify
sccp verify pgn FILE [--reference-engine PATH] [--network testnet]
sccp verify proof FILE [--network testnet]
sccp devnet up --miners 4 --blocks 10 --engines mock
sccp faucet send --to ADDRESS --amount 1000   # testnet only
```

Global flags: `--network`, `--data-dir`, `--passphrase-file`, `--log-level`,
`--json`. All commands are thin wrappers over library functions so every
operation is testable in-process without a CLI.


---

## 6. Milestones

Each milestone ends with a working demo, green tests, clean `ruff`/`mypy`, and an
updated `docs/PROTOCOL.md` if any wire format changed. Sizes are relative
(S = a few days, M = about a week, L = multiple weeks of focused work).

### M0 — Foundations (S/M)

**Goal:** an installable package, a canonical encoding, wallets, and CI.

Deliverables:

- `pyproject.toml` (hatchling backend, `src/` layout, entry point
  `sccp = "sccp.cli:app"`, optional `dev` extra), `Makefile`, `.github/workflows/ci.yml`
  (ruff, mypy, pytest on 3.11/3.12/3.13).
- `src/sccp/config.py`: `NetworkParams` pydantic models for `testnet`,
  `mainnet`, `devnet`; env/TOML overrides; validation of ranges.
- `src/sccp/serialization.py`: canonical JSON, domain-separated hashing, typed
  errors for floats/unsupported types.
- `src/sccp/crypto/{keys,address,wallet}.py`: Ed25519, bech32, keyfile
  encryption, mnemonic backup encoding.
- `src/sccp/cli.py`: `--version`, `wallet new|address|export-mnemonic`, `--help`
  for future commands.
- `tests/unit/test_config.py`, `test_serialization.py`, `test_keys.py`,
  `test_address.py`, `test_wallet.py`; golden vectors in
  `tests/fixtures/crypto_vectors.json`.

Acceptance criteria:

- `sccp wallet new --network testnet --out /tmp/a.key` writes a 0600 keyfile;
  `sccp wallet address --key /tmp/a.key` prints a valid `sccpt1...` address and
  refuses to run on the wrong network.
- Encrypt → decrypt → sign → verify round-trips; wrong password and tampered
  ciphertext fail cleanly.
- Canonical JSON golden hashes are stable across Python versions; floats raise.
- `make test`, `make lint`, `make typecheck` pass; coverage of `crypto/` and
  `serialization.py` ≥ 95%.

### M1 — Chess core (M)

**Goal:** turn a PGN into a verified, canonical game; drive engines; ship the
opening book.

Deliverables:

- `src/sccp/chess/pgn.py`: `validate_pgn`, `ParsedGame`, canonical game hash,
  result/termination derivation, typed errors.
- `src/sccp/chess/openings.py`: load/validate `data/openings.json`, deterministic
  selection from a 32-byte beacon, legality checks for every line.
- `data/openings.json`: at least 64 balanced lines, ≤ 12 plies, legal SAN,
  stable `line_id`, book version.
- `src/sccp/chess/engines.py`: `Engine` protocol, `UciEngine` (wraps
  `chess.engine.SimpleEngine.popen_uci`), `MockEngine` (random legal mover,
  seeded), `ScriptedEngine` (predefined moves for tests), engine profiles
  (`stockfish-17`, `stockfish-11`, `mock`), `engine_fingerprint()`.
- `src/sccp/chess/gameplay.py`: `play_game(white, black, opening, limits, hooks)`
  producing `RawGame` (SAN list, PGN, result, termination, plies), with move
  validation, max-ply guard, resignation hook, and no wall-clock in the result.
- `scripts/fetch_engine.py`: pinned URL + SHA-256 for current and older
  Stockfish; `--dest ./bin`; refuses checksum mismatches.
- Tests: `test_pgn_valid.py`, `test_pgn_invalid.py`, `test_game_hash.py`,
  `test_openings.py`, `test_engines_mock.py`, `test_gameplay.py`;
  `tests/integration/test_stockfish_smoke.py` (skipped when no binary).

Acceptance criteria:

- A legal game validates; each of these fails with a typed error: illegal SAN,
  illegal castling, wrong `Result` header, header/certificate mismatch,
  `Result "*"`, too few/many plies, a "resignation" in a won position, multiple
  games in one payload.
- The same moves with different headers/comments/clocks hash identically; a
  different move order hashes differently.
- `MockEngine` vs `MockEngine` plays a complete game deterministically given a
  seed; games never exceed `max_plies`.
- Every line in `data/openings.json` is legal and selectable; selection is
  deterministic and uniform enough (chi-square smoke test over 10k draws).
- With a real Stockfish, `play_game` finishes a 50k-node/move game and the
  resulting PGN validates.
- `chess.pgn.read_game` gotchas from section 5.8 each have a regression test.


### M2 — Matchmaking, beacon and transcripts (M)

**Goal:** two miners get an unpredictable, verifiable pairing and produce a
proof that anyone can check offline.

Deliverables:

- `src/sccp/protocol/models.py`: pydantic models for `Availability`, `Reveal`,
  `Matchup`, `MatchCertificate`, `GameResult`, `WinProof`, `Beacon`.
- `src/sccp/protocol/beacon.py`: commit/reveal derivation, drand mixing,
  fallback rules, reveal-set serialization, validation of a beacon from public
  data.
- `src/sccp/protocol/pairing.py`: deterministic pairing with constraints and
  quotas; `verify_pairing(match, roster, beacon)`.
- `src/sccp/protocol/transcript.py`: certificate creation, dual signing,
  transcript storage, PGN assembly with protocol headers.
- `src/sccp/protocol/verification.py`: `verify_win_proof(proof, context)` with
  ordered cheap checks and typed failure reasons.
- `src/sccp/miner/matchmaker.py`: relay service (FastAPI) with endpoints
  `POST /v1/matches/{id}/join|move|resign|result`, in-memory match registry,
  round scheduler, timeout handling.
- `src/sccp/node/routes.py` stubs for epoch/beacon/match reads so the relay and
  node can share models.
- Tests: `test_beacon.py`, `test_pairing.py`, `test_transcript.py`,
  `test_verification.py`, `test_relay_flow.py`.

Acceptance criteria:

- Same roster + beacon + round ⇒ identical pairings on every platform; changing
  one reveal changes the pairing set.
- Withholding a reveal is detectable; a commit without a reveal removes the
  miner from that epoch and is recorded.
- A complete 2-miner mock-engine match over the ASGI relay produces a
  certificate with both signatures and a `WinProof` that passes verification.
- Tampering tests fail verification: swapped colours, changed opening, swapped
  result, missing opponent signature, replayed match id, a pairing that the
  beacon did not produce.
- Relay restart preserves transcripts (SQLite or JSON store) and does not
  corrupt pending matches.

### M3 — Chain, state and wallet service (L)

**Goal:** a real chain that accepts claims and pays miners.

Deliverables:

- `src/sccp/chain/tx.py`: tx validation/application for every type in 5.4.
- `src/sccp/chain/block.py`: header/body, merkle roots, seal check, size limits.
- `src/sccp/chain/state.py`: accounts, bonds, registries, miner stats, epoch
  records, snapshots and restore.
- `src/sccp/chain/chain.py`: append, validate, fork choice, reorg, orphan
  handling.
- `src/sccp/chain/mempool.py`: tx/proof pools, nonce/duplicate handling, fee
  ordering, expiry, rebroadcast on reorg.
- `src/sccp/chain/difficulty.py`: retarget rules and clamps.
- `src/sccp/chain/storage.py`: SQLite schema, WAL mode, snapshots, migrations.
- `src/sccp/wallet/service.py`: balances, send, stake/register, history,
  fee estimation.
- `src/sccp/miner/block_builder.py`: select `k` proofs + txs, build coinbase,
  seal, submit.
- `genesis/testnet.json`, `genesis/mainnet.json`, `scripts/make_genesis.py`
  (deterministic timestamps and allocations; testnet faucet only).
- Tests: `test_tx_validation.py`, `test_state.py`, `test_block.py`,
  `test_difficulty.py`, `test_fork_choice.py`, `test_mempool.py`,
  `test_storage.py`, `test_wallet_service.py`, `test_block_builder.py`.

Acceptance criteria:

- A double spend, wrong nonce, insufficient balance, wrong network and bad
  signature are all rejected with distinct errors.
- Bond lifecycle: register locks the bond, unregister starts the timer, early
  withdrawal fails, late withdrawal succeeds.
- Coinbase is unspendable until maturity; subsidy halves at the configured
  height; fees are paid exactly once.
- A block with fewer/more than `k` proofs, a proof from another miner, a reused
  game hash, or a stale epoch is rejected.
- Fork choice picks the heaviest chain; a 3-block reorg restores state and
  returns orphaned proofs to the mempool.
- Restarting the node from SQLite reproduces the tip, balances and registries
  exactly.
- `sccp devnet up --miners 4 --blocks 10 --engines mock` produces 10 valid
  blocks and a per-account balance ledger that sums to the subsidy.


### M4 — Node, gossip and miner daemon (M/L)

**Goal:** a multi-process network on localhost and a miner that runs
unattended.

Deliverables:

- `src/sccp/node/app.py`, `routes.py`, `service.py`: full API from section 5.12,
  startup checks (genesis hash, network id, reference engine policy), graceful
  shutdown.
- `src/sccp/node/p2p.py`: envelope signing/verification, dedupe, fanout, peer
  table, header/block sync, reconnect/backoff, ban scoring.
- `src/sccp/miner/engine_miner.py`: registration, availability commits/reveals,
  relay connection, match loop, dual signing, proof submission, retry/backoff.
- `src/sccp/cli.py`: all commands from 5.13, structured `--json` output.
- `docker-compose.yml` for 1 node + 1 relay + 3 miners on testnet.
- Tests: `test_api.py` (ASGI), `test_gossip.py`, `test_sync.py`,
  `test_miner_loop.py`, `tests/e2e/test_local_network.py`.

Acceptance criteria:

- `sccp devnet up --miners 4 --blocks 10` runs node + relay + 4 mock miners in
  one command, mines 10 blocks, and prints a summary.
- Two nodes on localhost converge on the same tip after a partition heals.
- A miner survives relay restarts, duplicate matches, opponent disconnects and
  stale availability without corrupting its wallet or losing signed transcripts.
- The miner never plays a move for a match it was not assigned, and refuses to
  sign a transcript whose game hash or tags it cannot reproduce.
- API responses are pydantic-validated and include the network id; the OpenAPI
  schema is generated in CI and checked for accidental breaking changes.

### M5 — Quality verification, challenges and threat-model tests (L)

**Goal:** make fabrication and self-play expensive enough to be unattractive,
and prove it with adversarial tests.

Deliverables:

- `src/sccp/chess/quality.py`: implementation from 5.9, deterministic reference
  profile, summary + trace hashing, cached analysis mode.
- `src/sccp/protocol/challenges.py`: challenge lifecycle, bonded adjudication,
  slashing, reward vesting and burning.
- `src/sccp/node/quality_sampler.py`: deterministic sampling, async job queue,
  retry and disagreement handling.
- `docs/THREAT_MODEL.md`: assets, adversaries, attack trees, mitigations,
  residual risks, and explicit non-goals.
- `tests/adversarial/`: fabricated short mate, weak-loser self-play, poisoned
  engine output, duplicate games across forks, colluding pair, withheld reveal,
  transcript tampering, engine version mismatch.
- Benchmarks: games/minute with mock engines; analysis seconds/ply at depth 14;
  full block verification time.

Acceptance criteria:

- A fabricated scholar's mate with no certificate fails; one with a certificate
  fails quality because the loser's moves exceed the CPL budget.
- A self-play pair where the loser uses a weak engine is flagged; where both use
  strong engines it passes (documented residual risk).
- A challenge against a passing proof fails and slashes the challenger; a
  challenge against a failing proof succeeds, slashes the miner and burns the
  unvested coinbase.
- Sampler disagreement rate on two different CPUs is measured and documented.
- Threat-model document reviewed; every listed attack has a test or an explicit
  "accepted risk" note.

### M6 — Testnet launch (M)

**Goal:** a network other people can join without reading the source.

Deliverables:

- Frozen `genesis/testnet.json` and a published genesis hash in the README.
- `docs/RUN_A_TESTNET_MINER.md`: install, engine setup (current + older
  Stockfish), wallet, registration, bond, firewall notes, troubleshooting.
- Faucet CLI/endpoint with rate limits.
- `docker-compose.yml` + a seed-node example; Prometheus metrics endpoint and a
  minimal Grafana dashboard JSON.
- Block explorer (read-only HTML page served by the node) showing blocks, games,
  PGNs, proof verification status and challenges.
- Ops runbook: restart, reorg, stuck mempool, beacon/drand outage, engine crash.

Acceptance criteria:

- A clean machine can follow the guide and mine within 15 minutes.
- The explorer renders a block with a full PGN and a green verification badge.
- Metrics expose height, peers, games played, draws, wins, mempool depth,
  challenges and analysis time.
- A 72-hour soak test with 4+ miners on separate machines has no crashes, no
  stuck chain and no unexplained verification failures.

### M7 — Hardening and mainnet readiness review (L)

**Goal:** decide whether the design deserves real money. It may not.

Deliverables:

- External review of consensus, cryptography and the chess proof protocol.
- Fuzzing of PGN/tx/block decoders; property tests for state transitions.
- Performance work: incremental state root, batched signature verification,
  engine analysis cache, snapshot pruning.
- Governance proposal for slashing/adjudication and parameter changes.
- Written go/no-go with residual risks, economic modelling (self-play cost,
  draw rate, reward variance, bond sizing) and a mainnet launch checklist.

Acceptance criteria:

- No open critical/high findings from review.
- 30-day testnet run with mainnet-like parameters (real engines, quality sampling
  on, bonds active) and stable block times.
- Documented economic analysis shows self-play plus fabrication costs exceed the
  expected reward under stated assumptions.
- Mainnet genesis is reproducible from `scripts/make_genesis.py` and its hash
  matches the published value.


---

## 7. Testing strategy

### 7.1 Layers

| Layer | Scope | Speed | Runs |
| --- | --- | --- | --- |
| Unit | Pure functions: hashing, keys, PGN, state transitions, pairing | ms | every commit |
| Property | Encoders, state machine invariants, pairing constraints | < 5 s | every commit (Hypothesis profiles: `ci`, `dev`) |
| Integration | Module boundaries: node + storage, relay + miner, engine + game | < 60 s | every commit |
| End-to-end | Full local network with mock engines, sync, reorg, claims | 1–3 min | every PR |
| Engine | Real Stockfish smoke, quality analysis, cross-version | minutes | nightly + `-m engine` |
| Adversarial | Fabrication, self-play, tampering, DoS | minutes | nightly + `-m adversarial` |
| Fuzz | PGN, tx, block, gossip decoders | minutes | nightly |

Pytest markers: `engine`, `adversarial`, `slow`, `network`. The default run
deselects all four so `pytest` stays fast and hermetic. Engine tests skip
cleanly with a clear message when no binary is present (this machine has no
Stockfish, so CI installs a pinned binary in a separate job).

### 7.2 Rules

- **No network in unit/integration tests.** Use `httpx.ASGITransport`, fake
  clocks, temp dirs, seeded randomness.
- **Determinism.** Every test that touches engines uses `MockEngine(seed=...)` or
  `ScriptedEngine`. Real engines are only in `-m engine` tests, and those assert
  outcomes (game validates, verdict present), never exact evals or move choice.
- **Golden vectors** live in `tests/fixtures/` and are checked into git:
  canonical JSON hashes, key/address pairs, bech32 vectors (valid + invalid
  checksums), genesis hashes, a known PGN → game hash pair.
- **Property tests** (Hypothesis): canonical JSON round-trip and sort stability;
  bech32 encode/decode; address validation; `apply_tx` never creates coins;
  pairing never duplicates a seat; merkle proofs verify for random sets; reorg to
  any shorter fork restores the pre-fork state; `validate_pgn` never raises a
  non-`PgnValidationError` for hostile input.
- **Golden-chain tests**: a scripted 20-block chain committed as JSON; any
  consensus change must update it deliberately.
- **Coverage gates**: `crypto/`, `serialization.py`, `chain/state.py`,
  `protocol/` ≥ 95%; overall ≥ 85%. No gate for the CLI.
- **Mypy strict** on `src/sccp`; `ruff` with `E,F,W,I,UP,B,SIM,RUF`.
- **No silent skips** in CI: `-m engine` failures fail the nightly job.

### 7.3 What gets a regression test

Every bug fixed in `pgn.py`, `state.py`, `chain.py` gets a named test with a
comment linking the issue. The python-chess gotchas in 5.8 and the "verified
behaviour" notes in this document are all treated as test obligations, not
folklore. When an assumption here is contradicted by the installed library
version, the plan is wrong and gets updated with the test that proved it.

---

## 8. Threat model (outline)

Full version in `docs/THREAT_MODEL.md` at M5.

### 8.1 Assets

1. Block rewards and fees.
2. Ledger integrity (no inflation, no double spends).
3. Uniqueness of games (no replay, no reuse).
4. Fairness of matchmaking (no opponent shopping).
5. Wallet keys and miner identity.
6. Network liveness.

### 8.2 Adversaries

- **Lazy miner**: fabricates PGNs without playing.
- **Self-play miner**: runs both seats, weakening the loser.
- **Sybil pair**: many identities controlled by one entity to guarantee wins.
- **Colluding cartel**: many strong miners sharing wins to dominate issuance.
- **Matchmaker**: censors, reorders or lies about pairings.
- **Node**: serves invalid blocks, withholds proofs, eclipses peers.
- **Chain reorg attacker**: rewrites history to double-claim a game.
- **DoS attacker**: floods PGNs, proofs, gossip, or expensive analysis jobs.
- **Engine poisoner**: ships a binary that reports fake evals to pass quality.

### 8.3 Attack → mitigation → residual risk

| Attack | Mitigation | Residual risk |
| --- | --- | --- |
| Fabricated game | Dual signatures, PGN replays legally, opening matches beacon, quality policy | Novel legal-looking fabrication not caught by CPL thresholds |
| Weak-loser self-play | Both sides checked against a reference profile, bonds, challenges | Two strong engines pass; cost is the only defense |
| Sybil pairing | Identity-diverse pairing heuristics, bond cost, funding/endpoint clustering | Determined attacker with distinct infrastructure |
| Opponent shopping | Commit-reveal + drand beacon, pairing recomputed by every node | Last-revealer bias (removed by drand mixing) |
| Matchmaker censorship | Any node recomputes matchups; miners can submit proofs earned elsewhere | Liveness only if a relay exists |
| Replayed game | `game_hash` registry, `match_id` registry, proof ids | Reorg window reuse handled by rebuilding registries |
| Invalid block | Full validation on receipt, headers-first sync, peer scoring | Bug in validation code |
| Long-range reorg | Bonded finality heuristics later; coinbase maturity; checkpoint file | Not solved in v1 |
| DoS via analysis | Sampled verification, rate limits, proof size caps, job queue backpressure | Node operator must tune |
| Engine poisoning | Pinned binaries/net by hash on mainnet, cross-version checks | Proprietary cheating engines are out of scope |

### 8.4 Explicit non-goals

- Privacy: addresses and games are public.
- Resistance to a well-funded attacker running honest strong engines; the design
  makes cheating *cost as much as competing honestly*, not impossible.
- Sybil resistance without stake: identity is economic, not biometric.
- Real-time play: no clocks, no human tournament integration.


---

## 9. Cost model (first estimates, to be measured at M5)

All numbers are **estimates to be validated by benchmarks**, not claims.

Assumptions: `k = 3` wins/block, 60% draw rate (so ~5 games per win), ~80 plies
per game, `nodes_per_move = 250_000`, 600 s target block time, a modern desktop
core at ~1–5 Mnodes/s for a pinned Stockfish at a shallow node limit.

- Games per block: `k / (1 - draw_rate) / (1 - rematch_loss)` ≈ 15.
- Engine work per block: 15 games × 80 plies × 0.25 Mnodes ≈ 300 Mnodes ≈ 2–5
  CPU-minutes at 1–2 Mnodes/s. That is a few cores keeping up with a 10-minute
  block, which is the intended shape: mining is parallel and CPU-bound, not a
  lottery of wasted hashes.
- Verification: one sampled game per block at depth 14 is roughly 10–40 s of a
  single core; at 5% sampling the expected work is far below that. Full audit of
  a history costs `games × analysis_time`, so consumers never do it inline.
- Draw rate matters more than engine strength: at 80% draws the cost per block
  doubles. This is why the opening book is diverse, the pairing rotates, and
  draws earn nothing (miners are incentivised to play on).
- Reward variance: a miner with `wins_required = k` games must win `k` in a row
  against comparably strong opponents; with a 50% win rate, expected blocks per
  miner is high and variance is punishing. Mitigation ideas in open questions.

## 10. Open questions

Consensus and protocol:

1. Should `k` and `nodes_per_move` be the same difficulty lever, or should nodes
   per move be fixed by the quality policy? (Current: `k` for difficulty, nodes
   pinned by profile.)
2. Should proofs allow resignation at all, or require checkmate to make
   verification cheaper and less subjective?
3. Should the quality policy ever be consensus-critical (e.g. depth 8 inline) for
   small blocks, with sampling only for larger ones?
4. How are contract/upgrade forks signalled? (Current: `version` in header +
   activation height in config; no on-chain governance.)
5. Do we need light clients in v1? `state_root`/`roster_root` are committed but
   unused until a verifier exists.
6. Should game hashes include the engine fingerprints to prevent a strong engine
   replaying a book game against a weak one? (Current: no, uniqueness only.)
7. Is one global relay acceptable, or do we need relay discovery and incentives?
8. What is the bond size relative to expected reward, and does slashing burn or
   redistribute?
9. How do we handle an engine crash or timeout mid-game in a way that is
   verifiable rather than relay-policy?
10. Should availability be per-round rather than per-epoch to reduce idle time?

Economics and operations:

11. Reward variance for small miners; possible proportional pool or smoothing.
12. Whether draws should earn a small consolation to discourage reckless play.
13. Swap/testnet coin distribution and faucet abuse.
14. Long-term data growth: full PGNs on every node vs pruned mode with proof
    availability guarantees.
15. Legal/licensing: Stockfish is GPL-3.0; the network must not require shipping
    it, only referencing pinned builds.

## 11. Definition of done

Per change:

- Tests added at the right layer; `make check` (lint + types + tests) green.
- Wire-format changes update `docs/PROTOCOL.md` in the same PR.
- Consensus changes include a golden-chain update and a migration note.
- No new dependency without a line in section 3 explaining why.

Per milestone:

- Demo command documented and reproducible from a clean checkout.
- README/quickstart still accurate; broken instructions are a bug.
- Known limitations added to section 8.4 or the open-questions list.
- Tagged release (`v0.x.y`) with a short changelog.

Before mainnet:

- External review closed; economic model written; 30-day testnet run; genesis
  hash published; incident runbook rehearsed; legal review of the token
  distribution (or an explicit decision to run testnet-only indefinitely).

---

## 12. Immediate next steps

1. Review this plan and the README for naming and scope agreement.
2. Start M0: `pyproject.toml`, `src/sccp/{config,serialization,crypto,cli}.py`,
   tests, CI.
3. Then M1, which is where the chess-specific risk lives: PGN validation and the
   engine abstraction. Everything else depends on getting those two right.

