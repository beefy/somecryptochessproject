# Threat model

This document states what the network protects, who attacks it, which code
implements each mitigation, what remains exposed, and what is deliberately out
of scope. It describes the code in this repository, not an aspiration: every
"implemented" claim names the module and the test that proves it. The forward
work that would reduce the residual risks is tracked in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

Scope: devnet and testnet as they exist today, with mainnet parameters used only
where the difference matters (quality sampling on, real engines pinned).

---

## 1. Assets

1. **Block rewards and fees.** Subsidy and fees are the only issuance; nobody may
   mint outside the coinbase and the mempool.
2. **Ledger integrity.** No inflation, no double spend, no unsigned state change.
   Every transaction is signature-checked against the sender's registered key
   (`chain/tx.py`), and node state is rebuilt from validated blocks and
   snapshots (`chain/chain.py`, `chain/storage.py`).
3. **Uniqueness of games.** A `game_hash` or `match_id` may be credited once
   (`chain/state.py` registries), so a win cannot be replayed or reused across
   forks inside the finality window.
4. **Fairness of matchmaking.** No opponent shopping, no self-pairing by choice,
   no matchmaker discretion over who plays whom (`protocol/pairing.py`).
5. **Wallet keys and miner identity.** Keys are the only authority to move funds,
   claim wins, or bond a challenge (`crypto/`, `wallet/`).
6. **Network liveness.** Blocks keep being produced while some peers are down,
   lying, or spamming.
7. **Honest miners' time.** Verification is expensive; an attacker must not be
   able to make honest nodes burn unbounded CPU on garbage.

## 2. Adversaries

| Adversary | Capability assumed | Primary goal |
| --- | --- | --- |
| **Lazy miner** | Signs two seats' certificates, fabricates the PGN | Free rewards without play |
| **Self-play miner** | Runs both seats, weakens the loser | Legal but low-quality proofs |
| **Sybil pair** | Many funded identities, one operator | Guaranteed wins against itself |
| **Colluding cartel** | Several strong miners sharing wins | Dominate issuance |
| **Matchmaker** | Operates the relay | Censor, reorder, lie about pairings |
| **Node** | Serves blocks and proofs | Feed invalid data, eclipse peers, withhold evidence |
| **Reorg attacker** | Mining power plus timing | Rewrite history, double-claim a game |
| **DoS attacker** | Network flood, oversized payloads | Stall blocks, exhaust analysis capacity |
| **Engine poisoner** | Ships a modified engine binary | Fake evals that pass the quality policy |
| **Griefer challenger** | Posts bonded challenges | Burn honest miners' time and bonds |
| **Quantum attacker** | A cryptographically relevant quantum computer (Shor on Ed25519) | Forge a spend or a seat's signature |

The design goal is **cost parity**: cheating should cost at least as much as
competing honestly. It is not "cheating is impossible".

## 3. Trust boundaries

- **Keys.** One scheme: hybrid Ed25519 + ML-DSA-65 per account
  (`crypto/pq.py`). A bech32 address is a version-1 program over the full 32-byte
  digest of the 1985-byte key blob (`crypto/address.py`), and a transaction needs
  a signature whose Ed25519 and ML-DSA-65 halves both verify. Match
  certificates, results and stream checkpoints are hybrid-signed; moves, clock
  receipts and gossip use the Ed25519 half and are anchored by a hybrid-signed
  commitment (`PROTOCOL.md` sections 8.5 and 10.5), so a quantum attacker must
  break ML-DSA-65 to forge anything that moves value or reputation.
- **Relay.** The relay schedules games and signs clock receipts, but it cannot
  move funds or mint proofs. A proof is valid without the relay being online.
- **Chain.** Consensus-critical validation is deterministic and engine-free:
  signatures, registries, bond arithmetic, coinbase vesting. Nothing that needs
  an engine is block-validity critical; quality is sampled and challengeable.
- **Operators.** On testnet, panel adjudication may be triggered by an operator
  key. That is an explicit trust assumption until panel execution is scheduled by
  consensus; it can burn a challenger's bond, so it is documented per deployment.

