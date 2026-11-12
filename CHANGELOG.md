# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning. Versions before 0.3.0 predate this file; their contents are
summarised in the M0–M2 rows of the README roadmap.

## [Unreleased]

## [0.9.0] - 2026-11-12

A minor release: M6 makes the testnet deployable, fundable and operable with a
public faucet, node-side proof settlement, a deterministic two-miner bootstrap
and an automated deployment workflow.

### Added

- Public testnet faucet: `GET /v1/faucet` describes the grant and limits,
  `POST /v1/faucet` funds a recipient from the network's published faucet key,
  and `sccp faucet send` remains the operator path. Each address may receive one
  grant of up to 1000 coins per hour, and each client five grants per hour; the
  limits are in memory and reset with the node.
- Node-side proof settler: `sccp node run --mine` builds blocks around announced
  proofs, with `--miner-address`, `--no-mine-bootstrap` and
  `--mine-poll-seconds`.
- Deterministic testnet bootstrap in `deploy/bootstrap_testnet_stack.py`: it
  builds the empty bootstrap blocks through the public block API, funds and
  registers two miners, commits and reveals availability, and writes the relay
  configuration. `deploy/check_testnet_stack.py` verifies the resulting stack.
- `docker-compose.yml` now runs the node, relay, bootstrap job and two miners
  that use the real `stockfish-19` profile; `SCCP_IMAGE` selects a published
  image for CI while local runs build it.
- `.github/workflows/deploy-testnet-miners.yml`: builds and publishes the image
  to GHCR, then deploys it over SSH to the testnet VM without GCP credentials.
- Operator documentation: `docs/RUNBOOK.md` (restart, faucet, mining and
  incident procedures), `docs/TESTNET_DEPLOYMENT.md` (CI deployment and cost
  overview), `docs/CREATE_A_TESTNET_WALLET.md` (wallet and faucet quickstart),
  `docs/GCP_RESOURCE_SETUP.md` (VM, network, firewall and GitHub wiring), and an
  expanded `docs/RUN_A_TESTNET_MINER.md`.
- Tests for the faucet, node miner, faucet CLI, deterministic bootstrap and
  cross-thread block persistence.

### Fixed

- A node can persist blocks written from FastAPI's sync worker threads: SQLite
  now permits cross-thread use while `NodeService.write_lock` still serialises
  every mutation.
- A block's included transactions and proofs are removed from the mempool after
  ingestion, so a settled proof cannot be mined again on the next block.

### Changed

- The compose testnet is now the funded, registered two-miner stack rather than
  three mock miners; each deploy starts from empty volumes and therefore a fresh
  chain.

### Notes

- M6 remains in progress: the block explorer, metrics and the 72-hour soak are
  still outstanding.

## [0.8.0] - 2026-11-12

A minor release: M5 quality verification and bonded challenges, the threat model,
and operator guides for wallets, transfers and miners.

### Added

- Milestone M5: quality verification, bonded challenges and the threat model.
  - `sccp.protocol.challenges` now selects adjudication panels itself:
    `select_panel` ranks the bonded set by
    `H("sccp/v1/challenge", {beacon, proof_id, member})` (odd `PANEL_SIZE = 3`,
    parties excluded), `panel_verdict` aggregates a complete ballot with no
    stuffing or silence, `challenge_panel` derives the panel from the ledger, and
    `adjudicate_with_panel` resolves the challenge through `resolve_challenge`, so
    bonds and slashing settle deterministically.
  - `sccp.node.quality_sampler` turns a persistent disagreement into a challenge:
    `SamplerResult.summary` keeps the claimed summary, `.reason` is bounded by the
    protocol limit, `.evidence` is canonical JSON with the claimed and observed
    trace hashes, and `challenge_requests()` returns sorted `SamplerChallenge`
    payloads. An engine failure is still never a challenge.
  - `tests/adversarial/test_quality_attacks.py`: fabricated summaries, poisoned
    local engines, self-jury, panel divergence between nodes, weak-loser
    self-play that fails quality and is upheld on a panel vote, and stuffed or
    silent ballots; run with the new `make adversarial` target.
  - `tests/unit/test_quality.py`: scripted-engine coverage of the quality policy
    (blunder counting on each side, the reply-based fallback for moves outside
    MultiPV, decided positions, resignation bounds, the bounded LRU cache,
    strict engine-output parsing and `summary_matches`).
  - `docs/THREAT_MODEL.md`: assets, adversaries, attack → mitigation → residual
    risk tables, attack trees and explicit non-goals, with code references.
  - Operator guides: `docs/OPEN_A_WALLET.md` (create, back up, restore and
    inspect a keyfile), `docs/SEND_COINS.md` (faucet funding, transfers, fees,
    mempool settlement and troubleshooting) and `docs/RUN_A_TESTNET_MINER.md`
    (relay + miner quickstart, real engines, roster edits, registration,
    availability/reveal and operational safety), linked from the README.

