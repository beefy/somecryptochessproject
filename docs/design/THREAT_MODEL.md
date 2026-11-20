# Threat model

This document states what the network protects, who attacks it, which code
implements each mitigation, what remains exposed, and what is deliberately out
of scope. It describes the code in this repository, not an aspiration: every
"implemented" claim names the module and the test that proves it. The forward
work that would reduce the residual risks is tracked in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

Scope: devnet and testnet as they exist today, with mainnet parameters used only
where the difference matters (real engines, seal difficulty). The DEX and bridge
rows cover the SCCP chain on every network plus the Ethereum leg the bridge
observes -- Anvil chain 31337 on devnet, Sepolia 11155111 on testnet -- which is
an external system with its own threat model.

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
5. **Wallet keys and miner identity.** Keys are the only authority to move funds
   or claim wins (`crypto/`, `wallet/`).
6. **Network liveness.** Blocks keep being produced while some peers are down,
   lying, or spamming.
7. **Honest miners' time.** Verification is expensive; an attacker must not be
   able to make honest nodes burn unbounded CPU on garbage.
8. **DEX integrity.** Reserves and LP shares are consensus state: no swap may pay
   out more than the curve allows, no deposit may mint shares out of proportion,
   and no transaction may redeem more than it owns (`chain/dex.py`).
9. **Bridge atomicity.** An ETH<->SCCP swap either settles both legs or refunds
   both, without any party holding the other's asset (`chain/tx.py` HTLC types,
   `contracts/EthHtlc.sol`).

## 2. Adversaries

| Adversary | Capability assumed | Primary goal |
| --- | --- | --- |
| **Lazy miner** | Signs two seats' certificates, fabricates the PGN | Free rewards without play |
| **Self-play miner** | Runs both seats, weakens the loser | Legal proofs that earn without competing honestly |
| **Sybil pair** | Many funded identities, one operator | Guaranteed wins against itself |
| **Colluding cartel** | Several strong miners sharing wins | Dominate issuance |
| **Matchmaker** | Operates the relay | Censor, reorder, lie about pairings |
| **Node** | Serves blocks and proofs | Feed invalid data, eclipse peers, withhold evidence |
| **Reorg attacker** | Mining power plus timing | Rewrite history, double-claim a game |
| **DoS attacker** | Network flood, oversized payloads | Stall blocks, exhaust node CPU |
| **Quantum attacker** | A cryptographically relevant quantum computer (Shor on Ed25519) | Forge a spend or a seat's signature |
| **Block-ordering trader** | Builds or buys a block position, watches the mempool | Profit from moving a pool price around another swap; the chain has no batch clearing |
| **Counterparty trader** | The other side of an HTLC swap | Leave a leg locked, withhold the ETH claim until the SCCP refund height, or refuse to reveal |

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
  signatures, registries, bond arithmetic, coinbase vesting. No engine runs inside
  validation, and no engine verdict is a consensus rule; how well the chess was
  played is not a fact about the record (`PROTOCOL.md` section 9.2).
- **DEX.** There is no admin key, no oracle and no privileged market maker: a pool
  is addressed by its asset pair, its fee is the network's `dex_fee_bps`, and only
  the integer functions in `chain/dex.py` decide what a swap, deposit or
  redemption pays. The assets it can hold are exactly the ones `/v1/params`
  defines, so "which token is this" never depends on a third party's list.
- **Bridge, SCCP leg.** HTLC records are consensus state; the node holds no
  Ethereum key, and a claim needs a hybrid signature from the beneficiary as well
  as the preimage.
- **Bridge, Ethereum leg.** `EthHtlc.sol` is the counterparty: its security
  assumptions are Ethereum's (an unaudited contract, secp256k1, keccak), and a
  swap is only as strong as that leg. The node reads it through operator-configured
  JSON-RPC (`sccp.bridge.config`), which is a trust boundary for *observing* a
  lock, not for moving funds -- the timelock and the preimage are what settle it.
- **Order book.** Off consensus and unsyndicated: the node's in-memory book is a
  convenience, any party can mirror it, and losing it costs a re-post. Orders carry
  no funds, so censoring one denies a trade, not an asset.

