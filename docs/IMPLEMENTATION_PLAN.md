# Next steps and improvement areas

> Status: protocol v1 and the 0.12.0 testnet release are implemented. This is a
> forward-looking engineering document. It lists the work still needed, in
> priority order, and the areas where the current implementation should be
> improved. Wire formats and consensus rules are normative in
> [`PROTOCOL.md`](PROTOCOL.md); the adversary analysis is in
> [`THREAT_MODEL.md`](THREAT_MODEL.md).

---

## 1. Current baseline

The reference implementation covers the full wallet-to-block path:

- Hybrid Ed25519 + ML-DSA-65 keys (the only scheme), version-1 bech32 addresses over the full digest, encrypted keyfiles and mnemonic backup.
- PGN validation, canonical game hashing, opening-book selection and UCI engine
  abstraction, with offline `sccp verify pgn`.
- Commit/reveal beacons, deterministic pairing, signed match certificates,
  timed relay sessions and offline `sccp verify proof`.
- Account ledger, transfers, fees, bonds, registration, availability, blocks,
  difficulty retargeting, fork choice, reorg handling and SQLite persistence.
- FastAPI node, signed gossip, peer scoring, pull-sync and a node-side proof
  settler.
- Unattended engine miner, block builder, in-process devnet and the complete
  `sccp` command surface.
- Deterministic quality sampling, bonded challenges, beacon-ranked panels,
  slashing and the adversarial test suite.
- A funded two-miner testnet with a public faucet, deterministic bootstrap,
  Docker Compose stack, automated deploy workflow and operator runbooks.

The remaining work is about operational confidence, removing trusted edges and
preparing a mainnet decision. It is not a continuation of the protocol build.

---

## 2. Next steps

### 2.1 Priority 0 — prove the testnet in operation

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Run a continuous public testnet** | The current Compose stack is a deterministic demo that resets on every deploy. A real testnet needs rolling chain state and repeatable miner participation. | A hosted chain runs for at least 72 hours with two or more independent miners, survives restarts and publishes blocks throughout. |
| **Add metrics and alerting** | Height, peer count, mempool depth, relay sessions, faucet grants and sampler failures are currently observed through logs and ad-hoc HTTP calls. | Node and relay expose Prometheus metrics; a dashboard and alerts cover stalled height, peer loss, relay failure, mempool growth and faucet abuse. |
| **Ship a block explorer or read-only UI** | Users need to inspect blocks, transactions, accounts, games, proofs and challenges without writing API calls. | A read-only web view (or a maintained static explorer) serves the public testnet and links every game to its proof and challenge status. |
| **Run failure and soak drills** | Unit and integration tests cannot expose clock drift, disk growth, restart races or long-lived peer behaviour. | A documented drill exercises node restart, relay restart, partition/heal, reorg, disk pressure and clock skew; results and fixes are published. |

### 2.2 Priority 0 — remove trusted operational edges

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Automate timeout-claim submission** | The ledger, clock evidence and local claim verification exist, but the CLI/demo does not yet submit a `timeout_claim` automatically. | A flagged timed game produces, submits and settles a claim end to end without operator intervention; an end-to-end test covers dispute and finalisation. |
| **Pin a drand quicknet verifier** | `drand_used = 1` can only be verified when a caller injects a verifier. Without the pinned BLS12-381 check, production beacons cannot rely on drand. | A bundled verifier checks quicknet signatures against the pinned chain key; offline `sccp verify proof` accepts drand beacons and rejects bad ones. |
| **Move panel adjudication on chain** | Challenge panels and verdicts are deterministic, but votes still arrive through an operator-trusted path. | A signed `panel_vote` or `adjudicate` transaction commits ballots; a challenge resolves from chain data alone, with no privileged operator action. |
| **Persist faucet limits** | Faucet cooldowns and client quotas are in memory and reset with the node. | Limits survive restarts and are shared by all node workers; abuse tests cover concurrent requests and restart behaviour. |