## [0.7.0] - 2026-11-12

A minor release: the end-to-end whitepaper, resumable on-chain demo runs and
clearer export and refusal diagnostics.

### Added

- `docs/WHITEPAPER.md`: a descriptive end-to-end whitepaper covering the chain,
  chess proof layer, matchmaking, mining, node/gossip, storage and operations,
  with implemented and planned behaviour kept explicit.
- `scripts/play_miner.py --on-chain`: runs now **resume** an existing
  `--chain-dir` at its next epoch instead of refusing to start; `--fresh`
  explicitly discards it. `--games N` plays several games on that one chain.
  Each proof block also carries the availability/reveal transactions the next
  game's epoch needs, and the demo chain bootstraps three funded opponents so
  consecutive proofs do not trip devnet's `opponent_cooldown`. This removes the
  old "delete `data-play` between games" step.
- The on-chain settlement now names the opponent's refusal when it will not
  co-sign, and the final report states that a non-mined game left the chain
  height unchanged and that a decisive result is required for a proof. When the
  proof is mined, the report names the winning seat and address instead of
  printing the result score next to `winner`.
- `scripts/export_block_games.py` now prints the node URL, network and tip
  before the exported block, and warns when it exports genesis because the node
  it reached serves no games. This makes the common `--network`/`--data-dir`
  mismatch (or an old node still holding port 8645) visible in the output.
- `docs/PLAY_AND_EXPORT.md`: a step-by-step guide for playing a game with
  `scripts/play_miner.py --on-chain`, starting a node on the resulting chain and
  exporting the block's PGN and manifest with
  `scripts/export_block_games.py`, including resume/multi-game and wrong-node
  troubleshooting.

## [0.6.1] - 2026-11-12

A patch release: the timed relay became playable by hand, and the client can now
resign.

### Added

- `scripts/play_miner.py`: play one timed relay match by hand. Solo mode builds
  a relay, a witness key and one real `EngineMiner` in-process (devnet
  parameters, deterministic demo keys, no port, nothing on disk) and drives the
  human's seat over the same HTTP API `sccp devnet up` uses: signed joins,
  seat-signed moves, live clocks, `receipts` on demand, auto-flag and, when the
  human runs out of time, a verified `TimeoutClaim` from the opponent.
  `--relay/--key` instead takes a seat on a relay that is already serving.
  `--on-chain` builds a fresh devnet chain in `--chain-dir` (default
  `./data-play`), seats both players on it and, when the game ends, co-signs the
  result and mines the resulting `WinProof` into a block with the production
  block builder, then prints the block, proof id and a `node run` command that
  serves the chain. Draws, time forfeits and sub-20-ply games report why they
  produced no block. `tests/unit/test_play_miner.py` drives the loop headlessly
  with a fake clock and checks the on-chain settlement end to end.
- `scripts/export_block_games.py`: fetch a node's tip block (or the block given
  by `--block`, a height or hash), replay every proof PGN against its
  certificate, and write one `.pgn` per game plus a `manifest.json` with the
  block, proof and match identifiers. The export refuses a PGN that does not
  reproduce the proof's game hash, result, winner or ply count.
  `tests/unit/test_export_block_games.py` drives it through the node API.
- `RelayClient.resign` posts `POST /v1/matches/{id}/resign` and returns the
  updated `MatchView`, so a client can end its own game on the relay.

### Fixed

- `scripts/play_miner.py` hid the certified opening it started from. A session
  begins with the book line the beacon draws for the match, so the script's
  first prompt was really move 3 of a game the player had never seen: no line,
  no board, no move numbers, and black's reply — a knight that had already
  developed to f6 — read as an illegal first move. It now prints the line, the
  position it left and the move number, and numbers every reported move
  (`you played 5.Nc3`, `opponent played 5...g5`). `--seed` now derives the
  beacon too, so it changes the opening instead of replaying the same book line
  in every solo game.

## [0.6.0] - 2026-11-12

Timed relay sessions, completed: every match now runs the fixed 30+0 control on
the relay's own monotonic clock, with witness-signed receipts, automatic flags
and an opt-in round scheduler.

### Added

- The relay runs clocked sessions (`TIMED_SESSIONS` in `sccp.miner.matchmaker`).
  The second start signature activates a session and starts both clocks with the
  side to move on the clock; a `MoveRequest` may be the signed form
  `{network, match_id, seat, ply, san, clock_ms, signature}`, which is verified
  against the registered seat key before the move is applied.
