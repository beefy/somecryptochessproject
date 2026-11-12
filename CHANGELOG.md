# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning. Versions before 0.3.0 predate this file; their contents are
not itemised here.

## [Unreleased]

## [0.11.0] - 2026-11-12

A minor release: the wallet reads the account it spends from out of `--node`, so
`wallet send`, `wallet balance` and `faucet send` work against a remote testnet
with nothing synced and no local chain, and every refusal names the account view
it judged. Amounts are printed in coins rather than base units, so a refused
transfer reads as the amount you typed.

### Added

- `wallet send`, `wallet balance` and `faucet send` read the spendable balance and
  the next nonce from `GET /v1/accounts/{address}` on `--node` (default
  `http://127.0.0.1:8645`). That is the node that will judge the transaction, so
  signing against its answer is the only way the two agree, and it means none of
  the three commands needs a local `chain.sqlite` or a sync. A `--node` named
  explicitly is fatal when it cannot be read; the default one is not, because
  `--data-dir` is still the fallback for building a transfer offline.
- `wallet balance --node`: the balance command accepts a node URL, so the same
  numbers a transfer would spend from are the ones it prints.
- `source` in `wallet balance --json` and `wallet send --json`: the node URL the
  account was read from, or the `chain.sqlite` path used instead.
- `WalletService.from_account_view()` builds a wallet over a single account
  exactly as a node reported it, refusing a document whose `network` or `address`
  is not the one being spent on -- which is what catches a `--node` that serves
  another chain. `WalletService.source` records where its state came from and the
  spendable-balance refusal quotes it.

### Changed

- `wallet send` and `wallet balance` print amounts in coins, not base units: a
  refused `--amount 12` now reads `has 0.00000000 spendable, the transfer needs
  12.00001000 (12.00000000 plus a 0.00001000 fee); this balance came from the
  node at http://127.0.0.1:8645` instead of the bare `1200001000`, and a fee below
  the floor says "base units" so the two units a single command mixes are not
  confused. `--bond` failures are quoted in coins too (`sccp.config.format_coins`
  is now the one formatter the CLI and the wallet share).
- When the empty balance behind a refusal or a `wallet balance` reading was read
  from a `chain.sqlite` that holds only its genesis block, both commands print a
  note on stderr: no node answered, that database knows no other balances, and
  passing the URL of the node that holds the coins as `--node` is the fix (a copy
  taken while the node runs also needs `chain.sqlite-wal`, since a copy of
  `chain.sqlite` alone opens at height 0). See
  [`docs/SEND_COINS.md`](docs/SEND_COINS.md), which also documents the case of two
  chains started from the same genesis, where both sides look the same to a wallet
  but only one holds the coins.

## [0.10.0] - 2026-11-12

A minor release: the testnet chain now stays self-sustaining after its bootstrap.
Blocks past the bootstrap may be heartbeat blocks, the miners commit and reveal
availability on their own, the miner paces itself so the difficulty retarget does
not outrun the roster, and a drip keeps ledger work in the pool when games draw.

### Added

- `sccp node run --mine-interval-seconds N`: the settlement miner can pace
  itself, building at most one block per `N` seconds. A block's timestamp is the
  wall clock and the difficulty retarget reads those timestamps, so a settler
  that built a block on every two-second poll made the chain look thirty times
  faster than `target_block_seconds`; at the height 30 retarget (the first one
  that is also an epoch boundary after the bootstrap) the schedule answered by
  raising `wins_required` to `2`, and a block then has to carry two wins by the
  same miner at the same height -- more than a two-miner roster wins per epoch --
  which stopped the chain. One block per target interval puts the retarget window
  in its neutral band and keeps `wins_required` at `1`. `0` keeps the old
  behaviour; the testnet stack passes `60`.
- `deploy/drip_testnet_traffic.py` and the compose `drip` service: when the node
  reports an empty transaction pool, the drip signs one ordinary faucet transfer
  and submits it through the public `POST /v1/tx`. An epoch is five blocks and a
  two-miner roster only generates four availability/reveal transactions, so an
  epoch whose games all drew ran one block short and froze at the boundary. The
  drip keeps ledger work in the pool, so the epoch crosses regardless of the
  games' results, and it holds no roster slot and needs no node internals.