---

## 4. Attack → mitigation → residual risk

| Attack | Mitigation (code) | Residual risk |
| --- | --- | --- |
| Fabricated game without a certificate | `verify_win_proof` requires both seat signatures over the certificate, the beacon-selected opening, a legal PGN, and matching `game_hash`/`plies` (`protocol/verification.py`, `tests/unit/test_verification.py`) | A fabricated game that is *legal and co-signed* is out of scope (that is self-play, below) |
| Sybil pairing | Pairing prefers identity-diverse opponents; bond and funding heuristics live at the operator layer (`protocol/pairing.py`) | Distinct infrastructure and independent funding defeat the heuristic |
| Opponent shopping | Commit-reveal beacon selects opening and pairing; every node recomputes it (`protocol/beacon.py`, `protocol/pairing.py`) | Last-revealer bias when `drand_used = 0`; no slashing evidence for one bit |
| Matchmaker censorship | Any node recomputes matchups from the beacon; miners may submit proofs earned elsewhere | Liveness still needs *some* relay |
| Loser stalls to deny a win | Clocks auto-flag, receipts are signed, a bonded `timeout_claim` has a dispute window, and finalising charges the staller 10% of the miner bond split claimant/quorum/burn (`protocol/clocks.py`, `chain/timeout.py: apply_stall_slash`) | Needs available witnesses (the relay refuses to pair below quorum); see forged timeout |
| Forged timeout | Receipt signatures, monotonic plies, later-move dispute, claimant bond (`chain/timeout.py`) | A colluding witness quorum can still fabricate a flag; the bond plus the 10% stall slash make a false claim expensive but not impossible |
| Replayed game | `game_hash`/`match_id` registries, proof-id uniqueness, registries rebuilt on reorg (`chain/state.py`, `chain/tx.py`) | A game can be reused on a competing fork inside the finality window |
| Self-assigned or inflated Elo | Ratings are not consensus state: no block field, transaction or route accepts a rating, and the node replays settled proofs with Glicko-2 (`ratings.py`, `node/service.py: ratings`, `tests/unit/test_ratings.py`) | Two colluding seats can trade wins and inflate each other; a rating measures results, not intent, and is provisional until 10 games |
| Invalid block | Full validation on receipt and on replay, headers-first sync (`chain/block.py`, `chain/chain.py`, `node/service.py: sync_from`, `node/p2p.py: plan_sync`) | A bug in validation code; tested, not proven absent |
| Quantum forgery of a spend | Every address is a version-1 program over the full 32-byte digest, every transaction signature must have both Ed25519 and ML-DSA-65 halves verify over the same scheme-tagged txid, and there is no classical path to fall back to (`crypto/pq.py`, `crypto/address.py`, `chain/tx.py`, `tests/unit/test_pq.py`) | Breaking ML-DSA-65 (or its implementation) is the only way in; the curve is young and its implementation surface is larger than Ed25519's |
| Quantum forgery of a game result or clock | Certificates and results are hybrid-signed, and the result payload carries `move_commitment`; refused-checkmate and flag evidence carry a witness-quorum hybrid checkpoint over the folded stream (`protocol/commitments.py`, `protocol/clocks.py`, `chain/timeout.py`, `tests/unit/test_commitments.py`, `tests/unit/test_clocks.py`) | A colluding witness quorum can still fabricate a flag; per-ply signatures stay Ed25519 and are only as good as the checkpoint that anchors them |
| Cheap proofless fork (heartbeat abuse) | A block earns the `k * WIN_UNIT` chess term only when it carries its `k` proofs, so a heartbeat earns seal work alone; a proofless block must carry exactly one transaction (`chain/difficulty.py: earned_block_work`, `chain/block.py: _check_proof_count`) | Seal work is the only barrier: the required transaction can be one the miner pays to itself, so it adds no real cost (16 bits per block on mainnet, 0 on devnet/testnet, where history is valueless). There is no finality gadget and `reorg_depth` is reserved and unenforced (whitepaper 16.5) |
| Long-range reorg | Coinbase maturity today; bonded finality and checkpoints are planned | Not solved in v1 |
| Weak or deliberately losing engine | The subsidy is paid only to the winner of a valid proof, so a bad engine simply loses and earns nothing; no rule inspects move quality (`chain/block.py`, `chain/difficulty.py`) | A colluding pair can still trade legal wins; that is self-play, priced by the bond and by pairing diversity, not by a quality verdict |
| Self-play collusion | Both seats need a bond and a funded entry; pairing prefers identity-diverse opponents (`protocol/pairing.py`) | Per bond the pair earns what a median honest miner earns, with no variance; v1 has no mechanism that separates "lost on purpose" from "lost badly" (PROTOCOL.md 9.2) |
| Swap sandwich inside one block | `min_amount_out` is enforced against the reserves the swap actually hits, so a forced push below the floor fails the transaction (`chain/dex.py`, `chain/tx.py: _validate_swap`) | The chain applies transactions in block order, so an earlier swap in the same block moves the price a later one pays: ordering is worth something to whoever builds the block, and there is no batch clearing. A user who sets a loose floor can still be filled at a worse price than they quoted (BIG_PICTURE 6.2) |
| Stale swap executed after a mempool delay | Only the output floor binds; a swap carries no `deadline_height` | **Residual, unmitigated.** A transaction that sat for a long time executes at whatever price its block produces, bounded by the floor alone (BIG_PICTURE 6.1) |
| Pool rounding or an empty first deposit | `swap_output`, `added_shares` and `removed_amounts` round in the pool's favour, a zero-share deposit and a zero-output swap are rejected, and each operation recomputes reserves so `k` cannot decrease (`chain/dex.py`, `tests/unit/test_dex.py`) | No per-block `k` invariant is re-checked, so a future edit to those four functions is not caught by a sweep as well as by the unit tests |
| LP position theft or a fake LP asset | Shares live in `Pool.shares` against `total_shares`; only the holder can burn them, they are not transferable, and no transaction type moves them (`chain/dex.py`, `chain/state.py`) | None identified: an LP position cannot leave the pool, so it also cannot be used as collateral |
| HTLC preimage theft | On SCCP a claim needs the beneficiary's own hybrid signature plus the exact preimage, so a leaked secret cannot redirect funds; on Ethereum `claim` is permissionless but always pays the recorded beneficiary (`chain/tx.py`, `contracts/EthHtlc.sol`) | The secret is public once either leg is claimed, which is the point: the counterparty uses the same preimage on the other chain. The race is over *timing*, not ownership |
| HTLC lock griefing or a stuck leg | `htlc_refund` is signed by the payer and valid only at or after `timelock_height`; a claim after that height fails (`chain/tx.py`) | A payer who never refunds strands their own funds only; the counterparty's leg has its own timelock and its own refund path |
| Forged or replayed bridge order | Orders and takes are hybrid-signed and verified like a transfer, carry `nonce` and `expiry`, bind both addresses, and one order accepts exactly one take (`bridge/orders.py`, `tests/unit/test_bridge_orders.py`) | The book is off consensus and in memory: a restart drops it and a node operator can censor an order by not relaying it |
| Malicious Ethereum contract call | `EthHtlc.sol` has no owner, no proxy and no upgrade path; `claim` checks the preimage, `refund` checks the depositor and the timelock, and the lock rejects stray ether (`contracts/EthHtlc.sol`, `contracts/test/EthHtlc.t.sol`) | The contract is unaudited; the audit and a static-analysis run are open gates for mainnet |