- `GET /v1/matches/{id}/receipts` returns a `ClockBundle`: the time control, both
  clocks, every move receipt in ply order and the flag receipt if there is one.
  `ClockBundle.receipts` is exactly the `clock_evidence.receipts` of a
  `TimeoutClaim`, so a winner settles a flag without re-deriving the bundle.
- `MatchView` exposes `time_control`, `white_ms`, `black_ms`, `turn`, `last_ply`,
  `flagged` and a `receipts` count, and `/v1/status` reports `timed`,
  `time_control`, `round`, `round_seconds` and the relay's `witness` address.
- The relay signs its receipts when it runs with a witness key
  (`sccp matchmaker run --witness-key KEYFILE`), and auto-flags a clock that
  reaches zero even when the losing seat never answers, so a stall cannot deny
  the win.
- Opt-in round scheduler: `--round-seconds` (or `round_seconds` in the relay
  document) opens the next round on that interval up to `rounds_per_epoch`, and
  `--sweep-seconds` sets how often the serving loop sweeps clocks and rounds.
- `sccp miner run --relay-witness-key HEX` (repeatable) trusts the relay's clock
  witness. The miner pulls the relay's receipts, verifies them locally against
  those keys and builds a bonded `timeout_claim` for a flag it won;
  `MinerTick.claims` reports the match ids it settled.
- `tests/unit/test_relay_timed.py`: activation, per-mover charging, receipt
  issuance, forged-body refusal, flag-on-zero, late-move flagging, restart
  re-basing, the round schedule, the replay-independent reaper and a two-miner
  run that ends in a verified timeout claim.

### Changed

- The relay store is `format: 2`: sessions persist their clock, receipts and flag
  receipt. A live clock is re-based to "now" for the side to move on load,
  because monotonic timestamps from the previous process are meaningless.
- `MatchClock.remaining_ms` returns the stored values for a clock that has not
  started, and `MatchClock.stop(now_ms)` freezes a clock at a known instant.
- `sccp matchmaker run` starts a background service thread that sweeps the clocks
  and opens scheduled rounds; `sccp devnet up` pins the relay's millisecond clock
  so a devnet run stays deterministic.

### Fixed

- The inactivity reaper blamed a seat that had already joined: it compared seat
  *addresses* against `start_signatures`, which is keyed by colour. Only the seats
  that never joined now collect a no-show.
- `timeout_seconds` no longer abandons a live clocked session; abandoning one
  would let the losing seat escape the flag.

The next milestone, M5, is quality verification, challenges and the threat-model
tests. The `book_version: v1` quality profile, the challenge lifecycle and
`sccp.node.quality_sampler` are still to come. What is left of the timed-session
work is the on-chain claim transaction (plan section 5.7.1): the miner already
builds and verifies the `TimeoutClaim` that transaction will carry.

## [0.5.1] - 2026-11-12

Milestone M4, completed: the rest of the CLI surface and the multi-process stack.

### Added

- `sccp node run`: serves the section 5.12 API over uvicorn, replays the chain in
  `--data-dir`, honours `--genesis` and pull-syncs from `--peer` URLs at startup.
  A peer that is down is recorded, skipped and logged, never fatal.
  `sync_peers` in `sccp.node.service` is the library behind it.
- `sccp miner run`: the unattended loop with `--key`, `--relay`, `--node`,
  `--engine-path`, `--engine-profile`, `--nodes-per-move`, `--depth`,
  `--max-games-per-epoch`, `--poll-seconds` and `--iterations`; it reports joins,
  moves, results, proofs and refusals, and supports `--json`.
- `sccp miner register`, `sccp miner availability` and `sccp miner reveal`: sign
  registration, availability commitments and reveals against the chain in
  `--data-dir` and submit them to a node.
- `sccp wallet balance`, `sccp wallet send` and `sccp wallet history`, plus
  `sccp chain info|blocks|account|verify`: read the ledger in `--data-dir`
  (replaying it through full validation) and build transfers from it.
- `sccp faucet send`: the public testnet/devnet faucet, refused on a network
  that has none.
- `sccp devnet up --engines mock`, and `--network` on `matchmaker run`,
  `node run` and the miner commands so a global option behaves the same whether
  it is written before or after the subcommand.
- `docker-compose.yml`, `Dockerfile`, `.dockerignore` and `deploy/`: one node,
  one relay and three miners on testnet. `deploy/init_testnet_stack.py` writes
  deterministic stack keys and a relay config, and `make stack` generates and
  validates the file.