---

## 4. Attack → mitigation → residual risk

| Attack | Mitigation (code) | Residual risk |
| --- | --- | --- |
| Fabricated game without a certificate | `verify_win_proof` requires both seat signatures over the certificate, the beacon-selected opening, a legal PGN, and matching `game_hash`/`plies` (`protocol/verification.py`, `tests/unit/test_verification.py`) | A fabricated game that is *legal and co-signed* is out of scope (that is self-play, below) |
| Fabricated quality summary | Sampled re-run under the pinned profile; a disagreement becomes a bonded challenge (`node/quality_sampler.py`, `protocol/challenges.py`) | Unsampled proofs rely on challengers; 5% default sampling is policy, not proof |
| Weak-loser self-play | Both colours are counted against the reference profile, so blunders on either side fail the proof; a challenge slashes the miner (`chess/quality.py`) | Two strong engines pass — economics, not protocol, is the defense |
| Sybil pairing | Pairing prefers identity-diverse opponents; bond and funding heuristics live at the operator layer (`protocol/pairing.py`) | Distinct infrastructure and independent funding defeat the heuristic |
| Opponent shopping | Commit-reveal beacon selects opening and pairing; every node recomputes it (`protocol/beacon.py`, `protocol/pairing.py`) | Last-revealer bias when `drand_used = 0`; no slashing evidence for one bit |
| Matchmaker censorship | Any node recomputes matchups from the beacon; miners may submit proofs earned elsewhere | Liveness still needs *some* relay |
| Loser stalls to deny a win | Clocks auto-flag, receipts are signed, a bonded `timeout_claim` has a dispute window (`protocol/clocks.py`, `chain/timeout.py`) | Needs available witnesses; see forged timeout |
| Forged timeout | Receipt signatures, monotonic plies, later-move dispute, claimant bond (`chain/timeout.py`) | A colluding witness quorum can still fabricate a flag |
| Replayed game | `game_hash`/`match_id` registries, proof-id uniqueness, registries rebuilt on reorg (`chain/state.py`, `chain/tx.py`) | A game can be reused on a competing fork inside the finality window |
| Self-assigned or inflated Elo | Ratings are not consensus state: no block field, transaction or route accepts a rating, and the node replays settled proofs with Glicko-2 (`ratings.py`, `node/service.py: ratings`, `tests/unit/test_ratings.py`) | Two colluding seats can trade wins and inflate each other; a rating measures results, not intent, and is provisional until 10 games |
| Invalid block | Full validation on receipt and on replay, headers-first sync (`chain/block.py`, `chain/chain.py`, `node/service.py: sync_from`, `node/p2p.py: plan_sync`) | A bug in validation code; tested, not proven absent |
| Quantum forgery of a spend | Every address is a version-1 program over the full 32-byte digest, every transaction signature must have both Ed25519 and ML-DSA-65 halves verify over the same scheme-tagged txid, and there is no classical path to fall back to (`crypto/pq.py`, `crypto/address.py`, `chain/tx.py`, `tests/unit/test_pq.py`) | Breaking ML-DSA-65 (or its implementation) is the only way in; the curve is young and its implementation surface is larger than Ed25519's |
| Quantum forgery of a game result or clock | Certificates and results are hybrid-signed, and the result payload carries `move_commitment`; refused-checkmate and flag evidence carry a witness-quorum hybrid checkpoint over the folded stream (`protocol/commitments.py`, `protocol/clocks.py`, `chain/timeout.py`, `tests/unit/test_commitments.py`, `tests/unit/test_clocks.py`) | A colluding witness quorum can still fabricate a flag; per-ply signatures stay Ed25519 and are only as good as the checkpoint that anchors them |
| Cheap proofless fork (heartbeat abuse) | A block earns the `k * WIN_UNIT` chess term only when it carries its `k` proofs, so a heartbeat earns seal work alone; a proofless block must carry exactly one transaction (`chain/difficulty.py: earned_block_work`, `chain/block.py: _check_proof_count`) | Seal work is the only barrier: the required transaction can be one the miner pays to itself, so it adds no real cost (16 bits per block on mainnet, 0 on devnet/testnet, where history is valueless). There is no finality gadget and `reorg_depth` is reserved and unenforced (whitepaper 16.5) |
| Long-range reorg | Coinbase maturity today; bonded finality and checkpoints are planned | Not solved in v1 |
| DoS via analysis | Deterministic sampling with a per-call limit, dedup by proof id, bounded retries, bounded queue (`node/quality_sampler.py`) | Operators must size the queue and engine pool |
| Engine poisoning | The engine fingerprint is the binary's SHA-256 and is part of every certificate, summary and challenge evidence; operators can therefore blacklist a build | Nets (NNUE) are not pinned by hash yet, and a repacked cheating binary is out of scope |
| Panel packing / self-jury | Parties are excluded; the panel is ranked by a beacon-committed hash; odd size so majorities cannot tie (`protocol/challenges.py: select_panel`) | A cartel holding enough bonds can still supply the whole panel |
| Ballot stuffing / silence | Every panel member votes exactly once with a boolean; missing or extra votes are errors and change nothing (`panel_verdict`) | Votes arrive off-chain today; the transport is trusted until a commit transaction exists |
| Griefing challenges | The bond is locked on open; a rejected challenge burns it; one open challenge per challenger (`challenges.open_challenge`) | Challenging is cheap relative to a miner's analysis time if the bond is small |

