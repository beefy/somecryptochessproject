# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning. Versions before 0.3.0 predate this file; their contents are
summarised in the M0–M2 rows of the README roadmap.

## [Unreleased]

Milestone M5: quality verification, challenges and the threat-model tests. The
`book_version: v1` quality profile, the challenge lifecycle and
`sccp.node.quality_sampler` are still to come.

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