The attacks above are exercised by:

| Attack | Test |
| --- | --- |
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
| Swap below the floor, empty deposit, over-redemption | `tests/unit/test_dex.py::test_swap_moves_both_reserves_and_honours_the_minimum`, `::test_remove_liquidity_rejects_more_than_the_sender_owns`, `::test_pool_rejects_an_unordered_or_unbalanced_book`, `::test_the_pure_dex_helpers_cover_zero_and_invalid_inputs` |
| Pool reserve/share tampering in state | `tests/unit/test_dex.py::test_pools_are_committed_by_state_root_and_snapshots`, `::test_duplicate_pool_and_unknown_asset_are_refused` |
| HTLC claim after the timelock, refund too early, wrong preimage | `tests/unit/test_bridge.py::test_a_wrong_preimage_is_rejected`, `::test_claim_is_impossible_once_the_timelock_is_reached`, `::test_refund_returns_the_escrow_only_after_the_timelock`, `contracts/test/EthHtlc.t.sol` |
| Forged, expired or replayed bridge order; a second take | `tests/unit/test_bridge_orders.py` |
| A wallet signing the DEX and HTLC payloads the chain expects | `tests/unit/test_wallet_dex.py`, `tests/unit/test_wallet_htlc.py` |

`make adversarial` runs `pytest -m adversarial` over the remaining adversarial
cases. None of them concerns move quality: that layer was removed, and
`PROTOCOL.md` section 9.2 records why.

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
└── Publish a weak-but-legal proof
    ├── It pays only if it wins → the winner is paid and the loser is not, so a
    │   bad engine earns nothing and a strong one is rewarded
    └── Two seats colluding → self-play, 5.2
