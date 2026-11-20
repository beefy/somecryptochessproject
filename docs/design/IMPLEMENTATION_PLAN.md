# Next steps and improvement areas

> Status: protocol v1 through release 0.34.0 is implemented -- the chain, the
> relay, the engine miner, the hybrid wallet, the browser wallet and explorer,
> variant assets, the native DEX and the HTLC bridge. This is a forward-looking
> engineering document. It lists the work still needed, in priority order, and
> the areas where the current implementation should be improved. Wire formats
> and consensus rules are normative in [`PROTOCOL.md`](PROTOCOL.md); the
> adversary analysis is in [`THREAT_MODEL.md`](THREAT_MODEL.md); the design the
> release train followed is in [`BIG_PICTURE.md`](BIG_PICTURE.md).

---

## 1. Current baseline

The reference implementation covers the full wallet-to-block path:

- Hybrid Ed25519 + ML-DSA-65 keys (the only scheme), version-1 bech32 addresses
  over the full digest, encrypted keyfiles and mnemonic backup -- and the same
  keyfile ported to the browser, which imports the one `sccp wallet new` writes.
- PGN validation, canonical game hashing, opening-book selection and UCI engine
  abstraction, with offline `sccp verify pgn`.
- Commit/reveal beacons, deterministic pairing, signed match certificates,
  timed relay sessions and offline `sccp verify proof`.
- Account ledger, transfers, fees, bonds, registration, availability, blocks,
  difficulty retargeting, fork choice, reorg handling and SQLite persistence
  with snapshot-checked replay.
- Variant assets: Chess960 proofs mint C960, an asset with its own supply
  counter, cap and halving schedule beside native SCCP.
- The native DEX (`create_pool`, `add_liquidity`, `remove_liquidity`, `swap`
  with `min_amount_out`) and the ETH↔SCCP HTLC bridge (`htlc_lock`,
  `htlc_claim`, `htlc_refund`, `contracts/EthHtlc.sol`, a signed
  off-consensus order book in `bridge/orders.py` and the `sccp bridge` command
  group).
- FastAPI node, signed gossip, peer scoring, pull-sync and a node-side proof
  settler.
- Unattended engine miner with `--variant`, block builder, in-process devnet and
  the complete `sccp` command surface (`wallet`, `verify`, `matchmaker`, `node`,
  `miner`, `chain`, `devnet`, `faucet`, `dex`, `bridge`).
- The web app in `src/web`: an explorer (blocks, transactions, accounts, games
  with their full PGN and proof, ratings, a playable table) and a browser wallet
  that creates, restores, opens, funds, sends and exports the CLI's keyfile,
  plus DEX and Bridge pages, served beside a landing page from the hosted
  origins.
- A funded four-miner testnet (two standard, two Chess960) with a public
  faucet, deterministic bootstrap, Docker Compose stack, automated deploy
  workflow and operator runbooks. The stack still resets on every deploy.

The remaining work is about operational confidence, closing the gap between the
DEX design and the shipped transaction set, removing trusted edges and preparing
a mainnet decision. It is not a continuation of the protocol build.

### 1.1 Delivered since this page was written

These were listed here as next steps when the testnet was at 0.12.0. They have
shipped, and are recorded so the lists below stay honest.