The attacks above are exercised by:

| Attack | Test |
| --- | --- |
| Fabricated summary | `tests/adversarial/test_quality_attacks.py::test_a_fabricated_summary_becomes_a_bonded_challenge` |
| Weak-loser self-play | `tests/adversarial/test_quality_attacks.py::test_a_weak_loser_game_fails_quality_and_the_challenge_holds`, `tests/unit/test_quality.py` |
| Poisoned engine output | `tests/adversarial/test_quality_attacks.py::test_a_poisoned_engine_cannot_grief_an_honest_miner` |
| Panel packing / self-jury | `tests/adversarial/test_quality_attacks.py::test_a_challenger_cannot_pack_its_own_panel`, `::test_a_colluding_pair_cannot_be_its_own_jury` |
| Panel divergence, stuffed or silent votes | `tests/adversarial/test_quality_attacks.py::test_independent_nodes_derive_the_same_panel`, `::test_stuffed_or_silent_ballots_cannot_resolve` |
| Fabricated short mate / no certificate | `tests/unit/test_verification.py::test_missing_opponent_signature_fails` |
| Duplicate game or replayed match | `tests/unit/test_verification.py::test_seen_hashes_are_rejected`, `::test_replayed_match_id_fails` |
| Transcript tampering | `tests/unit/test_verification.py::test_tampered_pgn_fails`, `tests/unit/test_transcript.py::test_certificate_signatures_verify_and_reject_tampering` |
| Engine version mismatch | `tests/unit/test_transcript.py::test_certificate_signatures_verify_and_reject_tampering` |
| Withheld reveal | `tests/unit/test_beacon.py::test_epoch_reveals_detects_withholding_and_orphans` |
| Stalling loser, forged receipt, late dispute | `tests/unit/test_clocks.py`, `tests/unit/test_chain_timeout.py` |
| Refused checkmate | `tests/unit/test_clocks.py::test_refused_checkmate_needs_a_hybrid_checkpoint`, `tests/unit/test_commitments.py` |
| Witness equivocation | `tests/unit/test_chain_timeout.py::test_an_equivocating_witness_loses_its_bond` |
| Quantum forgery of a spend | `tests/unit/test_pq.py::test_a_transaction_signature_is_always_hybrid`, `::test_signing_refuses_a_classical_keypair`, `::test_a_classical_signature_is_a_schema_error`, `::test_a_classical_key_cannot_be_encoded` |
| Quantum forgery of a result or clock | `tests/unit/test_pq.py::test_the_dual_scheme_plumbing_is_gone`, `tests/unit/test_clocks.py::test_move_evidence_validates_order_authorship_and_commitment`, `tests/unit/test_chain_timeout.py` |
| Cheap proofless fork | `tests/unit/test_chain_chain.py::test_a_proofless_branch_cannot_outrun_a_proof_branch`, `tests/unit/test_chain_block.py::test_a_proofless_block_earns_only_its_seal_work`, `::test_a_proofless_block_carries_exactly_one_transaction` |

