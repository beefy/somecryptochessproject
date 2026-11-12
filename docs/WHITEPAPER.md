# somecryptochessproject

## Chess as proof of work

**Whitepaper for protocol v1 · reference implementation 0.9.3 · November 2026**

> **Scope.** This document explains how the current reference implementation
> works end to end: identity, chess proofs, matchmaking, the ledger, consensus,
> networking, storage and operation. It is descriptive, not normative. The
> wire formats and consensus rules are specified in
> [`PROTOCOL.md`](PROTOCOL.md); next steps and improvement areas are in
> [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). If this whitepaper and
> `PROTOCOL.md` disagree, `PROTOCOL.md` wins.
>
> **Status.** Protocol v1, the timed relay, quality sampling, bonded challenges
> and the two-miner testnet are implemented. Mainnet parameters exist, but no
> mainnet has been launched or audited. Testnet and devnet coins have no value.

---

**Contents**

1. [Abstract](#1-abstract)
2. [The problem and the thesis](#2-the-problem-and-the-thesis)
3. [Design goals and non-goals](#3-design-goals-and-non-goals)
4. [System model](#4-system-model)
5. [The life of a block](#5-the-life-of-a-block)
6. [Identity, addresses and wallets](#6-identity-addresses-and-wallets)
7. [Canonical encoding and hashing](#7-canonical-encoding-and-hashing)
8. [The chess proof layer](#8-the-chess-proof-layer)
9. [Epochs, availability and the beacon](#9-epochs-availability-and-the-beacon)
10. [Pairing and the opening](#10-pairing-and-the-opening)
11. [The match protocol and clocks](#11-the-match-protocol-and-clocks)
12. [Win proofs and verification](#12-win-proofs-and-verification)
13. [The ledger: accounts, transactions and bonds](#13-the-ledger-accounts-transactions-and-bonds)
14. [Blocks, difficulty and fork choice](#14-blocks-difficulty-and-fork-choice)
15. [Node, gossip, sync and storage](#15-node-gossip-sync-and-storage)
16. [Economics, security and residual risk](#16-economics-security-and-residual-risk)
17. [Current status and roadmap](#17-current-status-and-roadmap)
18. [Operating the reference implementation](#18-operating-the-reference-implementation)
19. [Glossary](#19-glossary)
20. [References](#20-references)

---

## 1. Abstract

`somecryptochessproject` is a cryptocurrency prototype in which the scarce
resource behind block production is **winning chess games against another
miner**, not evaluating SHA-256 repeatedly. Miners run ordinary UCI chess
engines. A public commit/reveal beacon fixes the epoch's roster and determines
who plays whom, with which colour, from which opening. The two seats sign a
match certificate, play a real clocked game, and co-sign the result. The winner
turns that transcript into a `WinProof`. A block is valid only when its miner
presents the required number of valid, unique, non-duplicated wins from earlier
epochs (zero during the bootstrap window), together with a normal signed
transaction body and a correct state commitment.

The design separates three kinds of work:

- **Chess work** is the expensive, adversarial part. It produces a winner and a
  replayable PGN.
- **Ledger work** is cheap and deterministic: signatures, nonce ordering,
  balance arithmetic, Merkle roots and hash targets.
- **Quality work** is deliberately *not* consensus-critical in v1. Reference-
  engine analysis is expected to be sampled and challengeable because engine
  versions and hardware differ; forcing every node to reproduce every centipawn
  judgement would risk consensus forks. The reference implementation therefore
  samples games deterministically and resolves disputes through bonded
  challenges; quality is evidence, not a block-validity rule.

The result is a complete, testable prototype of a different proof-of-work
economy, not a claim that chess is a verifiable delay function or that the
system is mainnet-ready. Chess strength, opening choice and hardware make the
cost model economic rather than cryptographic. Sybil resistance comes from
registration bonds, pairing constraints, uniqueness rules and the cost of
running two strong engines, not from a mathematical proof that two identities
are independent.

---

## 2. The problem and the thesis

### 2.1 The waste in hash-based proof of work

Hash-based proof of work is a lottery. Miners buy hardware, consume electricity
and race to find a nonce whose hash falls below a target. The winning hash has
no use outside securing the ledger. The security budget is real, but the
computation is otherwise discarded.

The thesis of this project is that the lottery ticket can be a **game of
skill**. If a block requires verified wins against independent opponents, then
the work spent producing a block is chess search: a computation that is
adversarial, reproducible as a record, and interesting in its own right.

### 2.2 What makes the substitution hard

A legal PGN is nearly free to fabricate. Anyone can write down a four-move
checkmate or copy a famous game. Legality alone is therefore worthless as work.
The protocol must also establish:

1. **Who played.** The two seats are registered identities with Ed25519 keys.
2. **That the game was assigned.** The pairing, colours and opening must be the
   deterministic output of public epoch data, not a private arrangement.
3. **That both sides participated.** A certificate is signed by both seats, and
   a result normally carries both result signatures.
4. **That the game is new.** The canonical game hash must not already be on the
   chain, and the proof id must be unique.
5. **That the winner is entitled to claim.** The proof must name the block
   miner as winner, come from an ended epoch's roster, respect the claim window
   and opponent cooldown, and carry the right evidence for its termination.
6. **That the game was not trivial.** A minimum ply count and, eventually,
   quality analysis raise the cost of a deliberately thrown game.

The first five are implemented as consensus rules. The sixth is economic rather
than consensus-critical: the minimum ply rule is enforced, and reference-engine
sampling plus bonded challenges raise the cost of a deliberately weak game
without making a node's engine judgement fork the chain.

### 2.3 The honest security statement

A valid proof says:

> A registered keypair, paired by public randomness with another registered
> keypair, signed a certificate for a legal game, played the moves in the PGN,
> and the recorded winner can be reproduced from those moves and signatures.

It does **not**, by itself, prove that the two keypairs are controlled by
different people, that both engines played honestly, or that the moves were
strong. Self-play remains the central economic attack. The project raises its
cost with bonds, pairing diversity rules, uniqueness, co-signatures and the
requirement to run two strong engines; it does not claim to eliminate it.

---


## 3. Design goals and non-goals

### 3.1 Goals

- **Use real chess.** Board state, move generation, legality, SAN, PGN and UCI
  control come from `python-chess`; the project writes no chess rules.
- **Make verification deterministic.** Serialization, hashing, addresses,
  beacon derivation, pairing, opening selection, transaction application and
  block validation are exact functions of public data.
- **Check cheap things first.** Signatures, hashes, replay and uniqueness are
  consensus checks. Expensive engine analysis is not.
- **Do not trust the relay.** The relay is a courier and clock observer. Any
  node can recompute the pairing from the beacon and roster, and every signed
  object is verified against registered keys.
- **Settle stalls without the loser's cooperation.** A 30+0 clock, witness-
  signed move receipts and a bonded timeout claim let a winner prove a flag.
- **Keep the ledger conventional.** An account model, Ed25519 signatures,
  integer amounts, nonce ordering, Merkle commitments, a mempool, SQLite
  persistence and cumulative-work fork choice.
- **Ship testnet and devnet first.** Network ids and address prefixes isolate
  chains, and devnet is fully deterministic for tests.

### 3.2 Non-goals for v1

- A GUI, chess engine or analysis product.
- Anonymity, privacy or confidential transactions.
- Smart contracts, NFTs or a general virtual machine.
- Light clients and SPV proofs (header/state roots are reserved for later).
- Mining pools, delegated mining or reward sharing.
- A production P2P stack with NAT traversal; v1 uses HTTP gossip and pull-sync.
- A cryptographic Sybil-resistance proof.
- Audited security or a launched mainnet.

### 3.3 Design principles

1. **Do not reinvent chess.**
2. **Deterministic where it matters.**
3. **Cheap checks before expensive checks.**
4. **No trusted matchmaker.**
5. **Testnet first, protocol changes are versioned.**

---

## 4. System model

### 4.1 Components

```text
                         signed certificate / moves
  +-------------+  HTTP   +-------------+  HTTP   +-------------+
  | engine      | <-----> | matchmaker  | <-----> | other miner |
  | miner       |         | / relay     |         | engine      |
  +------+------+         +------+------+         +-------------+
         |                       |
         | signed tx / proof     | signed transcript, receipts
         v                       v
  +-------------+   gossip   +-------------+   pull-sync   +-------------+
  | full node   | <--------> | peer node   | <-----------> | peer node   |
  | chain+pool  |            | chain+pool  |               | chain+pool  |
  +------+------+            +-------------+               +-------------+
         |
         | SQLite
         v
  +-------------+
  | chain store |
  +-------------+
```

| Component | Responsibility | Trust boundary |
| --- | --- | --- |
| Wallet | Holds an encrypted Ed25519 key, derives an address, builds and signs transactions | The user trusts their own keyfile and passphrase |
| Miner | Registers, commits availability, plays engine moves, co-signs results, builds proofs and blocks | Other nodes verify every claim; an invalid miner only wastes its own work |
| Matchmaker / relay | Derives matchup sessions, relays signed moves, runs the clock, stores transcripts and witness receipts | Not trusted for pairings, results or time; its output is recomputable or signed |
| Clock witness | Signs move and flag receipts with a bonded key | Consensus checks a quorum and slashes equivocation; testnet uses one witness |
| Full node | Validates and stores blocks, maintains mempool and peer state, serves the API | Peers revalidate everything; a bad node cannot make an invalid block valid |
| Peer | Gossips transactions, proofs and blocks; serves blocks for pull-sync | Content-addressed ids and full validation; failures affect only peer score |
| Faucet | Sends testnet/devnet coins from a published deterministic key | Test networks only; mainnet has no faucet and no premine |

### 4.2 Trust assumptions

- **Keys are private.** A stolen wallet key is a stolen identity. There is no
  key recovery beyond the mnemonic backup.
- **Genesis is pinned.** Network parameters contain a genesis hash. A node
  refuses a genesis document that does not hash to it.
- **Chess rules are the library's.** Protocol v1 pins the `python-chess` 1.11
  behaviour line. Changing that library's PGN semantics is a consensus change.
- **Clocks are observed, not derived.** The chain cannot see wall-clock time at
  sub-second resolution. It relies on bonded witnesses for flags and verifies
  their arithmetic and signatures offline.
- **Quality is not consensus-enforced.** A structural proof is not a quality
  proof; sampling and bonded challenges are probabilistic economic controls.
  See [section 16](#16-economics-security-and-residual-risk).

### 4.3 Network isolation

There are three parameter sets:

| Network | Purpose | Address prefix | Genesis |
| --- | --- | --- | --- |
| `testnet` | Public, valueless integration network | `sccpt1...` | Pinned testnet genesis with a faucet allocation |
| `mainnet` | Reserved production parameters | `sccp1...` | Pinned mainnet genesis, no premine |
| `devnet` | Deterministic in-process tests and demos | `sccpt1...` | Pinned deterministic genesis with a faucet allocation |

The network id is inside transaction hashes, game hashes, certificates,
gossip signatures and block headers. A testnet object cannot be replayed on
mainnet merely by changing a URL.

---


## 5. The life of a block

### 5.1 Heights and epochs

Heights are zero-based and every block is in exactly one epoch:

```text
epoch(e)        = floor(height / epoch_length)
epoch_start(e)  = e * epoch_length
epoch_end(e)    = (e + 1) * epoch_length - 1
```

Genesis is height 0. Testnet uses five-block epochs, mainnet uses twenty and
devnet uses one. Epoch arithmetic is pure integer arithmetic; no block times
enter it.

### 5.2 The bootstrap window

A fresh chain cannot immediately contain a win proof. Availability for a match
must be committed `availability_lead` epochs before the match, a reveal must
follow, the epoch must end, and only then can a proof be mined. Both testnet and
mainnet use `availability_lead = 2`. Epoch 2 is the first *nominal* matchable
epoch, but the bootstrap rule reserves one more epoch before demanding proofs:
the first realizable roster epoch is treated as epoch 3, and the first
proof-bearing block is in epoch 4.

The chain encodes that fact as:

```text
bootstrap_end_height = epoch_end(first_matchable_epoch + 1)
required_proofs(h)   = 0                      if h <= bootstrap_end_height
                       wins_required          otherwise
```

The boundary is a pure function of network parameters and height. It cannot be
extended by a miner, and every node computes the same value. During bootstrap,
miners do normal hash sealing and may include transactions, but no chess proof
is required. This lets the faucet, registrations, availability commits and
reveals enter the ledger before the first match can be claimed.

### 5.3 The epoch cycle

For a typical epoch `e`:

| Phase | When | What happens |
| --- | --- | --- |
| Availability commit | Epoch `e - 2` | A registered miner signs `available` for epoch `e` with a nonce commitment and game quota |
| Reveal | Epoch `e - 1` | The miner reveals the nonce from the commit; the pair is stored in state |
| Beacon | First block of epoch `e` | Header carries `beacon_hash`, derived from the parent hash and every valid commitment/reveal pair for `e` |
| Matchmaking | During epoch `e` | Any party deterministically pairs active roster members and derives colour and opening from the beacon |
| Play | During or after epoch `e` | Seats exchange signed certificate and moves; the relay records clock receipts |
| Proof eligibility | Epoch `e + 1` and later | A winner may put the game's proof in a block until the claim window closes |

Availability and reveal are separate transactions, not header fields. State
stores the commitment and quota per `(miner, epoch)`, the revealed nonce per
`(miner, epoch)`, and the engine fingerprint on the account. A commitment
without a reveal contributes nothing to the beacon and does not make the miner
matchable.

### 5.4 End-to-end timeline

```text
height 0            bootstrap: faucet, registrations, first commitments
epoch e-2           availability for e is posted on-chain
epoch e-1           reveal for e is posted on-chain
first block of e    beacon(e) = H(network, epoch, parent_hash, valid e reveals)
                    pairing = f(beacon, roster, match_id)
epoch e             certificate, 30+0 game, signed moves, result, receipts
epoch e+1           winner builds WinProof and includes it in a candidate block
                    nodes verify proof, transactions and state root
                    cumulative work selects the branch
```

The miner is not obliged to include a proof in the first eligible block. It can
hold a proof until the claim window closes, which allows batch mining, recovery
from an offline node, or waiting for a better chain tip.

### 5.5 One block, step by step

From the miner's point of view, producing a block at height `h` looks like
this:

1. **Read the parent state.** Confirm the network, height and parent hash, and
   select the difficulty schedule for `h`.
2. **Select proofs.** Gather locally held `WinProof`s that are still inside the
   claim window, unique, not already spent, and from a valid ended epoch.
3. **Select transactions.** Pull fee-paying transactions from the mempool in a
   deterministic order, including `coinbase` accounting and the miner's own
   operations.
4. **Apply.** Execute each transaction against a copy of the parent state,
   checking nonces, balances, bonds and type-specific rules. Add the coinbase
   reward and maturity.
5. **Commit.** Build the body Merkle root over the coinbase txid, transaction
   txids and proof ids, then compute the post-state account root.
6. **Seal.** Search a 64-bit `seal_nonce` such that the header hash has at least
   the scheduled number of leading zero bits. The seal is separate from the
   chess work; on devnet and testnet the initial seal difficulty is zero.
7. **Publish.** Wrap the block in a signed gossip envelope and announce it. The
   node may also receive it through `POST /v1/blocks`.

Every node that receives the block repeats steps 1, 3, 4 and 5 and checks step
6. No node trusts the miner's claimed state root or work.

---


## 6. Identity, addresses and wallets

### 6.1 One key, one identity

Every participant is an Ed25519 keypair. The public key is 32 bytes; private
operations use the 32-byte seed. There is no account registry keyed by email,
username or validator slot. An address is derived from the public key and a
network id:

```text
address_program = 0x00 || first20(H("sccp/v1/address", {
    "network": <network_id>,
    "public_key": <32-byte key as lowercase hex>
}))
address = bech32(<hrp>, address_program)
```

Version byte `0` means Ed25519. The hash covers the network, so the same key has
different addresses on mainnet and testnet. The human-readable prefixes are
`sccp1...` and `sccpt1...` respectively. Addresses are 21-byte bech32 programs,
not SegWit witness programs.

The first transaction from an address reveals its full public key. Subsequent
transactions may omit it; `resolve_public_key` accepts the key from state or
from the transaction, and `check_sender_key` makes sure both agree. This keeps
addresses short without losing self-authentication.

### 6.2 Keyfiles

`sccp wallet new` writes a JSON keyfile containing the network, address,
public key, KDF parameters, cipher parameters, nonce and ciphertext. The seed is
encrypted with AES-256-GCM under a key derived by scrypt (default `N = 2^15`,
`r = 8`, `p = 1`). The header is authenticated as associated data, so editing
the address, network or KDF fields breaks decryption. The file stores no
plaintext seed and the CLI never prints it.

A 24-word English BIP-39 mnemonic is a lossless backup of the 32-byte seed:

```text
sccp wallet export-mnemonic --key alice.key
sccp wallet import-mnemonic --out alice.key
```

The mnemonic is not an HD wallet. It restores exactly one keypair, not a tree of
derived accounts. Passphrases are read from an interactive prompt, from
`--passphrase-file` (mode 0600, first line used), or from
`SCCP_WALLET_PASSPHRASE`; they are not accepted as a plain command-line
argument.

### 6.3 Amounts and nonces

Amounts are integer base units, never floating point. One coin is
`100_000_000` units. A transaction has a strictly increasing `nonce` per sender.
The chain requires the exact next nonce. The mempool validates against a
projected state, so a transaction may depend on an earlier pending transaction
from the same sender, but two transactions cannot occupy the same
`(sender, nonce)`. Replays and double spends are rejected. A transfer specifies
`to`, `amount`, `fee`, and optionally the sender's public key. The fee is paid
to the miner through the coinbase transaction. There is no floating fee market:
the protocol enforces a minimum and miners choose what to include.

### 6.4 Transaction signing

The signed payload is not the JSON document as sent. It is:

```text
body = {version, network, type, sender, nonce, payload, fee}
digest = H("sccp/v1/tx", body)
signature = Ed25519.sign(seed, digest)
txid = digest
```

The `public_key` and `signature` fields are outside the signed body. A verifier
resolves the sender's key, rebuilds the body, checks the signature and computes
the txid. Canonical serialization is what makes this safe: object key order,
integer encoding and byte escapes are fixed by the serializer, not by the JSON
producer.

---

## 7. Canonical encoding and hashing

### 7.1 Why canonical bytes matter

Two honest nodes must never disagree because one emitted `{"a":1,"b":2}` and
the other `{"b":2,"a":1}`. Every hash in the system is therefore taken over a
canonical representation with sorted object keys, no insignificant whitespace
and exact integer encoding. Hashes and keys travel as lowercase hex.

The implementation uses Pydantic models at the edges and canonical JSON for
committed bytes. Domain-separated hashing prevents a signature for one kind of
object from being replayed as another:

```text
H(domain, payload) = SHA-256(domain_utf8 || 0x00 || canonical_json(payload))
```

| Domain | Committed object |
| --- | --- |
| `sccp/v1/address` | Network and public key |
| `sccp/v1/beacon` | Epoch beacon |
| `sccp/v1/beacon-commit` | Availability commitment |
| `sccp/v1/block` | Reserved block-document namespace |
| `sccp/v1/certificate` | Match certificate |
| `sccp/v1/challenge` | Challenge panel ranking and challenge id |
| `sccp/v1/clock` | Clock receipts and evidence bundle |
| `sccp/v1/game` | Canonical game record |
| `sccp/v1/gossip` | Gossip envelope |
| `sccp/v1/header` | Block header |
| `sccp/v1/match` | Match id |
| `sccp/v1/move` | Match move |
| `sccp/v1/opening` | Opening reference |
| `sccp/v1/pair` | Pairing score |
| `sccp/v1/proof` | Proof identity |
| `sccp/v1/result` | Match result |
| `sccp/v1/tx` | Transaction body and txid |

The serializer rejects an unregistered domain, so a typo cannot silently create
a parallel hash namespace.

### 7.2 Merkle commitments

The block body commits to one ordered list: the coinbase txid, then each
transaction txid, then each proof id. This single root is `merkle_root` in the
header. The state root is computed from account entries in address order. The
roster root commits to each matchable miner's public key and `max_games`. Merkle
leaves use tag byte `0x00` and position key, internal nodes use tag byte `0x01`,
and an empty tree has a fixed root.

The header does not commit to a full block size or to the PGN bytes themselves;
it commits to the body ids and the post-state root. The full proof, including its
PGN, is carried in the block and validated on receipt. PGN text is retained by
the relay and miner, and the chain's proof record commits to its game hash.

### 7.3 Determinism boundaries

Deterministic: address derivation, txid, proof id, game hash, beacon, pairing,
opening choice, state transition, Merkle roots, difficulty schedule and fork
choice.

Not deterministic across implementations: wall-clock measurements, engine
search results beyond the chosen move, relay scheduling, peer discovery order
and the order in which independent nodes learn about equal-work branches. The
protocol never makes consensus depend on those.

---


## 8. The chess proof layer

### 8.1 The board is the source of truth

All game logic delegates to `python-chess`. A game starts from the standard
initial position (protocol v1 rejects a custom `FEN`/`SetUp`). Moves are
replayed one at a time; every move must be legal on the live board, and SAN is
regenerated from that board rather than trusted from the submitted PGN. The
final board must agree with both the `Result` and `Termination` headers.

Accepted proof terminations are:

| Termination | Evidence expected |
| --- | --- |
| `checkmate` | Board is checkmate; result names the winner |
| `resignation` | Board has no terminal position; side to move resigned; result names the other side |
| `time_forfeit` | Clock evidence says the flagged seat reached zero and the claimant did not |
| `draw` | Valid for transcripts, but never for a `WinProof` |

`*`, `max-plies` and `unfinished` are not proof-grade. A clocked game that
reaches `max_plies` without a terminal result is a draw for the relay's
purposes and cannot be turned into a win proof.

### 8.2 Required PGN metadata

A proof-grade PGN must carry these tags:

```text
Event, Site, Date, Round, White, Black, Result, Termination,
MatchID, Epoch, RoundNumber, SeatWhite, SeatBlack,
EngineWhite, EngineBlack, OpeningID, OpeningHash,
TimeControl, Rules
```

`Rules` must be `chess`. `TimeControl` is `<initial>+<increment>` in seconds;
the v1 policy uses `30+0`. `MatchID`, epoch, round, seats, engines, opening id,
opening hash and time control are checked against the match certificate, so a
proof cannot silently describe a different game. Variations are rejected when a
certificate is supplied.

### 8.3 Canonical game identity

A game is identified by its moves and result, not by PGN formatting:

```text
game_hash = H("sccp/v1/game", {
    "network": <network_id>,
    "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "san_list": [...],
    "result": "1-0" | "0-1" | "1/2-1/2"
})
```

Headers, comments, clock annotations and move-number formatting are excluded.
Two PGNs with the same canonical moves and result on the same network produce
the same game hash. This is what the chain stores and what the uniqueness rule
uses.

### 8.4 Minimum length and time-forfeit nuance

A proof must have at least `min_plies` half-moves (20 on testnet, 30 on
mainnet). A game that ends earlier gets `low_ply = True`. `validate_pgn` with
`require_win=True` rejects a low-ply game unless its termination is
`time_forfeit`. The block layer only requires that `low_ply` be truthful, so a
short game cannot masquerade as a normal checkmate win. It is an explicit,
auditable exception for a genuine flag: a clock can run out before move 20.

Minimum length is not a quality measure. It merely prevents a one-move
"game" from being used as a proof. A pair of colluding engines can still play a
long, weak game, which is why sampling and bonded challenges exist.

### 8.5 Opening book

The shipped book lives in `data/openings.json` and is also packaged with the
wheel. Each line has a stable index, a name and a legal SAN prefix of at most
12 plies. The book is replayed and validated when loaded; an illegal or
malformed line is a startup error, not a runtime surprise.

Selection is a pure function:

```text
index = int.from_bytes(SHA256(beacon || "opening" || match_id)[:8], "big")
        mod number_of_lines
opening_hash = H("sccp/v1/opening", {book_version, line_id, moves})
```

Both seats must follow the chosen line exactly until it ends, then play freely.
The certificate binds `opening_id` and `opening_hash`, and PGN validation checks
the prefix. Because the beacon is public and the match id is unique, neither
seat can choose a favourable opening after seeing the pairing.

### 8.6 Engines and fingerprints

An engine is anything that implements the small UCI subset used by the miner:
`uci`, `isready`, `ucinewgame`, `position`, `go`, and `bestmove`, plus optional
`setoption`. The reference implementation provides:

- **MockEngine** — deterministic move selection for devnet and unit tests.
- **ScriptedEngine** — plays a supplied script, used to construct exact
  transcripts.
- **External UCI** — the `UciEngine` wrapper spawns a real UCI binary, configures
  options, parses `bestmove`, and terminates the process cleanly.

Registration can carry an engine fingerprint: the SHA-256 of the engine binary
for a real engine, or a hash of its configuration for mock and scripted engines.
The fingerprint is metadata for the quality layer and for operators; v1
consensus does not require a particular engine, and "pinned Stockfish" is a
target policy rather than a currently enforced rule. An engine that returns an
illegal move loses the game by protocol, so lying about strength does not help a
miner.

### 8.7 What a transcript contains

A complete match transcript contains:

- the signed `MatchCertificate` (network, match id, epoch, round, seats,
  colours, engine fingerprints, opening id/hash/moves, time control);
- the ordered signed moves, each with seat and ply;
- the signed result record (`GameResult`) with game hash, plies, termination and
  one or two signatures as appropriate;
- clock evidence: witness-signed move receipts plus a final flag receipt for a
  time forfeit;
- optionally the original PGN text with the required headers.

The PGN is convenient for humans and for `sccp verify pgn`; the proof record's
authority comes from the hashes and signatures, not from the text formatting.

---


## 9. Epochs, availability and the beacon

### 9.1 Why commit and reveal

If miners simply announced "I am available for epoch e", a miner could wait for
everyone else's announcements and then choose whether to play, or manipulate a
matchmaker. Commit/reveal removes that option. The miner first posts a hash of a
random nonce, then reveals the nonce. The pair is fixed before the beacon is
known, and anyone can recompute the beacon from on-chain data.

### 9.2 Transactions

```text
availability_payload = {
    "epoch": e,
    "max_games": maximum games this miner accepts,
    "commitment": 32-byte hash,
    "public_key": optional full key
}
reveal_payload = {
    "epoch": e,
    "nonce": 32-byte value
}
```

The commitment is domain-separated and bound to the miner address and epoch;
because the address already commits to the network, it cannot be replayed on
another network. The reveal must open it. A commitment without a reveal is
ignored for roster and beacon purposes. A miner may update its engine fingerprint
without changing identity.

### 9.3 Matchable roster

At the first block of epoch `e`, the parent state defines the roster. A miner is
matchable when it:

1. has an active registration through `e`;
2. holds at least the minimum registration bond;
3. posted a valid availability commitment for `e` within the lead window;
4. revealed the nonce during epoch `e - 1`;
5. is not banned at the epoch's opening height.

The roster is fixed when the epoch opens; quota exhaustion later in the epoch
affects pairing, not membership.

Each roster entry commits to the miner's public key and `max_games`. The roster
root is part of the header, although the header also carries the beacon, so a
light client could later reconstruct matchmaking from committed data.

### 9.4 Beacon derivation

For epoch `e`, the first block uses the parent block hash and the valid revealed
commitments for `e`:

```text
digest = SHA256(
    "sccp/v1/beacon" || 0x00 || network || 0x00 || u64(epoch)
    || parent_hash
    || concat(sorted(commitment || nonce for each valid reveal))
)
B_e = SHA256(digest || drand_signature)   if drand is used
B_e = digest                              otherwise
```

Entries are sorted as byte strings, so reveal order never matters. Every block
in epoch `e` must repeat the same `beacon_hash`; a different value is invalid.
Epochs before the first matchable epoch have no beacon at all and commit the
zero hash. A miner whose reveal is missing is withheld from the roster and
contributes nothing to `B_e`.

A miner cannot change a nonce after committing, but the last unrevealed identity
can see earlier reveals and the parent hash before deciding whether to reveal.
Withholding changes the set of entries that produce `B_e`, and an operator with
many identities has more selection power. This is bias by participation, not by
grinding an open nonce. The design reserves an optional drand mix-in to close
that gap. **The current implementation bundles no drand verifier and the chain's
`epoch_beacon` computes pure commit/reveal. Network parameters carry drand
fields for the mainnet policy, and the beacon module accepts an injected
verifier, but no production code supplies one.** Do not describe testnet beacons
as drand-attested.

### 9.5 Beacon properties and limits

- **Public:** every node can recompute it from stored transactions.
- **Epoch-bound:** the same inputs cannot produce a different beacon at another
  height.
- **Post-commitment:** miners choose availability before they see the result.
- **Not unbiasable:** a last revealer can bias the entry set by revealing or
  withholding; drand is the intended mitigation.
- **Not a VDF:** the beacon is not proof of elapsed time, and it does not make
  chess search sequential.

---

## 10. Pairing and the opening

### 10.1 Pairing is a deterministic function

There is no server that chooses opponents. Given the epoch's roster and beacon,
anyone derives the same matchups. For round `r`, each active miner gets an
ordering key:

```text
score(miner) = SHA256(
    "sccp/v1/pair" || network || beacon || u64(round) || address
)
```

Miners are sorted by `(score, address)`. The algorithm walks the sorted list and
pairs neighbours while quotas remain. Hard constraints are distinct addresses
and remaining game quota. Soft constraints are shared funder, shared subnet,
shared ASN and recent opponents inside `opponent_cooldown_epochs`; when a pair
violates one, the second seat is swapped with a later candidate that does not.
If no swap fixes it, the violation is accepted: liveness beats a diversity
nicety. An odd miner at the end sits the round out.

The soft constraints are only as good as the roster metadata supplied to the
relay. The on-chain roster commits to each miner's public key and `max_games`
only; consensus verification therefore cannot enforce funder, subnet, ASN or
recent-opponent diversity. Those checks are an operator/relay best effort, not a
consensus guarantee.

Match ids are:

```text
match_id = H("sccp/v1/match", {
    "network": network,
    "epoch": epoch,
    "round": round,
    "seats": [sorted_address_a, sorted_address_b]
})
```

The opening is deliberately absent, because the match id is an input to opening
selection. The certificate later binds the chosen opening id and hash.

### 10.2 Colour

For a fixed match:

```text
digest = SHA256(beacon || "color" || match_id)
white = seat_a if int.from_bytes(digest[:8], "big") & 1 == 0 else seat_b
```

Both seats derive the same answer. Neither can choose colour or rename a game
after the pairing is known.

### 10.3 Opening selection

The opening is selected from the certified book using the beacon and match id,
as described in [section 8.5](#85-opening-book). Both seats must play the same
prefix, and the certificate commits to the book version, line id, move list and
opening hash. Because the line is selected from public randomness, a player
cannot pick an opening after learning the opponent. The cost is that both
engines may be out of book quickly; that is intentional, because the game
should become real chess, not a memorised draw.

### 10.4 Roster liveness and the honest limit

Pairing needs at least two active miners with compatible availability. If only
one is available, no match can be formed and no proof can be produced. The
protocol stalls rather than lowering its requirements. That is a liveness
property, not a safety failure: an empty or tiny network simply stops producing
proof-bearing blocks until enough miners participate. Testnet and devnet avoid
this during development by running several local miners.

The pairing rules reduce, but do not prove, independence. Two identities can
still pair with each other if the beacon selects them. The bond, the cooldown
and the cost of operating two engines are economic friction, not cryptographic
separation.

---

## 11. The match protocol and clocks

### 11.1 Sessions

A matchmaker instance maintains a session per match. The session holds the
certificate, the board, the clock, ordered signed moves, witness receipts and
the result. The relay exposes endpoints to join, fetch the session, submit a
move, resign or agree a draw, fetch the transcript and fetch receipts. The
transport is HTTP for v1; messages are signed objects, not bearer-token
commands.

### 11.2 The certificate

A `MatchCertificate` binds:

- network;
- match id, epoch and round;
- white and black addresses;
- white and black engine fingerprints;
- opening id, opening hash and the exact book moves;
- time control;
- both seats' start signatures.

Both seats sign the canonical certificate payload. The relay refuses to start
until both signatures verify. The signatures are over the certificate, not over
a relay-generated session token, so the same certificate can be checked offline.
The fixed v1 control is 30 minutes per seat with no increment:

```text
TimeControl(initial_ms = 1_800_000, increment_ms = 0)
```

### 11.3 Starting the clock

The clock starts when the second valid start signature is accepted and the relay
has played the book line. The side to move on the resulting board is to move
(White normally, Black if the opening ends after an odd ply). Only that side
spends time. A move deducts

```text
max(0, elapsed_ms - lag_grace_ms)
```

from the mover, adds the increment (zero in v1) and switches turns. If the
deduction would leave zero or less, the move is rejected and the side to move
is flagged. `lag_grace_ms` defaults to 250 ms and is capped at 1,000 ms. It is a
relay policy that is recorded in network parameters; it is not part of the game
hash. A clock that is stopped after the game ends freezes its displayed values.

### 11.4 Moves

Each move is a signed object containing at least the match id, ply, seat and SAN
(or a canonical move representation). The relay checks:

1. the signature belongs to the seat whose turn it is;
2. the ply is exactly the next ply;
3. the SAN is legal on the relay's board;
4. the certificate and match id agree;
5. the clock has not already expired.

The relay then applies the clock and records the move. A move that arrives after
the flag is rejected; it cannot be used to rescue an expired clock. A move that
is legal but loses does not become invalid: chess legality and game outcome are
separate.

### 11.5 Move receipts and the clock witness

The relay is the live clock, but it is not trusted as the final authority. For
each accepted move it obtains a signed `MoveReceipt` from one or more witness
keys. A receipt commits to the match, ply, seat, receipt kind, remaining clocks
and receive time. Witness keys are registered on-chain with a bond.

A quorum of receipts is required for on-chain clock evidence. Mainnet reserves a
quorum of three; testnet uses one so that local operation is practical. The
witness bond is the collateral that can be slashed for equivocation. The relay
stores receipts in order and serves the bundle to either seat.

### 11.6 Ending a game

The result record contains the match id, network, ply count, result, game hash
and termination. The normal path is a co-signed result:

- both seats sign the same result record (`GameResult`);
- the relay checks that the board, game hash and result agree;
- the transcript is complete and either seat can build a proof if it won.

Special cases:

- **Checkmate without the loser's co-signature.** The winner's signed final
  move plus the certificate is sufficient to replay the mate. The loser cannot
  escape by refusing to sign a position that is already checkmate.
- **Resignation or agreed draw.** Both signatures are required. A unilateral
  "I resign" message is not a proof until the result protocol records it.
- **Time forfeit.** A co-signed result can still carry a `time_forfeit`
  termination with clock evidence. If the flagged seat refuses to sign,
  settlement uses a `FlagReceipt` and the timeout-claim path below.
- **Disconnect or refusal before a terminal position.** The game remains
  unfinished. It produces no win proof unless one side can prove a flag.

### 11.7 Timeout claims, disputes and slashing

The intent is that a winner never needs the loser's cooperation to settle a
flag. The pieces are:

1. **Flag evidence.** A witness-signed `FlagReceipt` says the flagged seat has
   zero milliseconds and the claimant has positive time, at the ply immediately
   after the claimant's last signed move.
2. **Bonded claim.** The claimant submits a `timeout_claim` with the
   certificate, the last move, the receipt bundle and a bond of at least
   `min_timeout_bond`. The claim opens a dispute window.
3. **Dispute.** The accused may submit `timeout_dispute` with a later valid
   signed move before the deadline. If it does, the claim is cancelled and the
   bond is returned.
4. **Finalisation.** If no valid dispute arrives, a proof with
   `time_forfeit` references the claim. The claim's bond is returned when the
   result is finalised.
5. **Slashing.** Two conflicting signed receipts from the same witness are
   `slash_evidence`. The witness bond is slashed and the witness is removed from
   the active set.

The ledger validators and appliers for claims, disputes and witness slashing are
implemented, and the engine miner builds and locally verifies a timeout claim.
The remaining integration gap is operational: the demo/CLI path does not yet
submit a claim transaction automatically. In the current reference flow, a
flagged game outside the tests yields a locally held claim rather than a block.
This is tracked as the on-chain claim integration in the implementation plan.

### 11.8 Why the clock is not a consensus timestamp

Block time is coarse and miner-controlled within limits. Chess clocks need
sub-second accounting. The protocol therefore treats clocks as *evidence*:
witnesses attest to what they observed, the claim binds the evidence to signed
moves, and the chain checks arithmetic and signatures. A malicious witness
quorum can lie about a flag; bonds and slashing make that costly, and a larger
quorum makes collusion harder. The chain does not pretend that a block timestamp
is a chess clock.

---


## 12. Win proofs and verification

### 12.1 The proof is self-contained

A `WinProof` carries the match id, epoch, round, winner, loser, winner seat,
result, termination, ply count, opening reference, full PGN, certificate, result
signatures and any evidence needed for the exceptional endings. It is designed
so an offline verifier with the network parameters, epoch beacon, roster and
public keys can check it without trusting the relay or the miner.

The proof id is:

```text
proof_id = H("sccp/v1/proof", {
    "game_hash": game_hash,
    "match_id": match_id,
    "winner": winner_address
})
```

The game hash is the canonical move/result commitment from section 8.3. Two
proofs for the same game necessarily collide; two proofs with the same id are
the same claim.

### 12.2 Verification order

The verifier is deliberately ordered from cheap and objective to expensive:

1. **Network.** Proof, certificate and beacon must name the verifier's network.
2. **Beacon.** Recompute `B_e` from reveals; check availability if supplied;
   check drand only if the beacon says drand was used.
3. **Certificate shape.** Match id, epoch, round, seats, colour draw and fixed
   time control must agree with each other.
4. **Opening.** The proof's opening must equal the certificate's, be internally
   valid, and — when a book is available — be the line the beacon selects.
5. **Roster and pairing.** Both seats must be in the epoch roster, and the
   matchup must be exactly the one the roster, quota and beacon produce.
6. **Signatures.** Both start signatures verify; result signatures are checked
   against the seats. One missing signature is allowed only for the two evidence
   paths: a timeout claim or a checkmate final move.
7. **PGN replay.** Parse exactly one game, enforce required tags, replay every
   move, check legality, opening prefix, result and termination, and apply the
   minimum-ply rule.
8. **Hashes.** Recompute the game hash, result, termination, plies, winner seat
   and `low_ply`; they must equal the proof's declared fields.
9. **Evidence.** For a time forfeit, verify clock evidence against the signed
   move stream and witness quorum. For checkmate, verify any move evidence and
   that the final mover is the winner.
10. **Proof id and uniqueness.** Recompute the id and reject an already-seen
    game hash or proof id. Chain-level code additionally checks the epoch ended,
    the claim window, the roster and the opponent cooldown.

A failure at any step rejects the proof as a whole; the block builder does not
partially repair it.

### 12.3 Result signatures and the two exceptions

The normal proof has both seats' result signatures. Two carefully bounded
exceptions prevent a loser from vetoing an already-decided game:

- **Checkmate:** the winner's signed final move plus a replayable PGN is enough
  when the missing signature is the loser's. The final move must be the
  winner's, and the board must actually be checkmate.
- **Time forfeit with a claim:** the winner's signature plus witness clock
  evidence and a timeout-claim id is enough when the missing signature is the
  flagged seat's. The claim must be open, undisputed and owned by the winner.

A resignation or draw has no such exception: if the opponent will not sign, the
game is not proof-grade. That is intentional. A unilateral claim that the
opponent resigned is not evidence.

### 12.4 Chain-level eligibility

Even a cryptographically valid proof is not automatically mineable. At block
height `h` inside epoch `E`, the block builder and validators require:

- `proof.epoch < E`; the referenced epoch has ended;
- the winner is the block miner;
- the winner was on that epoch's roster;
- the epoch has a matchable roster;
- `h` is after the epoch's end;
- `h <= epoch_end + claim_window`;
- the opponent has not already appeared in another proof in this block;
- the winner has not played that opponent within `opponent_cooldown` epochs;
- `low_ply` exactly matches `plies < min_plies`;
- the proof's game hash and proof id are not already on the chain;
- the block has exactly `wins_required` proofs (or zero during bootstrap).

The distinct-opponent rule and cooldown slow down a miner that tries to recycle
one cooperative opponent. They do not stop a miner from operating many keys;
that is the Sybil problem discussed in section 16.

### 12.5 Quality is evidence, not block validity

`WinProof` has an optional `quality_summary`, and the clocks/PGN modules expose a
resignation-policy hook and reference-engine analysis. Mainnet parameters
declare `quality_mode = sampled` and a non-zero sample rate. The structural
verifier deliberately ignores quality: a node does not reject a proof because a
local engine disagrees by a few centipawns, which would make heterogeneous
hardware a consensus risk.

Instead, a node deterministically samples proofs, records an analysis summary,
and turns a persistent disagreement into a bonded `challenge`. A
beacon-ranked panel re-runs the pinned quality profile and resolves the
challenge on chain. The challenge path is implemented; moving panel votes on
chain and calibrating bond sizes remain open work.

---


## 13. The ledger: accounts, transactions and bonds

### 13.1 Account state

State is an account map plus epoch, proof, match, timeout, witness and
registration indexes. An account holds spendable balance, immature coinbase
funds, locked bond, next nonce and registration metadata. Amounts are integers.
The state root commits to the account map in address order. Indexes are derived
from committed transactions and proofs, so they can be rebuilt by replay; the
consensus commitment is the account root plus the block chain.

### 13.2 Transaction types

| Type | Purpose |
| --- | --- |
| `transfer` | Move spendable balance |
| `register` | Register a miner and lock the registration bond |
| `register_witness` | Register a bonded clock witness |
| `update_registration` | Change metadata, extend registration, adjust bond |
| `unregister` | Begin unbonding |
| `withdraw_bond` | Withdraw a matured bond |
| `availability` | Commit to play a future epoch |
| `reveal` | Reveal the availability nonce |
| `timeout_claim` | Open a bonded claim that an opponent flagged |
| `timeout_dispute` | Answer a claim with a later signed move |
| `slash_evidence` | Prove witness equivocation |
| `challenge` | Bond a quality challenge against an accepted proof |

A transaction is rejected for the wrong network, unknown fields, wrong type,
bad signature, wrong nonce, insufficient balance, insufficient fee, or a
type-specific violation. The mempool additionally rejects a transaction that is
still valid on chain but already superseded in the pool. The chain validates
again at inclusion time; the pool is not a trusted pre-validation.

### 13.3 Registration and bonds

A miner registers with a bond of at least `min_bond`, an endpoint, an engine
string, and is valid for the network's registration period. The bond is locked,
not spent. Unregistering starts an unbonding period; the bond becomes
withdrawable only after it matures. This makes identity cheap enough to test but
expensive enough to mass-produce at mainnet parameters.

Clock witnesses register separately with `clock_witness_bond`. Their bond is
the collateral for receipt equivocation. Testnet sets it low; mainnet raises it.
Bonds are not a proof of personhood. They are a cost and a slashing target.

### 13.4 Coinbase and maturity

Every non-genesis block has exactly one coinbase transaction paying the block
miner. It has no signature (the block seal authorises it), no nonce, no fee, and
its sender and recipient are the miner. The subsidy is:

```text
subsidy(h) = params.subsidy >> (h // halving_interval)   if halving is set
             params.subsidy                              otherwise
subsidy(h) = min(subsidy(h), max(0, max_money - minted))
```

Coinbase outputs mature after `coinbase_maturity` blocks and cannot be spent
before then. Transaction fees in the block are added to the miner's reward by
the coinbase accounting. There is no separate fee transaction.

### 13.5 Deterministic application

Applying a block means applying its coinbase and transactions in order to a copy
of the parent state, then applying each proof's side effects (proof indexes,
match indexes, miner statistics and timeout finalisation). If any step raises,
the block is invalid and the original state is unchanged. The resulting state
root must equal the header's `state_root`.

The implementation uses copy-on-write state transitions internally; consensus
semantics are "all or nothing". A node never persists a partially applied block.

---


## 14. Blocks, difficulty and fork choice

### 14.1 Header

A block header contains, in canonical order:

```text
version, network, height, prev_hash,
merkle_root, state_root, roster_root,
timestamp, epoch, beacon_hash,
wins_required, seal_bits, seal_nonce, miner
```

`version` is 1. `network` is the network id. `height` is parent height plus one.
`prev_hash` is the parent block hash. `merkle_root` commits to the body (section
14.2). `state_root` is the post-state account root. `roster_root` is the epoch's
matchable roster commitment. `timestamp` is Unix seconds. `epoch` is
`height // epoch_length`. `beacon_hash` is the epoch beacon or the zero hash.
`wins_required` and `seal_bits` are the declared difficulty. `seal_nonce` is the
64-bit work counter. `miner` is the address that receives the coinbase.

The block hash is:

```text
block_hash = H("sccp/v1/header", header_payload)
```

The header does not contain a block reward field; the coinbase transaction
carries it and validation checks it against the schedule.

### 14.2 Body commitment

The body Merkle root is built over one ordered list:

```text
[coinbase_txid, transaction_txid_1, ..., proof_id_1, ...]
```

Leaves are `sha256(0x00 || decimal_position || 0x00 || canonical_json(item))`.
Internal nodes are `sha256(0x01 || left || right)`. An odd node is promoted
unchanged; an empty body has the fixed zero root. The root is `merkle_root` in
the header. There is no separate transaction root or proof root in the header;
the ordering and position keys make the list unambiguous.

Block size is the canonical JSON byte length of the whole block and must not
exceed `max_block_bytes`. Transactions and PGNs have their own caps. A block
that exceeds a cap is invalid even if every individual signature is correct.

### 14.3 Timestamps

A block timestamp must be:

- strictly greater than the median of the previous 11 block timestamps
  (`MTP_WINDOW = 11`), when the parent has history;
- no more than two hours ahead of the verifier's current time, when the
  verifier supplies one.

The median rule is the consensus constraint. The two-hour bound is a relay and
node sanity check; deterministic replays pass `now = None`. Timestamps are
seconds, not milliseconds, and they are not used for chess clocks.

### 14.4 The seal

The seal is a separate, cheap proof-of-work over the header hash. A header is
sealed when:

```text
int(block_hash, 16) < 2 ** (256 - seal_bits)
```

`seal_bits = 0` accepts every hash. Miners increment `seal_nonce` and rehash
until the condition holds. On mainnet the initial target is 16 bits; testnet and
devnet start at 0. The seal is not the security budget: it smooths block
arrival and prevents trivial header grinding. The expensive part is `k` verified
chess wins.

### 14.5 Difficulty schedule

Two values are scheduled together:

- **`wins_required` (`k`)** is the number of proofs a block must carry after
  bootstrap. It is the coarse lever and moves in integer steps.
- **`seal_bits`** is the hash target. It moves in single-bit steps and absorbs
  variance the chess results cannot.

The schedule is a pure function of the parent state, the parent's last `k`
change height and the last `retarget_interval` timestamps. On a retarget height
with enough samples:

```text
actual   = last_timestamp - first_timestamp
expected = target_block_seconds * (retarget_interval - 1)
```

- If `actual > 4/3 * expected`, reduce `seal_bits` by one and, if allowed,
  reduce `k` by one.
- If `actual < 3/4 * expected`, raise `seal_bits` by one and, if allowed, raise
  `k` by one.
- Otherwise carry the parent difficulty unchanged.

`k` may move only on an epoch boundary and not within one epoch of its previous
move. Bounds are `k_min..k_max` and `0..32` seal bits. Retargeting can be
disabled by parameters; devnet does so for determinism. Testnet begins at zero
seal bits and one required win; mainnet begins at 16 bits and three wins.

### 14.6 Fork choice

Each block contributes:

```text
block_work = (1 << seal_bits) + wins_required * 65536
```

A branch's work is the sum of its blocks. The node adopts the branch with the
greatest cumulative work. Ties are broken by the numerically lower block hash,
so two nodes that see the same two branches choose the same tip.

The formula is deliberately simple and documented as a v1 approximation. At
mainnet's initial 16-bit seal target, the seal contributes 65,536 units, the
same as one required win, so a block's work is `1 + k` such units. The formula
does not attempt to model actual engine strength, game length or wall-clock
cost. Fork choice is about which history has the most protocol-defined work,
not which miner is strongest.

### 14.7 Reorganisations

A reorg happens when a competing branch becomes heavier. The node:

1. validates every block on the competing branch from the fork point;
2. finds the highest common ancestor;
3. adopts the heavier branch's blocks above it;
4. returns orphaned transactions and proofs for rebroadcast;
5. persists the new adopted tip and invalidates cached tip-dependent data.

Blocks are immutable and content-addressed, so a reorg never rewrites a stored
block. It changes which branch the tip points at. A transaction or proof that
was only in an orphaned block becomes eligible again, unless it has since been
included in the new branch.

### 14.8 Genesis

Genesis is a special height-0 block with no coinbase and no proofs. Its header
has the genesis timestamp, zero parent, genesis state root and genesis roster
root. It is loaded from a pinned document; its hash must equal
`params.genesis_hash`. A network with no pinned hash still parses the document,
but production callers are expected to pin it.

The genesis state contains the initial allocations. Testnet and devnet include a
faucet allocation under a published deterministic key. Mainnet parameters
contain no premine and no faucet. Every later block must descend from the
genesis block; a document with a different genesis hash is a different chain.

---


## 15. Node, gossip, sync and storage

### 15.1 One node process

A node combines:

- a validated in-memory block tree with the adopted tip;
- the current state and its indexes;
- a mempool of signed, validated transactions and pending proofs;
- a transcript store for matches the node has seen;
- a peer table and a gossip deduplication cache;
- an HTTP API for wallets, miners, operators and peers;
- an optional SQLite database for durable blocks and snapshots.

The reference node is a single process. There is no separate consensus client,
execution client or database server. `sccp node run` starts the HTTP server and
optionally bootstraps from peer URLs.

### 15.2 HTTP API

The public v1 surface is small:

| Route | Purpose |
| --- | --- |
| `GET /v1/status` | Network, height, tip, sync and mempool summary |
| `GET /v1/params` | Effective network parameters |
| `GET /v1/blocks/{height-or-hash}` | A validated block |
| `POST /v1/blocks` | Submit a block |
| `GET /v1/accounts/{address}` | Balance, bond, nonce and registration |
| `GET /v1/epochs/{epoch}` | Roster, beacon, quotas and withheld miners |
| `GET /v1/epochs/{epoch}/beacon` | The epoch beacon document |
| `GET /v1/difficulty` | Current and next difficulty |
| `GET /v1/games/{game_hash}` | Indexed proof/game metadata |
| `GET /v1/matches/{match_id}` | Match record |
| `GET /v1/matches/{match_id}/transcript` | Stored certificate, result and PGN |
| `GET /v1/mempool` | Pool contents and fee summary |
| `GET /v1/transactions/{txid}` | Transaction status |
| `POST /v1/tx` | Submit a signed transaction |
| `POST /v1/proofs` | Submit a win proof |
| `GET /v1/challenges` | Open quality challenges, plus resolved ones on request |
| `POST /v1/gossip` | Receive a signed gossip envelope |
| `GET /v1/peers` | Known peers and scores |

The API has no authentication or TLS in v1. It is intended for localhost, a
private network or a reverse proxy. Exposing it directly to the internet gives
strangers the ability to submit objects and consume resources; every submitted
object is still validated, but availability is not protected.

### 15.3 Gossip envelopes

A gossip message is:

```json
{
  "network": "testnet",
  "kind": "transaction | proof | block | peer",
  "payload": { "...": "..." },
  "sender": "sccpt1...",
  "signature": "..."
}
```

The sender signs the canonical body in domain `sccp/v1/gossip`. The network id
and kind are inside the signed body, so an envelope cannot be replayed on
another network or relabelled. Objects are deduplicated by content id: txid,
proof id, block hash or peer URL. The envelope's sender identity is separate
from the embedded object's author; a relay may forward a block it did not mine.

When the sender's public key is known — supplied by the transport or found in
the ledger — the envelope signature is checked. An unknown sender may still
relay a block, proof or transaction because the embedded object is independently
validated, and transactions and proofs carry their own signatures. Unknown
senders only affect the abuse score.

### 15.4 Fanout and peer scoring

After accepting a new object, the node chooses up to four eligible peers at
random, excluding the sender. It returns the fanout plan to the caller, which
may relay the envelope. Duplicates are not re-applied; they are still counted
for deduplication and may be returned to the caller.

The peer table is bounded (64 by default). Each failure increments a counter;
three consecutive failures ban the peer for 300 seconds. A success clears the
counter and the ban. This is a coarse abuse score, not a reputation system. It
protects the node from repeatedly timing out on a dead URL; it does not prove
that a peer is honest.

Peer discovery is manual: an operator supplies a URL with `--peer`, or a peer
envelope advertises one. There is no DHT, no NAT traversal and no encrypted
transport in v1. A peer is just an HTTP base URL.

### 15.5 Pull sync

Sync is pull-based and height-oriented. The node asks a peer for
`GET /v1/status`, checks the network id, and requests the next range of blocks,
at most 512 per call by default. Each block is fetched by height, validated
through the same `connect_block` path as gossip, and adopted if it makes the
branch heavier. A malformed or out-of-order block aborts the sync; the peer gets
a failure. A peer that is down at startup is skipped rather than fatal.

Because blocks are validated, a malicious peer can waste bandwidth and time but
cannot make an invalid chain valid. It can withhold blocks, serve an old tip or
serve a valid but lighter branch; the node simply does not adopt it. A node that
cannot reach any peer keeps its current tip and continues serving.

### 15.6 Mempool

The mempool holds transactions and proofs that pass stateless validation and
the current-state checks the node can perform before inclusion. It evicts by
age and size, revalidates on inclusion, and prunes transactions invalidated by a
new block. A reorg returns orphaned transactions and proofs for possible
rebroadcast. The pool is a convenience, not consensus: two nodes may order or
retain different sets, and the chain only cares about what a block contains.

### 15.7 SQLite persistence

The store is one SQLite database with:

- `meta` — schema version, network id, tip hash, height;
- `blocks` — canonical block JSON keyed by hash, with height, parent and
  cumulative work, plus a `tip` flag;
- `snapshots` — periodic state snapshots keyed by height.

WAL mode allows reads during writes. The schema version is checked at startup;
an incompatible database fails loudly instead of being misread. A database is
bound to one network id and refuses to open under another.

A snapshot is a cache, never a trusted state. On restart the node rebuilds the
chain by replaying every stored block through validation, then loads the latest
snapshot and checks that its state root agrees with the replayed state at that
hash. A mismatch is a storage error, not a reason to trust the snapshot. Replay
is the authority.

The default snapshot interval is 100 blocks. `reorg_depth` exists in the
parameter set but is not currently enforced by the reference node: fork choice
follows any heavier branch, however deep. Treat the parameter as reserved for a
future operational policy, not as a guard that is active today.

### 15.8 What is not stored

The node stores block bodies, including proofs and their PGN strings, because
they are part of the block. It does not store a full chess engine, an analysis
database, an opening tree beyond the shipped book, or a historical price feed.
The relay stores live sessions and receipts; a node may store transcripts it
has seen, but consensus does not require every node to retain every transcript
forever. Archival policy is an operational choice in v1.

---


## 16. Economics, security and residual risk

### 16.1 Where the cost comes from

A block's cost has three parts:

1. **Chess work.** `k` games, each requiring an engine strong enough to win
   against another registered miner. This is the dominant cost and the only part
   that is adversarial in a useful way.
2. **Seal work.** `2^seal_bits` expected hashes per block. On mainnet the
   initial target is 16 bits, which is negligible compared with running two
   engines for a game; it exists to smooth arrival, not to secure the chain.
3. **Bond and opportunity cost.** Registration and witness bonds are locked
   capital. They make disposable identities expensive but do not make them
   impossible.

The reward is the block subsidy plus fees. On mainnet the subsidy starts at 50
coins and halves every 210,000 blocks, with a 21,000,000-coin cap. Testnet and
devnet rewards are valueless.

### 16.2 The central attack: self-play

The simplest attack is for one operator to run both seats. If its two engines
can produce a decisive game that passes every structural check, it earns a
proof without competing against an independent miner. The protocol does **not**
prevent this. It raises the cost and the chance of detection:

- both seats must be registered and bonded;
- availability and reveals are public and consume epochs;
- pairing is beacon-driven, so the operator cannot always choose its twin;
- distinct-opponent and cooldown rules prevent one pair from minting many
  proofs in one block;
- game hashes and proof ids are unique, so replaying a game is impossible;
- both seats must sign the certificate and, normally, the result;
- the winner must run an engine that can actually win the game.

None of these proves that the two seats are independently controlled. A
sufficiently rich operator can register many identities and accept the bond and
opportunity costs. The honest claim is that self-play is priced, not excluded.
Quality sampling and bonded challenges raise the price further by making
deliberately weak games challengeable, but they do not prove that two keys are
independently controlled.

### 16.3 Sybil resistance

Sybil resistance is economic, not cryptographic:

- **Bonds.** Each miner and witness locks collateral. More identities means
  more locked capital.
- **Registration duration.** Identities cannot be created and discarded
  instantly without cost.
- **Pairing diversity.** Shared funder, subnet and ASN are soft constraints in
  relay-configured rosters, so a cluster may be less likely to be paired
  internally when honest miners are available. The on-chain roster does not
  commit this metadata, so the rule is not enforced by consensus.
- **Opponent cooldown.** A winner cannot repeatedly claim against the same
  opponent within the cooldown window.
- **Unique games.** A game can only be claimed once, so a fixed script cannot
  be replayed across many blocks.

A cartel large enough to control most of the roster can still pair internally.
At that point it is not attacking a rule; it is the network. The design does not
claim otherwise.

### 16.4 Other attacks and mitigations

| Attack | Mitigation | Residual risk |
| --- | --- | --- |
| Fabricated PGN | Full replay, certificate tags, result/termination agreement, signatures | A legal but thrown game still passes |
| Replay a famous game | Game hash uniqueness; certificate binding to seats and epoch | None if hashes are checked |
| Claim someone else's win | Winner must equal block miner; seats and result signatures | None for a valid signature scheme |
| Forge availability | Commit/reveal with chain-stored commitment; reveal window | Last-revealer selection and withholding remain without drand |
| Relay lies about a flag | Witness-signed receipts, quorum, bonds, slashing | A corrupted witness quorum can lie |
| Miner withholds a proof | Claim window; proof remains valid until expiry | Lost opportunity, not a safety break |
| Long-range fork | Cumulative work; no finality gadget | A heavier history wins, even if old |
| Eclipse a node | Multiple peers, manual configuration | No DHT or automatic diversity in v1 |
| DoS the HTTP API | Validation before storage, size caps, peer bans | No auth, rate limiting or TLS |
| Stolen wallet key | Passphrase-encrypted keyfile | Key compromise is identity compromise |
| Weak engine / thrown game | Structural checks, deterministic sampling and bonded challenges | Sampling is probabilistic; panel votes still have an operator-trusted transport |

### 16.5 Finality

There is no finality gadget. A block is "confirmed" only in the economic sense
that a heavier competing branch is expensive to build. A deep reorg is possible
in principle if an attacker can produce more cumulative work. The `reorg_depth`
parameter is reserved and not currently enforced. Applications should wait for a
confirmation depth appropriate to the value at risk and should not treat a
testnet confirmation as final.

### 16.6 Privacy and anonymity

There is none. Addresses are public keys, transactions are public, registration
metadata may include an endpoint and engine string, and matches reveal which
miners played. A miner's address is linkable across epochs unless it rotates
keys, and rotating keys costs new bonds and registration. The protocol is not a
privacy system and makes no confidentiality claim.

### 16.7 The quality layer and its residual gaps

The quality layer is implemented as an economic control:

- deterministic sampling of games for reference-engine analysis;
- quality summaries carrying engine identity, depth and a trace hash;
- a bonded `challenge` transaction with a bounded reason and evidence payload;
- panel selection from the bonded set, a complete-ballot verdict and
  deterministic slashing of the proof miner or the challenger;
- a resignation policy that checks the final position before accepting a loss;
- an adversarial suite and [`THREAT_MODEL.md`](THREAT_MODEL.md).

What remains is hardening, not a missing subsystem:

- panel votes are delivered to the node/operator rather than committed by a
  `panel_vote` transaction;
- sample rate, bond size and panel size need calibration across hardware;
- the sampler should submit qualifying challenges automatically;
- NNUE networks are not pinned by hash;
- cross-CPU disagreement rates are not yet measured.

The protocol's security statement is therefore structural plus economic, not a
proof of strong chess. Do not describe sampling or challenges as "proof of
honest play".

---

## 17. Current status and roadmap

### 17.1 Implemented

| Area | Status |
| --- | --- |
| Canonical JSON, domain hashing, Merkle roots | Implemented |
| Ed25519 keys, bech32 addresses, encrypted keyfiles, mnemonic backup | Implemented |
| Account ledger, transfers, fees, coinbase, maturity, supply cap | Implemented |
| Transactions: register, update, unregister, withdraw, availability, reveal | Implemented |
| Genesis loading and pinned genesis hash | Implemented |
| Block validation, header seal, state root, body root | Implemented |
| Difficulty schedule and cumulative-work fork choice | Implemented |
| Commit/reveal beacon | Implemented |
| Deterministic pairing, colour draw, opening selection | Implemented |
| PGN validation, game hash, required tags, minimum plies | Implemented |
| Match relay: certificate, signed moves, clock, results, receipts | Implemented |
| Win proof construction and offline verification | Implemented |
| Timeout claim/dispute and witness slashing ledger rules | Implemented |
| Gossip envelopes, peer table, pull sync | Implemented |
| SQLite persistence with replayed, snapshot-checked state | Implemented |
| REST node API, proof settler and CLI | Implemented |
| Deterministic quality sampling, summaries and resignation policy | Implemented |
| Bonded challenges, panel selection, adjudication and slashing | Implemented |
| Wallet, devnet, multi-miner demo, unit/integration tests | Implemented |
| Public faucet, deterministic bootstrap and two-miner testnet stack | Implemented |
| GHCR image build and SSH testnet deploy workflow | Implemented |

### 17.2 Planned or incomplete

| Area | Status |
| --- | --- |
| Continuous public testnet and 72-hour soak | Not yet run |
| Metrics, alerting and block explorer | Not implemented |
| On-chain challenge-panel votes | Votes use an operator-trusted transport |
| Automatic timeout-claim submission in CLI/demo | Incomplete integration |
| drand quicknet verifier wiring | Hook exists; pinned verifier not bundled |
| Faucet limit persistence | Limits are in memory |
| Storage pruning and restore tooling | Not implemented |
| Light client / SPV proofs | Not implemented |
| P2P encryption, discovery, NAT traversal | Not implemented |
| Mainnet launch and audit | Not done |
| Finality gadget | Not planned for v1 |

### 17.3 Next work

The protocol build is complete; the next work is operational confidence and
mainnet readiness. In priority order:

1. Run a persistent public testnet and publish a 72-hour soak report.
2. Add metrics, alerting and a block explorer.
3. Automate timeout-claim submission and bundle a pinned drand verifier.
4. Commit challenge-panel votes through a signed transaction.
5. Persist faucet limits and harden storage, peer networking and miner/relay
   recovery.
6. Calibrate bonds and sampling, complete an external review and decide whether
   to launch mainnet.

The acceptance criteria and the longer list of improvement areas are in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). The distinction that matters
publicly is unchanged: a proof shows that a legal, assigned game happened and
was co-signed; sampling and bonds raise the cost of a weak game but do not prove
that both engines played honestly.

### 17.4 Readiness statement

The reference implementation is suitable for:

- local experiments and teaching;
- testnet/devnet integration;
- protocol research and adversarial testing;
- building wallets, explorers and tooling against a documented API.

It is not suitable for:

- storing real monetary value;
- claiming production-grade security;
- anonymous or private payments;
- high-throughput or low-latency applications;
- any use that assumes finality.

---


## 18. Operating the reference implementation

### 18.1 Requirements

- Python 3.11 or newer.
- `python-chess` on the pinned 1.11 behaviour line.
- An Ed25519 wallet keyfile for signing.
- A UCI engine binary for real play, or the built-in mock engine for devnet.
- SQLite is part of the Python standard library; no external database server.

Install from a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
sccp --version
```

The project pins `python-chess` deliberately. Upgrading it can change PGN
semantics and therefore consensus behaviour.

### 18.2 Create a wallet

```bash
sccp wallet new --network testnet --out alice.key
sccp wallet address --key alice.key
```

The command prompts for a passphrase. For automation, put the passphrase in a
mode-0600 file and pass `--passphrase-file` globally:

```bash
sccp --passphrase-file ./alice.pass wallet new --network testnet --out alice.key
```

Back up the key with the 24-word mnemonic:

```bash
sccp wallet export-mnemonic --key alice.key
```

Anyone with the mnemonic or the keyfile plus passphrase controls the identity.

### 18.3 Run a node

```bash
sccp --data-dir ./data node run --network testnet --port 8645
```

The node opens `./data/chain.sqlite`, replays the chain, serves the API and
accepts gossip. Add `--peer http://host:8645` to pull-sync at startup; repeat
`--peer` for several URLs. If a peer is down, the node warns and continues.

Inspect the same data directory with read-only commands:

```bash
sccp --data-dir ./data chain info --network testnet
sccp --data-dir ./data chain blocks --limit 5 --network testnet
sccp --data-dir ./data chain account sccpt1... --network testnet
sccp --data-dir ./data chain verify --network testnet
```

The node API has no authentication. Bind it to localhost or a trusted network,
or put an authenticating reverse proxy in front of it.

### 18.4 Use the faucet

Testnet and devnet only:

```bash
sccp --data-dir ./data faucet send --network testnet \
    --to sccpt1... --amount 1000
```

The faucet is a local command that submits a transfer from the published
deterministic genesis key. It is not a public service and it does not exist on
mainnet. Mainnet parameters have no faucet and no premine.

### 18.5 Register and commit availability

```bash
sccp --data-dir ./data miner register --network testnet \
    --key alice.key --bond 10 --endpoint tcp://alice.example:8646

sccp --data-dir ./data miner availability --network testnet \
    --key alice.key --max-games 3
```

The availability command prints the nonce secret. Keep it; the reveal must
present it in the next epoch:

```bash
sccp --data-dir ./data miner reveal --network testnet \
    --key alice.key --nonce-secret <hex-from-previous-step>
```

A missing reveal means the miner is not matchable and does not contribute to the
beacon. The registration bond must be funded from spendable balance.

### 18.6 Run the relay

```bash
sccp matchmaker run --config relay.json --store ./relay-state.json \
    --witness-key relay.key --round-seconds 900
```

`relay.json` supplies the network, beacon/epoch, roster, engine fingerprints and
match configuration. `--store` persists sessions and receipts across restarts.
`--witness-key` makes the relay sign clock receipts. `--round-seconds`
optionally opens the next round on an interval.

The relay is not a trusted matchmaker. It can refuse service, but it cannot
change a proof's pairing, colour, opening or result without producing something
that fails verification.

### 18.7 Run a miner

```bash
sccp miner run --network testnet \
    --key alice.key \
    --engine-path ./bin/stockfish-macos-universal \
    --relay http://127.0.0.1:8646 \
    --node http://127.0.0.1:8645 \
    --relay-witness-key <relay-witness-public-key-hex>
```

The miner polls the relay, joins assigned matches, plays its moves, co-signs
results, builds proofs and submits them to the node. `--iterations N` stops after
N polls, which is useful for tests. `--engine-profile mock` uses the
deterministic mock engine.

Two miners are the minimum for a match. Without enough available miners, the
network stalls; that is expected liveness behaviour, not a bug.

### 18.8 Offline verification

```bash
sccp verify pgn game.pgn --network testnet
sccp verify proof proof.json --context context.json --network testnet
```

The PGN command replays every move and prints the canonical game hash. The proof
command checks signatures, pairing, opening, PGN, hashes, evidence and
uniqueness against a context document that supplies the beacon, roster, public
keys and parameters. It needs no node.

### 18.9 Devnet

```bash
sccp devnet up --miners 4 --blocks 10 --engines mock
sccp --json devnet up --miners 2 --blocks 4
```

Devnet runs in-process with one-block epochs, zero bonds, zero seal difficulty
and mock engines. It is deterministic and designed for tests, demos and CI. It
does not prove that real engines or real timing work; use testnet or the timed
relay scripts for that.

### 18.10 A hand-played game

```bash
python scripts/play_miner.py
python scripts/play_miner.py --on-chain
python scripts/play_miner.py --on-chain --games 3
python scripts/play_miner.py --relay http://127.0.0.1:8646 --key alice.key
```

Solo mode builds a relay and a mock opponent in-process. It is an off-chain
protocol demo: real clocks, receipts and auto-flag, but no node, proof or block.
`--on-chain` is devnet-only; it resumes a chain, plays a co-signed game, turns
the transcript into a proof and mines it. A draw, a flag, a too-short game or a
result the engine will not co-sign produces no block.

### 18.11 Export games

With `sccp node run` serving the chain you played on:

```bash
python scripts/export_block_games.py --node http://127.0.0.1:8645 --out ./games
python scripts/export_block_games.py --block 5 --out ./games
```

The exporter writes PGN files and a `manifest.json`. Point it at the right data
directory and network; otherwise it may export a different chain's genesis
(block 0, zero games).

### 18.12 Docker testnet

```bash
docker compose up --build
```

The compose stack runs a node, a relay and three mock miners on testnet. It is a
demo and integration stack, not a production deployment: keys are generated for
the stack, the API is unauthenticated and the chain is local.

### 18.13 Tests

```bash
make test          # unit + property, mock engines only
make test-all      # adds the integration/e2e tests that exist
```

The default pytest selection excludes tests marked `engine`, `adversarial`,
`slow` and `network`. Those markers exist because real Stockfish, long-running
timing tests and socket tests are environment-dependent. Run them explicitly
when the environment supports them. `make test-all` does not automatically run
the engine-marked tests.

### 18.14 Operational checklist

- Pin the network id and genesis hash; never mix data directories.
- Back up keyfiles and mnemonics; a lost key cannot be recovered on-chain.
- Keep the node API on localhost or behind an authenticating proxy.
- Monitor height, peer count, mempool size and relay sessions.
- Treat a relay as replaceable: run more than one, and verify its pairings.
- Keep enough peers to avoid eclipse; v1 has no automatic discovery.
- Do not treat testnet coins or confirmations as valuable or final.
- Do not describe quality sampling as consensus enforcement; it is probabilistic
  evidence backed by bonds and challenges.

---


## 19. Glossary

| Term | Meaning |
| --- | --- |
| **Availability** | A bonded commitment to be matchable in a future epoch |
| **Beacon** | Public epoch randomness derived from reveals (and optionally drand) |
| **Block** | Header plus coinbase, transactions and win proofs |
| **Bond** | Locked collateral for a miner or witness identity |
| **Bootstrap** | The initial heights where no proofs are required |
| **Certificate** | Both-seat signed record of a match's fixed parameters |
| **Claim window** | Number of blocks after an epoch in which its wins can be proven |
| **Coinbase** | The unsigned transaction paying the block reward and fees |
| **Cumulative work** | Sum of `(1 << seal_bits) + k * 65536` over a branch |
| **Epoch** | Fixed number of heights used for availability, beacons and pairing |
| **Game hash** | Hash of network, start position, SAN moves and result |
| **Genesis** | Pinned height-0 block and initial state |
| **k / wins_required** | Number of valid proofs a block must carry |
| **Low ply** | A game shorter than the network's `min_plies` |
| **MTP** | Median time past of the previous 11 block timestamps |
| **Opening hash** | Commitment to the book version, line id and move list |
| **Proof id** | Hash of game hash, match id and winner |
| **Roster** | Miners with valid availability and reveals for an epoch |
| **Seal** | Header hash work measured in leading zero bits |
| **Self-play** | One operator controlling both seats of a match |
| **Timeout claim** | Bonded assertion that an opponent's clock reached zero |
| **Transcript** | Certificate, signed moves, result, receipts and PGN |
| **Witness** | Bonded key that signs clock receipts |
| **Win proof** | Self-contained evidence that a miner won an eligible game |

---

## 20. References

### 20.1 Repository documents

- [`docs/PROTOCOL.md`](PROTOCOL.md) — normative wire formats and rule details.
- [`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — next steps,
  acceptance criteria and improvement areas.
- [`docs/PLAY_AND_EXPORT.md`](PLAY_AND_EXPORT.md) — on-chain play and game
  export walkthrough.
- [`docs/OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — wallet creation, backup and
  restore.
- [`docs/SEND_COINS.md`](SEND_COINS.md) — transfers, fees and settlement.
- [`docs/RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — relay and miner
  operator guide.
- [`README.md`](../README.md) — overview, quickstart and command examples.
- `data/openings.json` — the shipped opening book.
- `data/engines.json` — pinned engine profiles and checksums.
- `genesis/{testnet,mainnet,devnet}.json` — pinned genesis documents.

### 20.2 Standards and libraries

- **Ed25519** — RFC 8032.
- **BIP-39** — mnemonic code for generating deterministic keys.
- **Bech32** — BIP-173, used for address text with a 21-byte program.
- **AES-256-GCM** — NIST SP 800-38D, used for keyfile encryption.
- **scrypt** — RFC 7914, used for keyfile key derivation.
- **UCI** — Universal Chess Interface, the engine protocol.
- **PGN** — Portable Game Notation, the transcript text format.
- **python-chess** — board state, move generation, PGN and UCI support.
- **drand quicknet** — BLS12-381 randomness beacon, supported as an optional
  mix-in; a pinned verifier is still outstanding.

### 20.3 Design lineage

The project borrows ideas, not code, from:

- Bitcoin: proof of work, account/UTXO trade-offs, cumulative-work fork choice,
  coinbase maturity and halving.
- Ethereum: account model and state roots (without a VM).
- Commit/reveal randomness beacons and drand: public, verifiable epoch
  randomness.
- Tournament chess: pairing, colours, openings, clocks and PGN as an auditable
  record.

### 20.4 A closing note on claims

This whitepaper describes a working prototype. It deliberately marks the line
between implemented and remaining rather than presenting the design as finished.
The strongest honest claim is:

> A miner can prove that it won a legal, assigned, previously unseen chess game
> against another registered key, and the chain can verify that proof without
> trusting the relay.

The claim it does **not** make is:

> The two keys are controlled by different people, or both engines played
> honestly and well.

Quality sampling and bonded challenges narrow that gap, but they do not close
it. Read every "proof of chess" statement with the structural and economic
qualifiers attached.