- Blocks past `bootstrap_end_height` may now carry zero proofs when they carry at
  least one transaction, and such a *heartbeat* block mints no subsidy (fees
  only). A two-miner testnet froze at height 22 because every block demanded
  exactly one proof, all settled proofs are spent once, and the availability and
  reveal transactions that rebuild the roster could therefore never be mined.
  `BlockBuilder` builds the heartbeat block when the mempool has something to
  include and no proof is eligible.
- `sccp miner run --nonce-secret HEX` / `--nonce-secret-file PATH`: the miner now
  commits availability two epochs ahead and reveals it every epoch on its own, so
  the roster keeps growing after the bootstrap's one-off transactions run out.
  The old behaviour is unchanged without the secret.
- `sccp matchmaker run --node URL [--node-poll-seconds S]`: the relay follows the
  chain. `relay.json` pins one epoch, and as the chain opens later matchable
  epochs the relay swaps in their beacon and roster, so matches always reference
  an epoch whose proofs the node still accepts.
- `deploy/init_testnet_stack.py` writes `/stack/keys/miner-N.nonce` and records
  it in `stack.json`; `docker-compose.yml` passes both the secret file to the
  miners and `--node` to the relay.
- `sccp miner run --resign-after-plies N`: the miner resigns on its own turn
  once an active game reaches `N` plies, so games end decisively instead of
  drawing. Off by default; the testnet stack passes `60`, mirroring the devnet
  policy. Two engines of comparable strength draw long games, and a draw has no
  winner, so it yields no proof -- the epoch is then left with only the
  availability and reveal transactions and can stall mid-epoch.

### Changed

- A proofless *heartbeat* block now carries **exactly one** transaction instead
  of the whole pending pool. A block is also the chain's clock, so an epoch of
  `epoch_length` blocks only ends if the ledger supplies that many blocks' worth
  of work; packing every pending transaction into one block spent the same work
  while producing a single block, which froze a proofless epoch one block short
  of its boundary. `BlockBuilder` asks the mempool for one transaction
  (`select_transactions(..., limit=1)`) and the mempool validates the limit.
- Mempool proof eligibility now mirrors the consensus checklist exactly: a proof
  from epoch `e` is selectable from `epoch_end_height(e) + 1` -- the first block
  of the *next* epoch, which is where `check_proof` starts accepting it, because
  the header at `epoch_end_height(e)` still belongs to epoch `e` -- until
  `epoch_end_height(e) + claim_window` closes. `reset` and `add_proof` share the
  same `_settleable` test, so the pool is never stricter than a block (which
  would strand a proof the next block accepts, exactly the block a proofless
  epoch needs to cross its boundary) and never looser (which would offer a
  builder a proof its block cannot carry).
- A *proof* block now carries only the transactions the epoch can spare: the
  builder keeps one pending transaction per block the epoch has left and adds
  only the surplus (the epoch's last block flushes what remains, still bounded by
  `max_block_bytes`). Packing the pool into proof blocks spent an epoch's work
  without buying the epoch's blocks, so a run with four transactions and three
  proofs per epoch still stalled one block short of the boundary that would have
  made the next epoch's proofs settleable. The bootstrap window keeps the old
  behaviour and carries the whole pool, because the deploy scripts seed those
  heights offline one at a time.
- Testnet docs (README, WHITEPAPER 5.2/12.4, PROTOCOL 12.2, RUNBOOK, deploy and
  wallet guides) describe the heartbeat as one transaction per block, state that
  the reference builder's policy -- not consensus -- sets that count, and state
  the settleability window as `(epoch_end, epoch_end + claim_window]`.
- `opponent_cooldown` is now `0` on testnet (and therefore devnet), which
  disables the on-chain rematch rule there while mainnet keeps its cooldown of
  `4`. A small test roster can settle every decisive game it plays instead of
  stranding all but the first proof of an epoch; a two-miner testnet previously
  stalled after its first proof block because every later pairing was rejected
  with "played ... in epoch ...; the cooldown is 1 epochs".

### Fixed

- A miner no longer drops a win proof the node refuses because its epoch has not
  ended yet. `EngineMiner` queues the proof in `pending_proofs` and re-announces
  it on every tick (`_flush_proofs`) until the node accepts it, drops it only
  when the node reports the claim window has closed, and treats "already ..."
  refusals as settled. Announcing once lost the proof for the rest of the epoch,
  so the epoch never closed and the chain starved.
- The relay can restart over its own store. `Matchmaker.from_document` now reuses
  the sessions already stored for `open_round` (via `Matchmaker.has_round`)
  instead of opening that round again, which raised on every restart.