| Work | Shipped | Where |
| --- | --- | --- |
| Read-only UI and block explorer | 0.19.0–0.28.0 | `src/web`; hosted at `https://app.proofofchess.com`; blocks, transactions, accounts, games (full PGN and proof), ratings, playable table, DEX and Bridge pages |
| Browser wallet with in-tab signing | 0.22.0–0.29.0 | `src/web/src/views/Wallet.tsx`, `lib/keys.ts`, `lib/tx.ts`; the keyfile picker landed in 0.29.0 |
| Variant assets and Chess960 settlement | 0.23.0, 0.25.0, 0.27.0 | `config.py: CHESS960`, `chain/tx.py`, `chain/block.py`, `miner --variant` |
| Native DEX | 0.24.0 | `chain/dex.py`, `/v1/pools`, `src/web/src/views/dex`, `sccp dex` |
| ETH↔SCCP HTLC bridge and signed order book | 0.24.0 | `chain/tx.py` (HTLC appliers), `bridge/`, `contracts/EthHtlc.sol`, `src/web/src/views/bridge`, `sccp bridge` |
| Genesis premine and testnet bootstrap | 0.25.0 | `genesis/`, `docs/setup/TESTNET_DEPLOYMENT.md` |
| Hosted app, landing page and split origins | 0.26.0 | `.github/workflows/deploy-explorer.yml`, `docs/setup/EXPLORER_DEPLOYMENT.md` |

---

## 2. Next steps

### 2.1 Priority 0 — prove the testnet in operation

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Run a continuous public testnet** | The current Compose stack is a deterministic demo that resets on every deploy. A real testnet needs rolling chain state and repeatable miner participation. | A hosted chain runs for at least 72 hours with two or more independent miners, survives restarts and publishes blocks throughout. |
| **Add metrics and alerting** | Height, peer count, mempool depth, relay sessions and faucet grants are currently observed through logs and ad-hoc HTTP calls. | Node and relay expose Prometheus metrics; a dashboard and alerts cover stalled height, peer loss, relay failure, mempool growth and faucet abuse. |
| **Run failure and soak drills** | Unit and integration tests cannot expose clock drift, disk growth, restart races or long-lived peer behaviour. | A documented drill exercises node restart, relay restart, partition/heal, reorg, disk pressure and clock skew; results and fixes are published. |

The explorer part of this priority is done (section 1.1): a hosted read-only UI
serves the public testnet and every game page shows the proof that settled it.
What it still lacks is an indexer -- account history is a bounded scan over
`GET /v1/blocks` -- and any automated UI test, so it is not yet something an
operator can trust without looking.

### 2.2 Priority 0 — remove trusted operational edges

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Automate timeout-claim submission** (done) | The ledger, clock evidence and local claim verification exist, and `sccp miner run` now signs the claim, submits it to `--node` by default, retries it through the shared nonce sequence, and queues the proof that finalises it. | A flagged timed game produces and submits a claim without operator intervention; the engine-miner test covers submission and proof construction, and `tests/unit/test_chain_timeout.py` covers dispute and finalisation. |
| **Pin a drand quicknet verifier** | `drand_used = 1` can only be verified when a caller injects a verifier. Without the pinned BLS12-381 check, production beacons cannot rely on drand. | A bundled verifier checks quicknet signatures against the pinned chain key; offline `sccp verify proof` accepts drand beacons and rejects bad ones. |
| **Persist faucet limits** | Faucet cooldowns and client quotas are in memory and reset with the node. | Limits survive restarts and are shared by all node workers; abuse tests cover concurrent requests and restart behaviour. |