The first five rows are the adversarial suite: `make adversarial` runs
`pytest -m adversarial`, which the default suite deselects.

---

## 5. Attack trees

### 5.1 Free reward without play

```
Goal: a coinbase for a game nobody played
├── Forge a proof
│   ├── No certificate → rejected: verify_win_proof needs both seat signatures
│   ├── Certificate from real seats, PGN from elsewhere
│   │   ├── Opening is not the beacon's pick → rejected
│   │   ├── PGN does not replay / wrong game_hash / wrong ply count → rejected
│   │   └── Two seats co-sign a lie → self-play, not fabrication
│   └── Reuse a previous game → rejected: game_hash/match_id registries
└── Publish a bad-but-legal proof that no one checks
    ├── Validator sample misses it → economic residual risk (5% default)
    ├── Validator finds it → SamplerResult.needs_challenge → bonded challenge
    └── Nobody challenges before challenge_window closes → accepted
```

Mitigation shape: legality is cheap, so it is checked inline; quality is
expensive, so it is sampled and bonded. `protocol/challenges.py` is where a
sample turns into money moving.

### 5.2 Capture the adjudication panel

```
Goal: a challenge resolved the way I want regardless of the truth
├── Sit on my own panel
│   ├── As the challenger → excluded by challenge_panel(record.challenger)
│   ├── As the miner → excluded by challenge_panel(record.miner)
│   └── As their sybil → possible, but ranked and capped by size
├── Stuff the ballot
│   ├── Extra voter → panel_verdict raises, challenge stays open
│   ├── Non-boolean vote → panel_verdict raises
│   └── Missing voter → panel_verdict raises; a minority can never decide
├── Predict a different panel from other nodes
│   └── Rank = H("sccp/v1/challenge", {beacon, proof_id, member}) over the
│       bonded set, so a node with the same state derives the same panel
└── Resolve twice with different verdicts
    └── resolve_challenge is idempotent for the same verdict and refuses a
        different one once resolved
```

### 5.3 Grief an honest miner

```
Goal: make honest miners waste CPU or lose bonds
├── Spam expensive analysis
│   ├── Sampling is deterministic per (block_hash, proof_id) and deduped by
│   │   proof id, so re-gossiping a proof does not re-queue it
│   └── run(limit=N) bounds work per call
├── Poison my own engine and "disagree" with everyone
│   └── An engine failure is SamplerResult.error, which is never a challenge
├── Challenge everything
│   └── If the challenge is rejected, the challenger's bond is burned
└── Open many challenges from one account
    └── One open challenge per challenger; bonds lock on open
```

### 5.4 Buy a fork with heartbeat blocks

```
Goal: replace history without playing chess
├── Mine a proofless block at every height
│   ├── Skip the k proofs → allowed only with exactly one transaction
│   ├── Claim k * WIN_UNIT of chess work anyway
│   │   └── rejected: work is credited by proofs carried, so the block earns
│   │       its seal work alone (difficulty.earned_block_work)
│   └── Fold the pool into one proofless block to re-freeze the chain
│       └── rejected: a proofless block carries exactly one transaction
├── Let the hash tie-break favour the proofless branch
│   └── no longer applies: at equal height a branch carrying proofs is heavier
└── Grind a long cheap branch anyway
    └── residual: it still costs seal work per block (16 bits on mainnet);
        there is no finality gadget and reorg_depth is not enforced (16.5)
```

### 5.5 Inflate a rating

```
Goal: appear stronger than the games support
├── Write a rating into a transaction or block
│   └── rejected: there is no such field; a rating is not consensus state
├── Submit a rating to the node
│   └── rejected: there is no POST route; /v1/ratings is derived on demand
├── Edit the explorer's copy
│   └── no effect: the page renders the node's replay of the chain, and any
│       other node replays the same proofs to the same numbers
├── Replay an old winning game → rejected: game_hash/match_id registries
└── Collude with a second seat and trade wins
    └── residual: the games are legal, so they count; pairing's identity
        heuristics raise the cost, and the rating is marked provisional
        until 10 settled games
```