- The node's mempool no longer refuses proofs whose epoch has ended but whose
  claim window is still open. `add_proof` judges a proof against the block it
  would land in (`next_height`) with the same `_settleable` window the block
  checklist uses, and names that window in its error: "too early" is retryable
  and a closed claim window says so.

## [0.9.3] - 2026-11-12

A patch release: the missing-chain-database error hint now shows the command
with the global `--data-dir` option in the right position.

### Fixed

- The missing-chain-database error hint now places the global `--data-dir`
  option before `node run`, matching the CLI syntax it tells you to run.

## [0.9.2] - 2026-11-12

A patch release: the testnet deployment workflow and public documentation use
the public DNS name.

### Changed

- The testnet deploy workflow now targets the public DNS name
  (`sccp@testnet.somecryptochessproject.com`) and verifies
  `http://testnet.somecryptochessproject.com:8645/v1/status` after each deploy.
  The README and operator docs publish that endpoint instead of an IP-shaped
  example.

## [0.9.1] - 2026-11-12

A patch release: documentation and source comments now describe the shipped
protocol and testnet directly, without construction-phase labels.

### Changed

- Documentation and code comments now describe the shipped protocol and testnet
  directly instead of construction-phase labels. `docs/IMPLEMENTATION_PLAN.md`
  is now a forward-looking list of next steps, acceptance criteria and
  improvement areas; protocol details remain normative in `docs/PROTOCOL.md`.

### Fixed

- Running `pytest` through its console script (as CI does) now adds the
  repository root to `sys.path`, so `tests/unit/test_deploy_bootstrap.py` can
  import the root-level `deploy` package during collection.

## [0.9.0] - 2026-11-12

A minor release: the testnet is now deployable, fundable and operable with a
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

- This release completes the planned protocol and testnet scope. A block
  explorer, metrics and a 72-hour soak are follow-on operational work, tracked in
  [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## [0.8.0] - 2026-11-12

A minor release: quality verification, bonded challenges, the threat model and
operator guides for wallets, transfers and miners.

### Added

- Quality verification, bonded challenges and the threat model.
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

Quality verification, the challenge lifecycle and `sccp.node.quality_sampler`
followed in 0.8.0. The timed-session work still needed the on-chain claim
integration: the miner already built and verified the `TimeoutClaim` that the
transaction carries.

## [0.5.1] - 2026-11-12

The node, P2P, unattended miner and the rest of the CLI surface.

### Added

- `sccp node run`: serves the protocol API over uvicorn, replays the chain in
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
  `BlockView`, `PeerView` and `DifficultyView` gained `network`, as the protocol
  API contract requires.
- The node test modules cover the delivered surface: `test_api.py`,
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

The node, P2P gossip, the unattended engine miner and the in-process devnet.

### Added

- `sccp.node.service.NodeService`: one node's ledger, mempool, peer table,
  transcript cache and gossip dedupe, with the reads behind the API
  (`status`, `block`, `account`, `epoch`, `beacon`, `difficulty`, `game`,
  `match`, `transcript`, `mempool_view`, `transaction`, `peers_view`), the
  writes (`submit_transaction`, `announce_proof`, `add_transcript`,
  `ingest_block` with duplicate/orphan/reorg handling) and pull-sync
  (`sync_from`). `HttpPeerClient` speaks the peer API over an injected
  `httpx.Client`.
- `sccp.node.app`: the complete protocol HTTP API (status, params, blocks by
  height or hash, accounts, epochs and beacons, difficulty history, games,
  matches and transcripts, mempool, transactions, transaction and proof
  submission, the challenges placeholder, gossip and peers), with startup
  checks, a lifespan that closes storage, and one error handler per exception
  family so ledger errors become 4xx instead of 500. `sccp.node.routes` keeps
  the earlier read-only entry point working.
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
  project coverage gates — the whole
  package at 85%, `crypto/` and `serialization.py` at 95% — like the `ci`
  workflow does, so a release can build and publish.

## [0.3.0] - 2026-11-12

The chain, the wallet service and the block builder.

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

- At this point the chain is a library: no `chain`, `miner` or `devnet` CLI
  command ships yet, and the relay does not track clocks. Both arrived with the
  node release.
- Quality challenges and proof-equivocation slashing landed in 0.8.0; this
  release rejects `challenge` transactions and `slash_evidence` payloads that
  carry `proof_id` explicitly rather than silently ignoring them.