- Genesis documents are force-included in the wheel (`sccp/chain/data/genesis/`)
  and `default_genesis_path` prefers them, so `pip install sccp` followed by
  `sccp node run` works outside a source checkout.
- The OpenAPI contract is snapshotted in `tests/fixtures/openapi.json`, checked
  by `tests/unit/test_api.py` and regenerated with
  `scripts/make_openapi_fixture.py` when a change is intended.
- `tests/e2e/test_local_network.py`: two nodes on real loopback ports converge
  on the same tip after a partition, and a signed envelope crosses the wire.

### Changed

- Every top-level API response now carries the network id: `AccountView`,
  `BlockView`, `PeerView` and `DifficultyView` gained `network`, as section 5.12
  and the M4 acceptance criteria require.
- The M4 test modules are named after the plan's deliverables: `test_api.py`,
  `test_gossip.py`, `test_sync.py` and `test_miner_loop.py`.
- `make lint`, `make format` and CI also cover `deploy/`.

### Fixed

- `EngineMiner` no longer re-joins a pending match on every poll: it compares the
  certificate's start signatures by colour rather than by address, so a seat
  waiting for its opponent makes progress instead of collecting
  "has already joined" refusals.
- `NodeService.sync_from` records a failure for a peer it cannot reach at all, so
  `sccp node run --peer` bans a dead peer instead of silently forgetting it.

## [0.5.0] - 2026-11-12

Milestone M4: the node, P2P gossip, the unattended engine miner and the
in-process devnet.

### Added

- `sccp.node.service.NodeService`: one node's ledger, mempool, peer table,
  transcript cache and gossip dedupe, with the reads behind the API
  (`status`, `block`, `account`, `epoch`, `beacon`, `difficulty`, `game`,
  `match`, `transcript`, `mempool_view`, `transaction`, `peers_view`), the
  writes (`submit_transaction`, `announce_proof`, `add_transcript`,
  `ingest_block` with duplicate/orphan/reorg handling) and pull-sync
  (`sync_from`). `HttpPeerClient` speaks the peer API over an injected
  `httpx.Client`.
- `sccp.node.app`: the complete section 5.12 HTTP API (status, params, blocks by
  height or hash, accounts, epochs and beacons, difficulty history, games,
  matches and transcripts, mempool, transactions, transaction and proof
  submission, the M5 challenges placeholder, gossip and peers), with startup
  checks, a lifespan that closes storage, and one error handler per exception
  family so ledger errors become 4xx instead of 500. `sccp.node.routes` keeps
  the M2 read-only entry point working.
- `sccp.node.models`: frozen, `extra="forbid"` response models, each carrying
  the network id.
- `sccp.node.p2p`: signed gossip envelopes (`sccp/v1/gossip` domain over
  `network`, `kind`, `payload` and `sender`), content-addressed object ids,
  a bounded dedupe cache, random fanout that never echoes to the sender, a peer
  table with failure-based banning, `plan_sync`/`fetch_blocks` pull-sync, and
  `GossipKind`/`GossipError` handling that penalises peers which serve garbage
  while leaving unknown-parent blocks unpunished.
- `sccp.miner.engine_miner`: `EngineMiner`, a synchronous, clock-injected miner
  loop that joins certified matches, replays and signs moves, co-signs results,
  stops before drawing the opponent's clock down, assembles bonded timeout
  claims from verified clock evidence and only announces proofs it can rebuild
  from a transcript. It refuses matches it was not seated in, certificate
  swaps, unreproducible results and transcripts whose game hash disagrees.
- `sccp.devnet` and `sccp devnet up`: a whole network in one process. Funding,
  registration, availability and reveals are signed transactions, the relay is
  the production `Matchmaker` behind its real HTTP API, miners are
  `EngineMiner`s with deterministic mock engines, and the node mines with the
  production `BlockBuilder`. `--miners`, `--blocks`, `--genesis` and the global
  `--json` are supported; the default run mines ten blocks from four miners in
  a couple of seconds without opening a port.
- `sccp.transport.ASGITransport` and `client_for`: a synchronous ASGI transport
  for `httpx>=0.28` (whose own `ASGITransport` is async-only), so the node, the
  relay and the devnet can be exercised in-process.
- `Domain.GOSSIP` and `sccp.errors.DevnetError`.
- Unit tests for the new layers: `test_node_p2p.py`, `test_node_service.py`,
  `test_engine_miner.py`, `test_devnet.py`, plus `devnet up` CLI coverage.

### Fixed

- `EngineMiner` no longer asks the relay for a transcript before the opponent
  has co-signed the result; it returns after recording its own signature and
  finishes the match on a later poll.