### 2.3 Priority 1 — harden the system

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Close the DEX execution gaps** | The original design (`BIG_PICTURE.md` §6.2) called for a `deadline_height` on every pool transaction, a single clearing price per pool per block and an optional commit-reveal swap. The shipped `swap` applies in transaction order inside the block and carries only `min_amount_out` (`chain/tx.py: _apply_swap`), so block ordering moves the price a later swap pays, up to that signer's own bound, and a mempool-delayed swap has no expiry. `BIG_PICTURE.md` §6.1-6.2 and `THREAT_MODEL.md` §4 now record the gap as shipped behaviour. | Either `deadline_height` and deterministic per-pool clearing land (a `sccp/v2`-style bump with refreshed golden vectors), or a documented decision keeps sequential execution and every design document matches it; property tests cover the bound and, if clearing lands, the clearing order. |
| **Audit the Ethereum bridge contract** | `contracts/EthHtlc.sol` holds counterparty funds and has had no external review or published static-analysis run. `tests/unit/test_bridge_orders.py` and `contracts/test/EthHtlc.t.sol` cover behaviour, not adversarial Solidity. | An external audit covers `EthHtlc.sol` (timelocks, preimage handling, ether rejection, absence of an owner/upgrade path), a Slither-class scan is published, and findings are closed or documented before mainnet use. |
| **Decide on automated market making** | `sccp mm` (a quoting bot) and `sccp miner --auto-sell` were in the original plan and are not built, so bridge and DEX liquidity is manual. | Either a quoter ships behind the existing signed-order API with inventory limits and a kill switch, or the plan drops it and the docs say liquidity is operator-supplied. |
| **Calibrate bonds and rewards** | Bond sizes are policy choices, not measured security parameters. | A written economic analysis relates bond size and reward value; testnet measurements support the chosen mainnet values. |
| **Populate roster diversity fields** | `funder`, `subnet` and `asn` exist on the roster but nothing fills them, so pairing diversity is a heuristic over empty data. | Registration or the node's roster projection carries real provenance, and a pairing test shows two same-funder accounts are separated. |
| **Pin engine artefacts completely** | The binary SHA-256 is committed, but NNUE networks are not pinned. | Engine profiles pin every network file; a mismatch is visible in the certificate and rejected by the documented policy. |
| **Add storage lifecycle controls** | The node keeps the full chain and every stored PGN. Long-running networks need pruning, snapshots and restore procedures. | Operators can prune old block bodies/PGNs with a documented retention policy, verify state from a snapshot and restore within a rehearsed target. |
| **Harden peer networking** | HTTP gossip has no discovery, encryption, authentication or transport rate limiting. | Discovery or a documented static-peer process, TLS/proxy guidance, per-peer rate limits and DoS tests are in place. |
| **Improve miner and relay resilience** | A miner reconnects, retries and handles multiple relays only as far as current tests cover. | Long-running tests cover relay restarts, stale sessions, clock corrections, duplicate results and multiple relay endpoints. |
| **Expand adversarial and property tests** | The suite is strong on protocol edges but light on multi-node timing and resource exhaustion. | New property and stochastic tests cover reorg/mempool interactions, receipt replay and bounded resource use. |


### 2.4 Priority 2 — mainnet readiness

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Freeze mainnet parameters** | `wins_required`, bonds, maturity and claim window are provisional. | A reviewed parameter set, genesis document and published genesis hash are tagged and reproduced from a clean checkout. |
| **Commission external review** | The protocol and code have not been independently audited. | Consensus, cryptography, wallet handling and the proof/clock lifecycle have an external review; findings are closed or documented. |
| **Decide the economics** | Reward variance, pooling, draw incentives and bond sizing are unresolved. | A published economic model states the intended miner behaviour, attack costs and why the chosen parameters are acceptable. |
| **Rehearse operations** | Mainnet operations need runbooks that have been exercised under failure. | Incident, upgrade, key-rotation, backup/restore and genesis-ceremony runbooks are rehearsed and timed. |
| **Make the launch decision** | Testnet success does not automatically justify mainnet. | An explicit go/no-go record covers audit results, soak data, legal/licensing review and the option to remain testnet-only. |

---

## 3. Areas for potential improvement

### 3.1 Protocol and consensus

- **Finality.** Cumulative work is the only fork-choice rule. Bonded finality,
  checkpoints or a finality gadget would reduce deep-reorg risk.
- **Upgrade signalling.** Header version plus activation height is minimal; a
  documented upgrade and rollback process is still needed.
- **Light clients.** `state_root` and `roster_root` are committed but there is no
  SPV or light-client verifier.
- **Block and fee policy.** Block capacity, fee market and proof-count limits
  have not been stress-tested.
- **Removed quality layer.** There is no `challenge`, `panel_vote` or quality
  summary any more, so there is no appeal rule or ballot penalty to design: the
  layer was built, exercised on a testnet and removed (`PROTOCOL.md` 9.2,
  `WHITEPAPER.md` 16.7). Any quality opinion belongs off chain, against a
  published recipe, with no consensus weight.