```

Mitigation shape: legality is cheap and decidable, so it is checked inline.
Move quality is neither cheap nor decidable from the record — a deliberately
lost game and a badly played one are the same bytes — so v1 does not pretend to
arbitrate it (`PROTOCOL.md` section 9.2).

### 5.2 Farm issuance with a self-play pair

```
Goal: collect the subsidy without competing against strangers
├── Bond both seats
│   └── both must be registered, funded and seeded; the cost is two bonds
├── Engineer a decisive game
│   └── legal: a game where one seat loses is a valid proof either way
├── Beat the pairing diversity heuristic
│   ├── Pairing prefers distinct funder/subnet → a split operator costs more
│   └── residual: nothing on chain verifies the claim, and the roster's
│       funder/subnet fields are not populated yet
└── Try to have the win invalidated by quality
    └── no such mechanism: a proof is valid if it replays (PROTOCOL.md 9.2)
```

Residual: per bond the pair earns the median honest return, with zero variance.
v1 accepts that and does not pay anyone to adjudicate intent.

### 5.3 Buy a fork with heartbeat blocks

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

### 5.4 Inflate a rating

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
| The reference profile is deterministic across honest nodes | Not a consensus rule any more; the layer was removed. An off-chain indexer still has to pin depth, options and the binary's SHA-256, and NNUE nets are not pinned | PROTOCOL.md 9.2 |
| A proof is valid independently of move quality | Implemented: verification replays the game and checks signatures, the opening, commitments and clocks; it never compares a move to an engine line | `protocol/verification.py` |
| A weak engine earns nothing | Implemented: only a winner's proof is paid, and a win against a weak opponent is still a valid proof; the pool's weak miner demonstrates the loss column | `chain/block.py`, `deploy/synthetic_miner_creation.py` |
| Witnesses for timeouts are available and honest enough | Mainnet requires a 3-witness quorum (`PROTOCOL.md` section 10.4) | `chain/timeout.py` |
| A proofless block cannot claim chess work | Implemented: `earned_block_work` credits the `k * WIN_UNIT` term only when the block carries its `k` proofs, and a proofless block must carry exactly one transaction | `chain/difficulty.py`, `chain/block.py` |
| A miner cannot set its own displayed rating | Implemented: the rating is a pure function of settled proofs, with no wire field and no submit route; the node recomputes it from the adopted chain | `ratings.build_ledger`, `node/service.py: ratings` |
| ML-DSA-65 is available wherever a signature is verified | Backend-gated by `cryptography`: verification returns `False` and key generation raises `PQUnavailableError` when the OpenSSL build has no ML-DSA provider, and `node run`, `miner run`, the relay and `devnet up` call `require_pq()` and refuse to start | `crypto/pq.py`, `errors.PQUnavailableError` |
| The browser's ML-DSA-65 agrees with the reference implementation | Pinned by vectors: the public key derived from the master seed is byte-identical to Python's, vitest verifies Python-produced hybrid signatures, and the node accepts browser-signed transactions. The residual risk is supply chain, not cryptography: the bundle trusts `@noble/post-quantum` | `web/src/lib/keys.ts`, `keys.test.ts`, `tx.test.ts` |
| A DEX swap cannot pay out more than the curve allows | Implemented: reserves are consensus state under the state root, every operation recomputes them with integer maths that rounds in the pool's favour, and a swap fails validation when `min_amount_out` is not met | `chain/dex.py`, `tests/unit/test_dex.py` |
| A swap price is what the user asked for | **Not implemented.** Only `min_amount_out` binds; a swap has no `deadline_height`, and there is no batch clearing, so price is block order plus the floor | `chain/tx.py: _validate_swap`, BIG_PICTURE 6.1-6.2 |
| An HTLC cannot be claimed by the wrong party or after the timelock | Implemented: claim requires the beneficiary's hybrid signature plus the exact preimage and is void at `timelock_height`; refund requires the payer's signature and the height | `chain/tx.py: _validate_htlc_claim`, `::_validate_htlc_refund`, `tests/unit/test_bridge.py` |
| The Ethereum HTLC contract holds only what it escrows | The contract has no owner, no upgrade path and no way to accept stray ether (`receive()` reverts with `NoEther`), so `lock` is the only inflow and `claim`/`refund` the only outflows. It is **unaudited**, and neither a formal model nor a static-analysis run has been published | `contracts/EthHtlc.sol`, `contracts/test/EthHtlc.t.sol` |

Open items that affect this model:

1. A `drand` BLS verifier, so `drand_used = 1` is verifiable offline.
2. Bonded finality, so a reorg cannot un-slash a miner.
3. Benchmarks for games/minute with mock engines (see the implementation plan);
   published figures are still estimates, not measurements.
4. Populated `funder`/`subnet` roster fields, so the pairing-diversity heuristic
   has real data instead of the empty strings it tolerates today.
5. An off-chain quality indexer, if anyone wants a published signal about how
   strong a miner's chess is. It must stay out of consensus (`PROTOCOL.md` 9.2).
6. A swap `deadline_height` and, if ordering turns out to be worth enough to
   matter, batch clearing. Both are protocol changes, so they cost a
   `sccp/v2`-style bump and a golden-vector refresh (`BIG_PICTURE.md` 6.2,
   `IMPLEMENTATION_PLAN.md`).
7. An external audit of `contracts/EthHtlc.sol` plus a published static-analysis
   run (Slither or equivalent). This gates the bridge's mainnet use.

## 7. Explicit non-goals

- **Privacy.** Addresses, PGNs, timestamps, pool reserves, HTLC preimages once
  claimed and open bridge orders are public. A DEX trade is visible before it
  settles, and a symmetric HTLC reveals the secret to both chains.
- **Order-flow protection.** No private mempool and no batch clearing: block
  ordering moves pool prices (BIG_PICTURE 6.2). Only the output floor protects a
  trader, so a user who quotes a loose floor has quoted a loose price.
- **Resistance to a well-funded attacker playing strong chess.** The goal is that
  cheating costs at least as much as competing; identical strong engines are
  indistinguishable from honest ones.
- **Sybil resistance without stake.** Identity is economic.
- **Human tournament rules.** v1 is fixed 10+0, no increments, no negotiation.
- **Sub-second trustless clocks.** Consensus verifies witness-signed receipts.
- **Proprietary or modified engines.** Engine builds are identified by SHA-256 but
  not enforced by consensus. A weak or padded engine is not a fault the chain can
  see: it simply loses and earns nothing.
- **Guaranteed liveness under total relay failure.** Proofs are portable, but
  games need some relay to happen.
- **A mainnet launch.** Mainnet parameters, the genesis hash and the premine exist
  and are pinned in `config.py`, but nothing has been deployed on mainnet. The
  go/no-go list (testnet soak, bridge audit, published benchmarks) lives in
  `IMPLEMENTATION_PLAN.md`.
- **A trustless Ethereum light client.** The bridge observes the Ethereum leg
  through an operator-configured JSON-RPC endpoint and relies on the HTLC
  timelock and preimage rather than on verifying Ethereum headers.