- The pinned-certificate check ignores join signatures, and the pinned copy is
  refreshed after joining, so a legitimate join is not mistaken for a
  certificate swap.
- `HttpPeerClient`'s docstring no longer recommends `httpx.ASGITransport`, which
  cannot drive a synchronous client on httpx 0.28.

## [0.4.1] - 2026-11-12

### Fixed

- The publish workflow's `build and verify` job failed on release tags: its
  single test step combined `--cov=sccp` with `--cov=sccp.crypto` and
  `--cov=sccp.serialization`, and coverage.py applies `--cov-fail-under` to the
  union of measured files, so it demanded 95% whole-package coverage (89% actual)
  and the release job stopped before building the distributions. It now runs the
  pair of gates defined in `IMPLEMENTATION_PLAN.md` section 7.2 — the whole
  package at 85%, `crypto/` and `serialization.py` at 95% — like the `ci`
  workflow does, so a release can build and publish.

## [0.3.0] - 2026-11-12

Milestone M3: the chain, the wallet service and the block builder.

### Added

- `sccp.chain.tx`: validation and application for every transaction type in the
  plan (transfers, miner registration and bonds, witness registration, updates,
  unregistration, bond withdrawal, availability commits and reveals, bonded
  timeout claims, disputes and slash evidence), with stable `TxFailure` reasons,
  canonical `txid`, first-use public-key reveal and a coinbase constructor.
- `sccp.chain.state`: accounts, bonds, miner statistics, clock-witness registry,
  epoch records, timeout claims, `state_root`/`roster_root` and reversible
  snapshots.
- `sccp.chain.block`: headers, body merkle roots, coinbase value and maturity,
  proof rules, epoch beacon and roster roots, median-time-past timestamps, seal
  verification and `state_root` recomputation.
- `sccp.chain.chain`: block store, append, heaviest-chain fork choice, reorgs and
  orphaned transaction/proof recovery.
- `sccp.chain.mempool`: projected-state admission, one entry per
  `(sender, nonce)` and per proof id, deterministic fee-then-address ordering,
  expiry and selection under the block size cap.
- `sccp.chain.difficulty`: `wins_required`/`seal_bits` retargeting, `block_work`
  and `seal_valid`.
- `sccp.chain.storage`: SQLite persistence (WAL, schema version, block JSON, tip
  and snapshot replay) with integrity checks on load.
- `sccp.chain.timeout`: bonded `timeout_claim`/`timeout_dispute` lifecycle,
  dispute deadlines, finalisation through a `time_forfeit` proof and
  clock-witness equivocation slashing.
- `sccp.chain.genesis`: genesis documents, allocation validation (address, key
  derivation, positive amounts, duplicates), pinned-hash checks, faucet keypairs
  for test networks and `load_genesis`/`Chain.from_genesis_file`.
- `sccp.wallet.service`: balances, transfers, staking/registration helpers, fee
  estimation and on-chain history.
- `sccp.miner.block_builder`: proof and transaction selection, coinbase,
  `state_root` precomputation, sealing and build-and-append.
- `genesis/devnet.json`, refreshed `genesis/testnet.json` and
  `genesis/mainnet.json`, regenerated deterministically by
  `scripts/make_genesis.py`.
- `docs/PROTOCOL.md` sections 10–13: transaction envelopes and payloads,
  bonded timeout settlement, ledger state, blocks, difficulty, fork choice and
  genesis documents.

### Changed

- `Chain`, `Mempool` and block validation take the epoch's `OpeningBook`, so
  proofs are replayed against the same book the challenger used.
- `expected_interval_ms` renamed to `expected_interval_seconds`; block
  timestamps are Unix seconds.
- `MatchCertificate` carries the fixed `TimeControl`, and proofs carry clock and
  move evidence for `time_forfeit` and refused-checkmate settlements.
- `NetworkParams` gained the ledger constants: pinned `genesis_hash`,
  `mempool_ttl_blocks`, `snapshot_interval` and `reorg_depth`.

### Fixed

- Transfers debit the sender as well as crediting the recipient.
- A timeout claim's accused seat is the flagged seat, and the claim's last move
  must be the claimant's own, so a claim cannot invert the parties.

### Notes

- M3 is a library milestone: no `chain`, `miner` or `devnet` CLI command ships
  yet, and the M2 relay does not track clocks. Both are M4 work.
- Quality challenges and proof-equivocation slashing are M5 work; M3 rejects
  `challenge` transactions and `slash_evidence` payloads that carry `proof_id`
  explicitly rather than silently ignoring them.