### 2.3 Priority 1 — harden the system

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Calibrate bonds and sampling** | Bond sizes, sample rate and panel size are policy choices, not measured security parameters. | A written economic analysis relates bond size, reward value and analysis cost; testnet measurements support the chosen mainnet values. |
| **Pin engine artefacts completely** | The binary SHA-256 is committed, but NNUE networks are not pinned. | Engine profiles pin every network file; a mismatch is visible in the certificate and rejected by the documented policy. |
| **Add storage lifecycle controls** | The node keeps the full chain and every stored PGN. Long-running networks need pruning, snapshots and restore procedures. | Operators can prune old block bodies/PGNs with a documented retention policy, verify state from a snapshot and restore within a rehearsed target. |
| **Harden peer networking** | HTTP gossip has no discovery, encryption, authentication or transport rate limiting. | Discovery or a documented static-peer process, TLS/proxy guidance, per-peer rate limits and DoS tests are in place. |
| **Improve miner and relay resilience** | A miner reconnects, retries and handles multiple relays only as far as current tests cover. | Long-running tests cover relay restarts, stale sessions, clock corrections, duplicate results and multiple relay endpoints. |
| **Expand adversarial and property tests** | The suite is strong on protocol edges but light on multi-node timing and resource exhaustion. | New property and stochastic tests cover reorg/mempool interactions, challenge races, receipt replay and bounded resource use. |


### 2.4 Priority 2 — mainnet readiness

| Work | Why it matters | Done when |
| --- | --- | --- |
| **Freeze mainnet parameters** | `wins_required`, bonds, maturity, claim window and quality policy are provisional. | A reviewed parameter set, genesis document and published genesis hash are tagged and reproduced from a clean checkout. |
| **Commission external review** | The protocol and code have not been independently audited. | Consensus, cryptography, wallet handling and the quality/challenge lifecycle have an external review; findings are closed or documented. |
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
- **Challenge lifecycle.** Panel selection is deterministic, but on-chain votes
  and a clear appeal/finalisation rule remain open.


### 3.2 Quality and game integrity

- **Hardware sensitivity.** Reference-engine analysis varies with CPU, engine
  build and NNUE network. Sampling thresholds should be calibrated across
  machines.
- **Resignation policy.** The resignation check is implemented, but it depends
  on a reference evaluation and should be measured against strong and weak
  engines.
- **Sample coverage.** A 50/1000 sample rate is a policy choice, not a security
  proof. The cost of missing bad proofs should be modelled.
- **Operator trust.** Until panel votes are on chain, challenge adjudication has
  an explicit operator trust assumption.
- **Engine provenance.** Binary hashes help, but a repacked cheating engine and
  unpinned networks remain outside the current guarantee.

### 3.3 Economics

- **Reward variance.** Draws earn nothing and `k` wins are required; small
  miners may wait a long time between payouts. Pooling and smoothing are open.
- **Bond sizing.** Registration, witness, challenge and timeout bonds need a
  measured relationship to expected rewards and attack cost.
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
  operational gaps.
- **Multi-relay operation.** Proofs are portable, but running and switching
  relays is not yet a first-class workflow.


### 3.5 Developer experience and assurance

- **Specification drift.** `PROTOCOL.md`, the OpenAPI fixture and the code must
  change together; a doc/code consistency check would help.
- **Load and soak testing.** The default suite is fast and deterministic but
  does not measure throughput, latency or long-run memory use.
- **Fuzzing.** Expand fuzzing beyond PGNs to transactions, blocks, gossip
  envelopes and challenge payloads.
- **Release automation.** Version checks exist; reproducible builds, signed
  images and provenance attestations do not.
- **Documentation.** Keep operator guides current as the testnet moves from a
  reset-on-deploy demo to a persistent network.

---

## 4. Working agreements

Every change should still meet the following bar:

- Tests at the right layer; `make check` green.
- Wire-format or consensus changes update `docs/PROTOCOL.md` in the same change.
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

