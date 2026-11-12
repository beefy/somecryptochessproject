# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning. Versions before 0.3.0 predate this file; their contents are
summarised in the M0–M2 rows of the README roadmap.

## [Unreleased]

Milestone M4: the node, P2P gossip, the miner daemon, relay-tracked clocks and
the `chain`, `miner` and `devnet` CLI commands. Nothing has landed yet; 0.3.0 is
the M3 baseline.

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