### 3.2 Quality and game integrity

- **Quality is not arbitrated.** Nothing on chain compares a move to an engine
  line, so a "too weak to count" rule cannot be enforced (`PROTOCOL.md` 9.2). An
  off-chain indexer is the only place a quality signal can live, and it must pin
  its engine build, options and NNUE weights to be comparable at all.
- **Result evidence.** A checkmate or time forfeit can settle on one seat's
  signature plus the matching board or clock evidence, while a resignation or a
  draw needs both seats, because those attest intent rather than a position
  (`protocol/transcript.py`). Nothing separates a fabricated resignation from a
  real one: that is self-play, priced by the bond and by pairing diversity.
- **Engine provenance.** Binary hashes help, but a repacked cheating engine and
  unpinned networks remain outside the current guarantee.

### 3.3 Economics

- **Reward variance.** Draws earn nothing and `k` wins are required; small
  miners may wait a long time between payouts. Pooling and smoothing are open.
- **Bond sizing.** Registration, witness and timeout bonds need a measured
  relationship to expected rewards and attack cost.
- **Draw incentives.** A small consolation could reduce reckless play, but may
  also subsidise low-quality games.
- **Faucet abuse.** Testnet limits are in memory and unauthenticated; a public
  faucet needs persistent quotas or a stronger gate.
- **Supply and halving.** Mainnet subsidy, halving and the supply cap need a
  reviewed issuance schedule.

### 3.4 Networking and operations

- **Peer discovery.** Static peers are workable for a testnet but not a public
  network.
- **Transport security.** The API has no TLS or authentication; deployments
  must use a reverse proxy and firewall.
- **Rate limiting.** Validation happens before storage, but there is no
  per-client request budget.
- **Data growth.** Full PGNs and block bodies accumulate indefinitely.
- **Observability.** Metrics, tracing and structured alerting are the largest
  operational gaps. The explorer polls the REST API; it is a read-only view, not
  a monitoring system, and it keeps no history of its own.
- **Multi-relay operation.** Proofs are portable, but running and switching
  relays is not yet a first-class workflow.


### 3.5 Developer experience and assurance

- **Specification drift.** `PROTOCOL.md`, the OpenAPI fixture and the code must
  change together; a doc/code consistency check would help.
- **Load and soak testing.** The default suite is fast and deterministic but
  does not measure throughput, latency or long-run memory use.
- **Browser UI assurance.** `src/web` is covered by vitest (196 tests at 0.29.0)
  with a stub DOM, which catches builder and rendering regressions but never
  opens a real file dialog or talks to a live node. There is no Playwright or
  devnet end-to-end run, so "create a wallet, fund it, send, export the keyfile,
  open it with the CLI" is exercised by hand (see `BIG_PICTURE.md` §4.5).
- **Fuzzing.** Expand fuzzing beyond PGNs to transactions, blocks, gossip
  envelopes, pool swaps and HTLC payloads.
- **Release automation.** Version checks exist; reproducible builds, signed
  images and provenance attestations do not.
- **Documentation.** Keep operator guides current as the testnet moves from a
  reset-on-deploy demo to a persistent network.

---

## 4. Working agreements

Every change should still meet the following bar:

- Tests at the right layer; `make check` green.
- Wire-format or consensus changes update `docs/design/PROTOCOL.md` in the same change.
- Consensus changes include a golden-chain update and a migration note.
- New dependencies are justified in the change description.
- README and operator instructions remain accurate; broken instructions are a
  bug.
- A release has a version bump, a changelog entry and a reproducible validation
  command.

Before any mainnet launch:

- External review closed.
- Economic model and mainnet parameters published.
- A continuous testnet run of at least 30 days completed.
- Genesis hash published and reproduced independently.
- Incident, upgrade and key-rotation runbooks rehearsed.
- Legal and licensing review completed, or an explicit decision made to remain
  testnet-only.