---

## 6. Assumptions and verification status

| Assumption | Status | Where |
| --- | --- | --- |
| The reference profile is deterministic across honest nodes | Implemented for depth, engine options and (when set) the binary's SHA-256; NNUE nets are not pinned yet; thresholds are deliberately loose | `chess/quality.py`, `QUALITY_V1` |
| Every node sees the same bonded set and beacon when it derives a panel | True for nodes at the same height; a reorg can change a not-yet-resolved panel | `challenges.challenge_panel` |
| Sample rate 50/1000 catches enough bad proofs | Policy choice, tunable per network; not a security proof | `config.py: quality_sample_rate` |
| Bonds exceed the expected value of one bad proof | Parameter choice; mainnet sizing is an open question in the implementation plan | `config.py: challenge_window`, `bond` fields |
| Panel votes are delivered honestly | Not yet enforced on chain: there is no `panel_vote` transaction, so the node/operator transport is trusted | open item |
| Witnesses for timeouts are available and honest enough | Mainnet requires a 3-witness quorum (`PROTOCOL.md` section 10.4) | `chain/timeout.py` |
| A proofless block cannot claim chess work | Implemented: `earned_block_work` credits the `k * WIN_UNIT` term only when the block carries its `k` proofs, and a proofless block must carry exactly one transaction | `chain/difficulty.py`, `chain/block.py` |
| A miner cannot set its own displayed rating | Implemented: the rating is a pure function of settled proofs, with no wire field and no submit route; the node recomputes it from the adopted chain | `ratings.build_ledger`, `node/service.py: ratings` |
| ML-DSA-65 is available wherever a signature is verified | Backend-gated by `cryptography`: verification returns `False` and key generation raises `PQUnavailableError` when the OpenSSL build has no ML-DSA provider, and `node run`, `miner run`, the relay and `devnet up` call `require_pq()` and refuse to start | `crypto/pq.py`, `errors.PQUnavailableError` |

Open items that affect this model:

1. A consensus-scheduled `panel_vote`/`adjudicate` transaction so votes do not
   depend on operator honesty.
2. Bond sizing that makes grieving an honest miner unprofitable relative to the
   CPU it burns.
3. A `drand` BLS verifier, so `drand_used = 1` is verifiable offline.
4. Bonded finality, so a reorg cannot un-slash a miner.
5. Benchmarks for games/minute with mock engines and analysis seconds/ply at
   depth 14 (see the implementation plan); published figures are still estimates, not
   measurements, and the sampler's disagreement rate across CPUs is unmeasured.
6. Browser ledger signing: the web library can verify everything and sign the
   move stream, but producing a hybrid signature needs an ML-DSA implementation.
   It fails closed (`signTransaction` throws) and the hand-play tab hands the
   result payload to `sccp wallet sign-result`; adding `@noble/post-quantum`
   closes this (`PROTOCOL.md` section 10.5).

## 7. Explicit non-goals

- **Privacy.** Addresses, PGNs and timestamps are public.
- **Resistance to a well-funded attacker playing strong chess.** The goal is that
  cheating costs at least as much as competing; identical strong engines are
  indistinguishable from honest ones.
- **Sybil resistance without stake.** Identity is economic.
- **Human tournament rules.** v1 is fixed 30+0, no increments, no negotiation.
- **Sub-second trustless clocks.** Consensus verifies witness-signed receipts.
- **Proprietary or modified engines.** Engine builds are identified by SHA-256 but
  not enforced by consensus; a cheating engine that repacks itself is out of
  scope.
- **Guaranteed liveness under total relay failure.** Proofs are portable, but
  games need some relay to happen.
- **Mainnet today.** Mainnet requires the panel-vote transaction and a 30-day
  testnet run; until then mainnet parameters stay disabled.


