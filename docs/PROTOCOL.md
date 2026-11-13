# sccp wire protocol, version 1

Status: **normative** for protocol v1 (canonical encoding, game hash, PGN
contract, opening book, engine fingerprints, epoch beacons, pairing,
certificates, transcripts, clocks and clock evidence, win-proof verification,
the ledger — transaction envelopes, account state, block headers and bodies,
difficulty, fork choice and genesis documents — the quality and challenge
lifecycle, and the node: HTTP API, gossip envelopes, peer scoring and
pull-sync). The clock layer covers the off-chain primitives *and* on-chain
settlement.

Reference implementation: `src/sccp/protocol/`, `src/sccp/chess/`,
`src/sccp/chain/`, `src/sccp/node/`, `src/sccp/miner/`, `src/sccp/devnet.py` and
`src/sccp/serialization.py`. This document is the normative source for these
rules; forward work is tracked in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

---

## 1. Conventions

- **Hash function.** `H(domain, payload) = SHA-256(domain.encode("utf-8") ||
  0x00 || canonical_json(payload))`, where `canonical_json` is sorted-key,
  compact, UTF-8, NFC-normalized JSON with no floats and integers in
  `[-2**63, 2**64 - 1]`. `src/sccp/serialization.py` is the only encoder.
  The beacon and pairing-score constructions in sections 6 and 7 are byte-level
  concatenations instead and specify their own input encoding.
- **Domains.** Every domain starts with `sccp/v1/`. Chess uses `game` and
  `opening`; matchmaking uses `beacon`, `beacon-commit`, `pair`, `match`,
  `certificate`, `result` and `proof`.
- **Digests.** Lowercase hex, 32 bytes (64 characters).
- **Ints, never floats.** All numeric protocol values are integers. No
  wall-clock value enters a game hash.
- **Network binding.** `network_id` (`testnet`, `mainnet`, `devnet`) is part of
  every game hash, so a testnet game can never be replayed as a mainnet game.

## 2. Canonical game hash

```text
game_hash = H("sccp/v1/game", {
    "network":   <network id>,
    "start_fen": <standard start FEN>,
    "san_list":  [<SAN>, ...],
    "result":    "1-0" | "0-1" | "1/2-1/2"
})
```

Rules:

1. `san_list` is produced by replaying the mainline from the standard start
   position and calling `Board.san(move)` **on the live board at that ply**.
   Re-deriving SAN from the final or root board is forbidden: `Board.san()`
   depends on the current position.
2. SAN is the canonical form emitted by `python-chess` 1.11.x: `O-O`, `O-O-O`,
   `e8=Q`, `exd8=Q+`, `Qxf7#`, and explicit disambiguation where required.
3. `result` uses the PGN vocabulary. `"*"` never enters a proof.
4. `start_fen` is `rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1`.
   Protocol v1 has no variant start positions; a PGN carrying a different
   `FEN` (or any `SetUp`) is rejected.
5. PGN headers, comments, clock annotations, move numbers and whitespace are
   **not** hashed. The same moves with different metadata hash identically; a
   different move order or result hashes differently.
6. The hash commits to uniqueness only. Certificates commit to identity,
   pairing and engine metadata; transcript hashes are a separate construction.

## 3. PGN contract

A proof-grade PGN is UTF-8, at most `max_pgn_bytes`, and contains exactly one
game. The payload is rejected when `chess.pgn.read_game` reports an error, or
when a second game follows the first.

### 3.1 Required headers

| Tag | Meaning |
| --- | --- |
| `Event`, `Site`, `Date`, `Round` | Free-form event metadata (never hashed) |
| `White`, `Black` | Seat addresses of the players |
| `Result` | `1-0`, `0-1` or `1/2-1/2` (`*` is rejected) |
| `Termination` | `checkmate`, `draw`, `resignation` or `time_forfeit` |
| `MatchID` | Match identifier from the certificate |
| `TimeControl` | `<initial>+<increment>` in whole seconds; must equal the certificate |
| `Epoch`, `RoundNumber` | Non-negative integers; must equal the certificate |
| `SeatWhite`, `SeatBlack` | Bech32 addresses; must equal the certificate |
| `EngineWhite`, `EngineBlack` | Engine fingerprints; must equal the certificate |
| `OpeningID`, `OpeningHash` | Book line id and its opening hash |
| `Rules` | Exactly `chess` |

Every tag must be present and non-empty. When a certificate is supplied,
`MatchID`, `Epoch`, `RoundNumber`, `SeatWhite`, `SeatBlack`, `EngineWhite`,
`EngineBlack`, `OpeningID`, `OpeningHash` and `TimeControl` are compared
exactly; any mismatch is a validation error. The `TimeControl` form is strict:
decimal seconds with no underscores or whitespace, initial time at least one
second, and the worst case `initial_ms + increment_ms * max_plies` may not exceed
`MAX_CLOCK_MS` (24 hours).

### 3.2 Replay

- Replay starts from the standard position. Every mainline move must satisfy
  `Board.is_legal(move)` **before** it is pushed; `Board.push()` does not
  validate and must never be trusted as a gate.
- The game may not exceed `max_plies`; a proof also requires at least
  `min_plies` half-moves.
- Variations are ignored for hashing. When a certificate is supplied (the proof
  path) a PGN containing any variation is rejected; the offline verifier
  (`sccp verify pgn`) accepts them.
- Comments and clocks are preserved in the stored PGN and ignored for hashing.

### 3.3 Result and termination derivation

`computed = board.outcome(claim_draw=False)` decides the result; the headers
must agree with it.

| Board state | Required `Result` | Required `Termination` |
| --- | --- | --- |
| Checkmate | winner (`1-0` or `0-1`) | `checkmate` |
| Stalemate, insufficient material, 75-move rule, fivefold repetition | `1/2-1/2` | `draw` |
| No automatic outcome, both seats attest the loss | decisive | `resignation` |
| No automatic outcome, `ClockEvidence` proves a flag | winner | `time_forfeit` |
| Anything else (abandoned, unterminated) | rejected | rejected |

A `time_forfeit` PGN is accepted only when its `TimeControl` matches the
certificate and the accompanying clock evidence verifies. The protocol uses one
fixed 30+0 control for every match; matchmaking does not offer per-match
choices. See section 8.2 below and the clock implementation in
`src/sccp/protocol/clocks.py`.

Notes:

- `claim_draw=False` deliberately excludes claimable 50-move and threefold
  draws. A game stopped by one of those is only valid as a resignation.
- For a resignation the side to move is the side that resigned, so a White
  resignation is `0-1`.
- A resignation is additionally subject to the quality policy through
  `validate_pgn(..., resignation_check=...)`; the check must confirm the
  position is lost.
- Draws are valid PGNs but earn nothing; a win proof requires a decisive
  result.


## 4. Opening book

The book is `data/openings.json`, shipped inside the wheel at
`sccp/chess/data/openings.json`.

```json
{
  "format": 1,
  "books": {
    "v1": {
      "description": "...",
      "lines": [
        {"name": "Italian Game: Giuoco Piano",
         "moves": ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]}
      ]
    }
  }
}
```

- `line_id` is the line's zero-based index in its book. It is stable for a
  published book version: never reorder or delete a line, only append.
- Each line has 1–12 plies of legal SAN from the standard start. Stored SAN is
  normalized to canonical SAN on load (check and mate markers included).
- Loading replays every line. An illegal move, a line that continues after the
  game ended, a duplicate name, an empty book or an unsupported `format` is a
  hard error, not a warning.
- `opening_hash = H("sccp/v1/opening", {book_version, line_id, moves})` where
  `moves` is the canonical SAN list.
- Selection for a match:

  ```text
  index = int.from_bytes(SHA256(beacon || "opening" || match_id)[:8], "big")
          % len(book)
  ```

  `beacon` is the 32-byte epoch beacon; any other length is rejected. Selection
  is deterministic and uniform over the book.
- The certificate carries the chosen line and its `opening_hash`, so a verifier
  checks the replay prefix without consulting a local book.

## 5. Engines and fingerprints

A match certificate records one fingerprint per seat. Verifiers use it to
detect "same moves, different claimed engine" substitutions.

| Engine | Fingerprint |
| --- | --- |
| UCI binary | SHA-256 of the executable file, lowercase hex |
| `mock` | `mock-v1:<first 16 hex of SHA-256("mock-v1:<seed>:<name>")>` |
| `scripted` | `scripted:<first 16 hex of SHA-256(<comma-joined move list>)>` |

Engine profiles and pinned downloads live in `data/engines.json` (shipped in the
wheel as `sccp/chess/data/engines.json`) and are installed by
`scripts/fetch_engine.py`. The manifest pins, per engine and
platform, the HTTPS URL, the archive SHA-256 and the archive member. A download
whose digest does not match is never extracted. Profiles in this release:
`mock`, `stockfish-19` (current) and `stockfish-18` (older reference). Engine
binaries are GPL-3.0 and are not redistributed with this package.

## 6. Epoch beacon

A miner commits in epoch `e-2` and reveals in `e-1`:

```text
commitment = H("sccp/v1/beacon-commit", {address, epoch, nonce})
```

`nonce` is a 32-byte value as 64 lowercase hex characters. The payload has no
network field because the address already commits to its network.

The beacon for epoch `e` is a byte-level hash over public data:

```text
digest = SHA256("sccp/v1/beacon" || 0x00 || network || 0x00 || u64(epoch)
                || block_hash
                || concat(sorted(commitment_bytes || nonce_bytes)))
B_e    = digest                                   when drand_used = 0
B_e    = SHA256(digest || drand_signature_bytes)  when drand_used = 1
```

`block_hash` is the 32-byte hash of `H_e`, the last block of `e-1`. Each reveal
contributes 64 bytes (32-byte commitment + 32-byte nonce); entries are sorted as
byte strings, so reveal order never affects the result. `u64(x)` is 8-byte
big-endian.

Validation:

1. `network` is the verifier's network and `epoch` is the epoch being checked.
2. Every reveal's address belongs to the network, is unique, and recomputes its
   commitment. When availability transactions are supplied, the commitment must
   equal the availability commitment, and a reveal without availability is
   rejected.
3. Availability with no reveal is **not** a beacon error: the miner is omitted
   from the epoch roster and the withholding is public, so the chain or relay can
   record it.
4. `drand_used = 1` requires both `drand_round` and a 48-byte (96 hex chars)
   `drand_signature`, plus a pinned verifier that accepts `(round, signature)`.
   A flag with no signature, a signature with the flag clear, or a missing
   verifier is invalid. `drand_used = 0` is always valid and needs no verifier.
5. The round is deterministic:
   `round(e) = mtp(H_e) // drand_period_seconds + drand_round_offset`, where
   `mtp` is the median of the last 11 block timestamps ending at `H_e` (lower
   median for an even count). quicknet uses 3 s and offset 1.
6. Recompute `B_e` and compare it with the declared `hash`.

drand quicknet signs with BLS12-381. The reference implementation injects a
`DrandVerifier` rather than pulling in a second crypto stack. A verifier pinned
to the quicknet chain key is not bundled yet, so a caller must supply one;
without it, `drand_used = 1` is rejected instead of accepted blindly. Pinning
the verifier is tracked in the implementation plan.

## 7. Pairing

Pairing is a pure function of the roster, the 32-byte beacon and the round:

```text
score(m) = SHA256("sccp/v1/pair" || network || beacon || u64(round) || address)
```

Candidates with a positive quota are sorted by `(score, address)`. Adjacent
candidates are paired from the front; when a pair breaks a soft constraint the
second seat is swapped with the first later candidate that satisfies it, and the
violation is accepted if no swap exists. An odd miner sits the round out.

- Hard constraints: distinct addresses, both in the roster, both with quota.
- Soft constraints: shared funder, subnet or ASN, and a recent opponent when
  `opponent_cooldown > 0`.
- Quota defaults to `max(0, min(max_games, rounds_per_epoch) - round)`: one game
  per miner per round, so a booked quota of `g` allows `g` rounds. Explicit
  quotas override the default.
- `match_id = H("sccp/v1/match", {network, epoch, round, seats})` with `seats` the
  sorted address list. It deliberately excludes the opening.
- Colour draw: `int.from_bytes(SHA256(beacon || "color" || match_id)[:8], "big")
  & 1`; 0 means the lexicographically first seat is White.
- Opening: `book.select(beacon, match_id)`, the same formula as section 4.
- `verify_pairing(matchup, roster, beacon)` recomputes the round and requires an
  exact match on match id, colours, epoch and round.

## 8. Certificates, transcripts and the relay

### 8.1 Start certificate

A certificate carries the opening line itself, so a verifier needs no local book
to check the prefix:

```json
{"network": "testnet", "match_id": "hex", "epoch": 3, "round": 0,
 "white": "sccpt1...", "black": "sccpt1...",
 "opening": {"book_version": "v1", "line_id": 17, "hash": "hex",
             "moves": ["e4", "e5", "Nf3"]},
 "engine_white": "sha256-or-mock-id", "engine_black": "...",
 "time_control": {"initial_ms": 1800000, "increment_ms": 0},
 "start_signatures": {"white": "hex", "black": "hex"}}
```

Both seats sign this payload with `H("sccp/v1/certificate", …)`:

```json
{"network": "...", "match_id": "...", "epoch": 3, "round": 0,
 "seats": ["<sorted seat A>", "<sorted seat B>"],
 "colors": {"white": "<address>", "black": "<address>"},
 "opening_hash": "hex",
 "time_control": {"initial_ms": 1800000, "increment_ms": 0},
 "engine_fingerprints": {"white": "...", "black": "..."}}
```

`start_signatures` is excluded from the payload. A certificate is valid only
when both signatures verify against the addresses' registered Ed25519 keys, the
colour draw matches the beacon, and `match_id` recomputes from the seats.

The certificate commits to `time_control = {initial_ms, increment_ms}` and the
signed payload covers it. Every match uses one protocol-fixed **30+0** control
(`initial_ms = 1_800_000`, `increment_ms = 0`); matchmaking never offers or
accepts a per-match choice. See section 8.4 for the timed-session API.

### 8.2 Result attestation

When the board is terminal or a seat resigns, both seats sign:

```json
{"network": "...", "match_id": "...", "game_hash": "hex",
 "result": "1-0" | "0-1" | "1/2-1/2",
 "termination": "checkmate" | "draw" | "resignation" | "time_forfeit",
 "plies": 87,
 "result_signatures": {"white": "hex", "black": "hex"}}
```

The signature domain is `sccp/v1/result`; `result_signatures` is excluded from
the signed payload. `game_hash` is the canonical game hash from section 2, so the
two seats also attest the exact move list.

A checkmate may be proven without the loser's result signature: the winner
supplies its own signature plus the signed move messages (`move_evidence`), and
the verifier replays them to checkmate. A time forfeit likewise needs only the
winner's signature when the proof carries the `timeout_claim_id` and complete
`clock_evidence`; the evidence includes witness-signed move receipts and the
final flag receipt. Resignation and agreed draws still need both signatures.
Exactly one missing result signature is tolerated, and the winner must be the
seat that signed. The offline verifier checks the actual signature bytes against
the registered keys.

### 8.3 PGN assembly

A transcript PGN is the game plus the required tags of section 3.1, with
`White`/`Black` = seat addresses, `MatchID`, `Epoch`, `RoundNumber`,
`SeatWhite`, `SeatBlack`, `EngineWhite`, `EngineBlack`, `OpeningID`,
`OpeningHash`, `TimeControl = <initial>+<increment>` and `Rules = chess`.
`Termination` and `Result` come from the result attestation. The opening prefix
is included in the move list. A `time_forfeit` PGN without a matching
`TimeControl` tag is rejected before the clock evidence is considered.

### 8.4 Relay API

The relay is untrusted. It derives the round from public data and stores
signatures; it never signs a result. It does keep the clock: every session runs
the fixed **30+0** control (`TIMED_SESSIONS` in `sccp.miner.matchmaker`), charged
from the relay's own monotonic clock, and the receipts it issues are the evidence
the chain's bonded claim lifecycle (section 10.4) settles.

Timed sessions work like this:

- The **second start signature activates the session** and starts both clocks;
  the side to move is on the clock.
- The relay's monotonic millisecond source is authoritative. A move body may
  carry `clock_ms`, but it is display data: the relay ignores it and recomputes
  from its own source.
- A move is checked for turn and SAN legality *before* the clock is charged, so
  an illegal move costs no time. A charge is `elapsed - lag_grace_ms` (floored at
  zero) plus the increment, applied to the mover only.
- A move arriving after the mover's clock reached zero is refused and the
  session is flagged instead. The flag ends the game whether or not the losing
  seat ever answers, so a stall cannot deny the win.
- Every accepted post-opening ply gets a **move receipt** (`kind: "move"`, `ply`,
  `seat`, `san`, `received_at_ms`, both clocks); a flag adds a **flag receipt**
  (`kind: "flag"`, `ply` = total plies, the flagged `seat`, both clocks, the
  flagged clock at zero). Both are signed by the relay's clock witness when the
  relay runs with a witness key, and carry no witness signatures when it does
  not (a relay may observe without attesting).
- A live clocked session is never abandoned by `timeout_seconds`: that reaper
  only applies to pending joins, because abandoning a clocked game would let the
  losing seat escape the flag.
- After a restart the relay re-bases every live clock to "now" for the side to
  move. Monotonic timestamps from the previous process are meaningless here, and
  flagging on them would punish a seat for the relay's downtime.

| Method | Path | Body |
| --- | --- | --- |
| POST | `/v1/rounds` | `{"round": 0}` — derive and create that round's sessions |
| GET | `/v1/matches` | all sessions, with live clocks |
| GET | `/v1/matches/{id}` | one session: certificate, SAN list, result, PGN, clocks |
| GET | `/v1/matches/{id}/receipts` | the `ClockBundle`: time control, clocks, every receipt, the flag |
| GET | `/v1/matches/{id}/transcript` | co-signed transcript, 404 until both signs |
| POST | `/v1/matches/{id}/join` | `{"seat", "public_key", "signature"}` |
| POST | `/v1/matches/{id}/move` | `{"seat", "san"}`, or the signed form below |
| POST | `/v1/matches/{id}/resign` | `{"seat"}` |
| POST | `/v1/matches/{id}/result` | `{"seat", "signature"}` |
| GET | `/v1/status`, `/v1/reputation` | relay status (timed fields, round, witness) and no-show counts |

`MatchView` carries `time_control`, `white_ms`, `black_ms`, `turn`, `last_ply`,
`flagged` and `receipts` (a count; fetch the bundle for the objects).
`ClockBundle.receipts` is exactly the `clock_evidence.receipts` list of a
`TimeoutClaim`, and `ClockBundle.flag` is its `final`.

The signed move body is `{network, match_id, seat, ply, san, clock_ms, signature}`;
`clock_ms` is neither signed nor trusted. The signed payload is
`{network, match_id, seat, ply, san}` under `sccp/v1/move`, and `san` must be the
canonical SAN for the position. A body carrying a signature must also carry
`network` and `ply`, and the signature is verified against the registered seat
key. Moves remain off-chain and only the final proof (or timeout claim) reaches a
block; because each move is signed, a checkmate can later be proven without the
loser's result signature. See section 8.4 for the signed move stream.

Illegal moves, wrong turns, stale plies, forged bodies, duplicate joins and bad
signatures are rejected with HTTP 400 and never change the session or its
clocks. A pending session may be abandoned after `timeout_seconds`; that is a
liveness choice, not evidence. Restart-safe state is a JSON document
(`format: 2`) of sessions with their clocks, receipts and flag; pending sessions
replay their SAN list on load and live clocks are re-based as described above.

`sccp matchmaker run --config relay.json [--witness-key relay.key]
[--round-seconds 900] [--sweep-seconds 1]` reads:

```json
{"network": "testnet",
 "beacon": {"network": "testnet", "epoch": 3, "hash": "hex", "block_hash": "hex",
             "reveals": [...], "drand_used": false},
 "roster": {"sccpt1...": {"public_key": "hex", "engine": "sha256-or-mock-id",
                           "max_games": 3}},
 "timeout_seconds": 300,
 "lag_grace_ms": 250,
 "timeout_dispute_window": 2,
 "min_timeout_bond": 1,
 "clock_witness_quorum": 1,
 "clock_witness_bond": 1,
 "round_seconds": 900,
 "open_round": 0}
```

`engine` is the fingerprint the seat committed to at registration; `open_round`
is optional and opens one round at startup. `round_seconds` (or
`--round-seconds`) is relay policy, not consensus: while the relay serves, a
background loop sweeps the clocks and opens the next round once the interval has
passed, up to `rounds_per_epoch`. `--witness-key` makes the relay sign its
receipts. A miner given the matching `--relay-witness-key` (the witness's public
key in hex) verifies those receipts locally before it builds a `timeout_claim`;
without it the claim is refused rather than guessed at.

## 9. Win proofs and offline verification

A proof wraps the co-signed transcript:

```json
{"network": "testnet", "proof_id": "hex", "game_hash": "hex",
 "match_id": "hex", "epoch": 3, "round": 0,
 "winner": "sccpt1...", "loser": "sccpt1...", "winner_seat": "white",
 "result": "1-0", "termination": "checkmate", "plies": 87, "low_ply": false,
 "opening": {...}, "pgn": "[Event ...] ...",
 "certificate": {...}, "result_signatures": {"white": "hex", "black": "hex"},
 "clock_evidence": null, "move_evidence": null, "timeout_claim_id": null,
 "quality_summary": null}
```

`proof_id = H("sccp/v1/proof", {game_hash, match_id, winner})`. The optional
`quality_summary` is evidence for the quality layer and is ignored by structural
verification.
`clock_evidence` is required for every `time_forfeit` and carries the time
control, the witness-signed final flag receipt and every post-opening move
receipt. `timeout_claim_id` binds a single-signature proof to its bonded timeout
claim; it is null for every other termination.
`move_evidence` is the signed post-opening move stream for a single-signature
`checkmate`. Both evidence fields must be absent for other terminations.

Verification runs cheap checks first and stops at the first typed failure:

| Reason | Check |
| --- | --- |
| `network` | proof, certificate and beacon all name the verifier's network |
| `beacon` | epoch matches and `B_e` recomputes from reveals/availability/drand |
| `certificate` | match id, epoch, round, seats, winner seat, colour draw |
| `opening` | line hash, canonical SAN, book version and beacon-selected line |
| `roster` | both seats are in the verifier's roster |
| `pairing` | the matchup is exactly what the beacon and roster produce |
| `signature` | both start signatures and every present result signature verify; a missing loser signature requires matching clock or move evidence |
| `pgn` | section 3 replay, headers including `TimeControl`, and opening prefix |
| `game-hash` | the replayed SAN list hashes to `game_hash` |
| `result` | result, termination, plies and winner seat match the replay; clock and move evidence are checked for `time_forfeit` and `checkmate` |
| `proof-id` | `proof_id` recomputes from its fields |
| `duplicate` | neither hash has already been seen by the caller |

A proof that passes is structurally and cryptographically valid. It says nothing
about how well the moves were played; quality is handled separately as sampled
evidence and a bonded challenge process.

### 9.1 Context document

`sccp verify proof FILE --context FILE` reads public data only:

```json
{"network": "testnet",
 "beacon": {"network": "testnet", "epoch": 3, "hash": "hex",
             "block_hash": "hex", "reveals": [...], "drand_used": false},
 "roster": {"sccpt1...": {"public_key": "hex", "max_games": 3}},
 "availability": {"sccpt1...": "commitment-hex"},
 "quotas": {"sccpt1...": 2},
 "seen_game_hashes": [], "seen_proof_ids": []}
```

`availability`, `quotas`, `drand_round` and the seen sets are optional. The
opening book is loaded from the installed package unless `--book` names an
override. drand needs a callable verifier, so a `drand_used = 1` beacon fails
offline verification unless the caller supplies a pinned verifier.

### 9.2 Quality evidence and challenges

`quality_summary` is optional evidence. It never affects structural verification
or block validity. A node that wants to audit a proof selects it
deterministically from `H(block_hash || proof_id) mod 1000 < sample_rate`, so
every validator computes the same sample without coordination. Analysis results
are reported by the node's quality sampler; a persistent disagreement can become
a bonded `challenge` transaction (section 10.2).

A challenge is valid only inside `challenge_window`, must name an accepted
proof, and cannot be opened by the proof's miner. The challenger's bond is
locked. The adjudication panel is derived from the ledger and beacon: eligible
bonded members are ranked by the challenge domain hash, the parties are excluded,
and the panel size is odd. In the current implementation panel votes are
delivered to the node/operator rather than committed as a transaction, so
`adjudicate_with_panel` is called with a complete ballot. An upheld challenge
returns the challenger's bond, slashes the proof miner's bond and burns the
still-unvested coinbase credit from the proof's block; a rejected challenge burns
the challenger's bond. On-chain panel voting is tracked in the implementation
plan.

## 10. Transactions

A transaction is one canonical JSON object: the same shape in the mempool, in a
block body and on the wire. `src/sccp/chain/tx.py` is the only validator, and
`ALLOWED` there is the normative list of payload keys per type.

| Field | Type | Rule |
| --- | --- | --- |
| `network` | string | equals the chain's `network_id` |
| `type` | string | one of the twelve values in 10.2 |
| `from` | address | the sender; the Python attribute is `sender` |
| `nonce` | int ≥ 0 | equals the sender's current nonce; a gap is a rejection |
| `fee` | int ≥ 0 | at least `min_fee`, debited from spendable balance, never from a bond |
| `payload` | object | type-specific; unknown keys are rejected |
| `signature` | 128 hex chars | Ed25519 over the txid; `null` only for a coinbase |

`txid = H("sccp/v1/tx", {network, type, from, nonce, fee, payload})`. The
signature is outside the hash, so signing is well defined and re-signing never
changes an id; every payload is normalized (section 1) before hashing. One
`(from, nonce)` pair may exist at most once on a chain and at most once in a
mempool, which is the anti-replay rule at this layer.

Validation order is fixed and identical in both places: schema, network, sender
key and signature, nonce, fee, balance, type rules, size (`max_tx_bytes`).
Blocks rerun the whole checklist from scratch, so a mempool bug cannot make an
invalid block valid. Failures carry a stable reason: `schema`, `network`,
`signature`, `nonce`, `balance`, `fee`, `type`, `size`, `duplicate`, `window`,
`bond`, `registration`.

### 10.1 Sender keys

Ed25519 is not key-recoverable, so a spend needs the sender's public key from
somewhere. Two sources are authoritative: a genesis allocation and a miner's
`register` transaction, both of which store the key on the account. An account
with neither (a plain recipient) puts `public_key` in the payload of its first
transaction; consensus then checks that the key re-encodes to the `from` address
before storing it. The reveal is self-authenticating, so no key registry and no
extra signature format is needed.

### 10.2 Types and payloads

| Type | Payload keys | Effect |
| --- | --- | --- |
| `transfer` | `to`, `amount`, `public_key`\* | moves `amount` from `from` to `to`; `to != from`, `amount >= 1` |
| `register` | `public_key`, `endpoint`, `engine`, `bond` | locks `bond >= min_bond`, stores key/endpoint/engine, sets `registered_until_epoch = epoch + registration_epochs`; refused while banned |
| `register_witness` | `public_key`, `endpoint`, `bond` | locks `bond >= clock_witness_bond` and registers a clock witness for `registration_epochs` |
| `update_registration` | `endpoint`, `engine`, `extend_epochs`, `bond`, `public_key`\* | must change at least one of those; optionally tops up the bond |
| `unregister` | `public_key`\* | clears registration and starts `unbond_delay` at `height + unbond_delay` |
| `withdraw_bond` | `public_key`\* | only after the unbonding height, and only the unlocked bond, moves back to spendable |
| `availability` | `epoch`, `max_games`, `commitment`, `public_key`\* | commits for `epoch = current + availability_lead`; `1 <= max_games <= rounds_per_epoch`; the committer must be registered through `epoch`, hold `min_bond`, and may not change a commitment |
| `reveal` | `epoch`, `nonce`, `public_key`\* | only valid in epoch `epoch - 1`, must open that epoch's commitment, may not change |
| `timeout_claim` | `match_id`, `certificate`, `last_move`, `clock_evidence`, `bond`, `public_key`\* | opens a bonded claim (10.4) |
| `timeout_dispute` | `claim_id`, `move`, `move_signature`, `clock_evidence`, `public_key`\* | the accused answers with a later signed move (10.4) |
| `slash_evidence` | `evidence`, `proof_id`, `public_key`\* | burns an equivocating clock witness (10.4); `proof_id` is refused here and is handled by the challenge layer |
| `challenge` | `proof_id`, `reason`, `evidence`, `bond`, `public_key`\* | locks a bond and opens a quality challenge; an upheld challenge slashes the proof miner and burns unvested coinbase, a rejected challenge burns the challenger's bond (9.2) |

\* optional: only needed for an account whose public key the chain does not
already know (10.1).

### 10.3 Coinbase

Every block above genesis carries exactly one coinbase: an *unsigned* `transfer`
from the miner to itself with `nonce = 0`, `fee = 0` and
`payload = {to: miner, amount: subsidy + fees}`. Its authorisation is the block
seal, not a signature, so it is never applied as an ordinary transaction and
consumes no nonce. `subsidy = params.subsidy >> (height // halving_interval)`,
clamped so minted supply never exceeds `max_supply`; testnet and devnet have no
cap and no halving. The credited amount matures after `coinbase_maturity` blocks
(`immature` becomes spendable) and a body transaction may not imitate a coinbase.

### 10.4 Bonded timeout settlement

A losing seat can always stall, so a flag must settle without its cooperation.
The lifecycle (implemented in `src/sccp/chain/timeout.py`) is:

1. `timeout_claim` — from one seat of the match, bonded with
   `bond >= min_timeout_bond`, carrying the certificate, the claimant's own
   signed `last_move`, and `clock_evidence` whose `final` flag receipt is for
   ply `last_move.ply + 1`, names the *other* seat, shows `0` ms for that seat
   and a positive clock for the claimant, matches the certificate's
   `time_control`, and carries at least `clock_witness_quorum` signatures from
   witnesses registered and unexpired at the certificate's epoch. The bond is
   debited (never the fee's source) and the record gets
   `deadline_height = height + timeout_dispute_window`. Only one claim per match
   may be open.
2. `timeout_dispute` — from the **accused** seat only, at or before
   `deadline_height`, carrying a later signed move of its own (`ply >
   last_move.ply`). It returns the bond to the claimant and makes the claim
   unfinalisable.
3. Finalisation is not a transaction: when a `WinProof` with
   `termination = "time_forfeit"` and `timeout_claim_id` is accepted, the claim
   must exist, match the proof's certificate, be opened by the proof's winner
   and be undisputed; the bond then returns to the claimant. A claim may only
   finalise in a block **after** its deadline.

`slash_evidence` covers clock-witness equivocation only: it carries
`evidence = {witness, receipts}` with exactly two receipts of the same
`(match_id, kind, ply)`, different contents and valid signatures from the named
witness. The witness must be registered with a non-zero bond; the bond is burned,
the witness is de-registered and `banned_until = height + unbond_delay`.
Equivocation about proofs (`proof_id` in the payload) is refused here; proof
disputes go through the bonded challenge layer (9.2).

## 11. Ledger state

The state is a deterministic, integer-only object (`src/sccp/chain/state.py`);
nothing in it reads a clock, a random source or the network. It holds one
`Account` per address:

| Account field | Meaning |
| --- | --- |
| `balance` | spendable base units |
| `immature` | coinbase credits that mature at a later height |
| `bonded` | locked bond (registration or witness) |
| `nonce` | next valid transaction nonce |
| `public_key` | the spend key, once known (10.1) |
| `registered_until_epoch`, `endpoint`, `engine`, `unbonding_height`, `banned_until` | registration and bond lifecycle |

alongside the proof and match registries (`game_hash -> proof_id`, match id ->
epoch/round/seats), per-miner stats (wins, games, last opponent epoch, which is
what the opponent cooldown reads), per-epoch records (beacon, roster, quotas,
matchable flag), the availability commitments and reveals (by epoch), the clock
witness registry, and open/closed timeout claims.

`state_root` is the merkle root (section 1) over `address -> account` leaves,
where an account leaf commits to every field of the table above. `roster_root` is
the same construction over the epoch's roster, committing only
`{public_key, max_games}` per member. Both are recomputed rather than trusted.

Snapshots are JSON with the top-level keys `network`, `height`, `tip_hash`,
`accounts`, `proofs`, `proof_ids`, `matches`, `miner_stats`, `epochs`,
`availability`, `reveals`, `witnesses`, `timeout_claims`, `difficulty`,
`last_k_change_height`, `timestamps` and `minted`. A snapshot is a cache: a node
replaying blocks checks the snapshot's `state_root` against the state it
rebuilds, so a corrupted snapshot cannot become consensus.

## 12. Blocks, difficulty and fork choice

A block is a header plus a body of *one* coinbase, a list of transactions and a
sorted list of win proofs. The header commits, in wire order, to:

| Header field | Rule |
| --- | --- |
| `version` | `1` |
| `network` | the chain's `network_id` |
| `height` | parent height `+ 1` (`0` for genesis) |
| `prev_hash` | the parent's block hash; 32 zero bytes for genesis |
| `merkle_root` | body merkle root, below |
| `state_root` | the state merkle root *after* the block (section 11) |
| `roster_root` | the epoch roster root (section 11) |
| `timestamp` | strictly greater than the median of the last 11 timestamps, at most 2 h ahead of the verifier |
| `epoch` | `height // epoch_length` |
| `beacon_hash` | the epoch's `B_e` (0 before the first matchable epoch) |
| `wins_required`, `seal_bits` | exactly the values the retarget schedule yields |
| `seal_nonce` | the value the miner ground for the seal |
| `miner` | the coinbase payee and the required winner of every proof |

`block_hash = H("sccp/v1/header", header)`. `merkle_root` is the merkle root of
the ordered list `[coinbase_txid, txid..., proof_id...]` — *given* order, no
sorting, leaves `sha256(0x00 || str(index) || 0x00 || canonical_json(item))`, an
odd node promoted unchanged, and the 32-zero-byte hash for an empty body. Genesis
therefore has a zero body root and no coinbase.

### 12.1 The seal and difficulty

`block_work(seal_bits, k) = (1 << seal_bits) + k * 65536` is what a header
*declares*. Fork choice credits `earned_work`: the same expression minus
`k * 65536` when the block carries fewer than the `k` proofs it owes. A bootstrap
block and a heartbeat block (section 12.2) carry none, so each earns its seal
work alone; crediting the chess term regardless would let a proofless branch
claim work nobody performed. A chain's weight is the sum of earned work over its
blocks, so depth plus work *actually performed* decide forks. Genesis is the
root and contributes its declared work as a constant every chain shares.

The seal is valid when `int(block_hash, 16) < 2 ** (256 - seal_bits)`; with
`seal_bits = 0` every hash passes, which is why testnet and devnet can seal for
free. `wins_required` (`k`) is the real cost: it is the number of verified chess
wins a block must carry. Both values are retargeted from the parent's timestamps
every `retarget_interval` blocks, comparing elapsed time with
`expected_interval_seconds`: slower than 4/3 of the target lowers `seal_bits` by
one bit and may lower `k`, faster than 3/4 raises them. `k` is clamped to
`[k_min, k_max]`, may change at most once per epoch, and never within
`epoch_length` blocks of the previous change (so open claim windows survive).
A header that declares easier values than the schedule is invalid, so every node
derives the schedule itself. A miner therefore controls the schedule through the
timestamps it writes: a chain mined faster than `target_block_seconds` is
answered with a higher `k`, and one block then has to carry that many verified
wins. A deployment whose roster cannot supply them paces its miner at the target
interval instead -- `sccp node run --mine-interval-seconds`, and the bootstrap
scripts, which synthesize timestamps one target interval apart -- so the
retarget window stays in its neutral band.

### 12.2 Proof rules

A block at `height` must carry exactly `required_proofs(height, k)` proofs, which
is `0` up to and including the end of the first realizable matchable epoch (the
bootstrap exemption, a pure function of the parameters) and `k` afterwards. Each
proof must: name this network and this header's `miner` as winner; reference an
epoch that ended before this block's epoch and that has a matchable roster, with
the winner on that roster; use a `game_hash` and `proof_id` not already on the
chain and not repeated in the block; sit inside
`(epoch_end, epoch_end + claim_window]`, i.e. settle from the first block of the
next epoch until the claim window closes; respect the `opponent_cooldown` against
the winner's previous games (a cooldown of `0` disables the rule, which is how
testnet and devnet let a small roster settle every game it plays; mainnet keeps
`4`); declare `low_ply` as `plies < min_plies`; and carry
`move_evidence` only for `checkmate` and `clock_evidence` for `time_forfeit`.
Proofs are ordered by `proof_id`, which makes the body canonical.

The exception is the *heartbeat*: past the bootstrap window a block may instead
carry **zero** proofs if it carries **exactly one** transaction, and such a block
mints no subsidy (it still collects its transaction's fee). Without it a chain
can deadlock -- settled proofs are spent once, a small roster plays fewer games
than an epoch has blocks, and if no block may be added then the availability and
reveal transactions that rebuild the roster can never be mined, so no proof can
ever exist again. The exemption cannot be farmed because skipping `k` proofs
always costs more than it saves, and a heartbeat earns seal work only, never the
`k * 65536` chess term (section 12.1), so a branch of proofless blocks cannot
outrun a branch of equal length that carries proofs. Exactly one transaction is
a consensus rule, not a builder policy: a block is also the chain's clock, and an
epoch of `epoch_length` blocks can only end if the ledger supplies that many
blocks' worth of work. A miner that folded the whole pool into one proofless
block would spend that work while producing one block, re-freezing the chain at
the next boundary; the validator rejects such a block.

The same reasoning bounds the transactions a *proof* block carries. A proof
block is one of the epoch's blocks without spending any ledger work, so the
reference builder keeps one pending transaction in reserve for every block the
epoch has left and adds only the surplus (the epoch's last block flushes what
remains, still bounded by `max_block_bytes`). Packing
a whole pool into one proof block spends an epoch's work without buying the
epoch's blocks; if the pool then runs dry before the epoch's last block, the
next epoch never opens, the proofs that only settle from that boundary are never
selectable, and the chain stops for good. During the bootstrap window, where no
proof is required, the builder carries the whole pool, because the deploy
scripts seed those blocks offline one scripted height at a time.

### 12.3 Order of block checks

Header shape and network, height and parent hash, timestamp, epoch and beacon and
roster root, declared difficulty, seal, body size and tx ids and merkle root,
coinbase maturity and transactions, proofs (the expensive part), coinbase value
(`subsidy + fees` exactly), and finally `state_root` recomputed from the state
the block actually produces. A block that disagrees only on `state_root` is
invalid, not "almost valid": that root is what a light client trusts.

### 12.4 Fork choice and reorgs

The heaviest chain wins, ties broken by the numerically lower block hash. A newly
stored block that is heavier than the current tip triggers a reorg: the fork
point is the highest common ancestor, blocks above it leave the main chain and
the candidate branch is adopted, and the orphaned transactions and proofs are
returned to the mempool. Storage keeps canonical block JSON, the adopted tip and
periodic snapshots, and replays from the last snapshot on restart instead of
trusting it.

## 13. Genesis documents

Genesis is the only premine. Its block hash is pinned in `NetworkParams`
(`genesis_hash`), and a pinned network refuses a document that hashes to
anything else, so two nodes cannot disagree about height 0.

```json
{"version": 1,
 "network": "testnet",
 "timestamp": 1700000000,
 "miner": "sccpt1...",
 "allocations": [{"address": "sccpt1...", "public_key": "hex", "amount": 5000000000}],
 "hash": "64 hex chars"}
```

The `hash` shown above is illustrative; a document may omit it, and a pinned
network always checks the computed hash against `NetworkParams.genesis_hash`.

`version` must be `1` and `network` must name the parameter set. `miner` is
optional and defaults to the first allocation. `allocations` may be empty
(mainnet has no premine); each entry needs a positive integer `amount` in base
units, a valid address for this network, and a `public_key` that **derives** that
address — Ed25519 is not key-recoverable, so an allocation without a matching key
would be unspendable. Duplicate addresses are rejected. `hash` is optional and,
when present, must equal the block hash the document builds; `scripts/make_genesis.py`
writes it.

Genesis state is the allocation accounts plus the initial difficulty, height 0,
the genesis timestamp as the first median-time-past sample, an open epoch 0 record
and an empty body. `genesis/devnet.json` is the devnet chain used by the test
suite; `genesis/testnet.json` and `genesis/mainnet.json` are the pinned networks.
Test-network faucet keys come from published seeds, which is why those networks
are safe to hand out and mainnet must never gain one.

## 14. Node API and gossip

### 14.1 HTTP API

Every response is a frozen pydantic model with `extra="forbid"` and carries the
network id where the plan asks for it, so a wallet can refuse a node on the
wrong network. Errors map as: `NotFoundError` → 404, any other `SccpError` → 400,
an orphan block on `POST /v1/blocks` → 202.

| Method | Path | Response |
| --- | --- | --- |
| GET | `/v1/status` | `NodeStatus`: network, protocol version, height, tip, genesis, peers, syncing, mempool sizes |
| GET | `/v1/params` | the network parameters |
| GET | `/v1/blocks/{height-or-hash}` | `BlockView`: network, hash, parent, height, work, main-chain flag, block |
| POST | `/v1/blocks` | `BlockView`, or 202 when the parent is unknown |
| GET | `/v1/accounts/{address}` | `AccountView`: network, balance, immature, bonded, nonce, registration, endpoint, engine, key, ban height |
| GET | `/v1/epochs/{epoch}` | `EpochView`: beacon, roster, availability, quotas, matchable flag |
| GET | `/v1/epochs/{epoch}/beacon` | `Beacon` (404 when the chain has none) |
| GET | `/v1/difficulty` | `DifficultyView`: network, current `wins_required`/`seal_bits` and retarget history |
| GET | `/v1/games/{game_hash}` | `GameView`: proof/match ids, winner, height, mempool flag |
| GET | `/v1/games/{game_hash}/pgn` | `GamePgn`: the move text the proof for that game carries (404 when this node holds no block body for it) |
| GET | `/v1/matches/{match_id}` | `MatchInfo`: epoch, round, seats, certificate, transcript |
| GET | `/v1/matches/{match_id}/transcript` | `Transcript` |
| GET | `/v1/mempool` | `MempoolView`: pending txids and proof ids |
| GET | `/v1/transactions/{txid}` | `TransactionRecord`: type, height, block hash, mempool flag |
| POST | `/v1/tx` | `TxAccepted`: the recomputed txid |
| POST | `/v1/proofs` | `ProofAccepted`: the proof id |
| GET | `/v1/challenges` | `ChallengeList`: open challenges by default, resolved ones with `?include_resolved=true` |
| GET | `/v1/challenges/{challenge_id}` | `ChallengeView`: one resolved or open challenge |
| POST | `/v1/gossip` | `GossipResult`: object id, duplicate flag, fanout |
| GET | `/v1/peers` | `PeerView[]`: network, url, address, height, failures, ban and last-seen times |

`POST /v1/tx` and `POST /v1/proofs` run the same checks a block would, so an
invalid object never reaches the mempool; a duplicate `(sender, nonce)` or proof
id is rejected instead of replacing the pending one. `POST /v1/blocks` is
idempotent for a block the node already has and returns 202 for an unknown
parent so a peer can retry once it receives the ancestor.

The generated OpenAPI schema is part of the contract: prose and the package
version are stripped and the rest is snapshotted in
`tests/fixtures/openapi.json`, so a route, field or status-code change fails CI
until `scripts/make_openapi_fixture.py` is run deliberately.

### 14.2 Gossip envelopes

```json
{"network": "testnet",
 "kind": "transaction" | "proof" | "block" | "peer",
 "payload": {"transaction": {...}},
 "sender": "sccpt1...",
 "signature": "128 hex chars"}
```

The signature covers the canonical JSON of
`{"network", "kind", "payload", "sender"}` in the `sccp/v1/gossip` domain, so an
envelope cannot be replayed on another network or relabelled as another kind.
`verify_envelope` checks the network id and the signature whenever the sender's
public key is known — passed by the caller or registered in the ledger. An
unknown sender may still relay blocks, proofs and transactions because those
carry their own signatures and are revalidated by the ledger; its identity only
feeds the abuse score.

Object ids are recomputed from content, never taken from the sender: the txid,
the proof id, the block hash, or the peer URL. The dedupe cache is a bounded,
insertion-ordered set of `(kind, object_id)` pairs (4096 entries; the oldest is
evicted first). A duplicate is acknowledged with `duplicate: true` and is not
applied again. Fanout picks up to four eligible peers at random and never
includes the peer the envelope arrived from.

### 14.3 Peer scoring and pull-sync

A peer starts with zero failures. Each failed sync or malformed block response
adds one; three consecutive failures ban the peer for 300 seconds, and any
successful response clears the score. Unknown-parent blocks are *not* failures:
gossip order is not a promise.

Pull-sync asks the peer for `/v1/status`, refuses a peer on another network,
then requests heights `local_height + 1 .. min(remote_height, local_height +
max_sync_blocks)` in strict order. Every block is validated and ingested through
the same path as a gossiped block, so a reorg is adopted only when the peer's
branch is heavier. A malformed document, a wrong height or a ledger error aborts
the sync, penalises the peer and leaves the local chain untouched beyond the
blocks already accepted. A block that cannot be connected (its parent is still
missing) is counted as `orphaned` and retried by a later sync.

`sccp node run --peer URL` runs this once per URL before serving, through
`sccp.node.service.sync_peers`. A peer that cannot be reached at all is recorded
as one failure and skipped: startup must not depend on a peer being up, and the
peer table bans an address that keeps failing.

## 15. Devnet parameters and policy

`devnet` is the in-process network used by tests, CI and `sccp devnet up`. It
inherits testnet and changes only what determinism needs:

| Parameter | Devnet value | Reason |
| --- | --- | --- |
| `epoch_length` | 1 | one epoch per block, so a ten-block run exercises several epochs |
| `target_block_seconds` | 1 | no retargeting pressure |
| `retarget_enabled` | false | fixed difficulty |
| `wins_required` | 1 | one proof per block |
| `min_bond` | 0 | faucet funding is enough to register |
| `genesis_timestamp` | 1700000000 | fixed median-time-past start |
| `genesis_hash` | pinned in `NetworkParams` | one chain for every run |

Consensus-defined bootstrap lets the first three blocks be empty, which is
exactly the window funding, registration and availability commitments need; the
first matchable epoch is therefore 3. Availability for epoch `e` must be
committed by epoch `e - 2` and revealed in epoch `e - 1`, so a devnet keeps the
epochs rolling by submitting both one block ahead.

A devnet run is deterministic: participant keys come from fixed seeds, engines
are seeded `MockEngine`s with fixed fingerprints, availability nonces are fixed
strings, and the relay's clock and its monotonic millisecond clock are both
pinned to fixed values, so no clock ever runs out and every accepted move costs
zero time. Two runs with the same arguments produce the same tip hash and the
same proof ids. Because two seeded engines can shuffle past the ply ceiling, the
devnet resigns the side to move once a game reaches 60 plies; resignation is a
consensus-valid termination, both seats co-sign it, and the winner's proof is
verified like any other. The same policy is available to a live miner with
`sccp miner run --resign-after-plies 60` (off by default), which is what keeps a
testnet of equal-strength engines from drawing every game in an epoch.

## 16. Change control

- Adding a header tag, changing a hash payload key, changing result or
  termination semantics, or changing selection all require a protocol version
  bump (`sccp/v2`) and a golden-vector update.
- Golden vectors for the game hash and opening hash live in
  `tests/fixtures/chess_vectors.json`; changing them is a deliberate consensus
  change, never a test fix.
- The gossip envelope fields, the signed payload and the documented HTTP paths
  and response fields are part of this document: adding a field is a compatible
  change only if clients ignore unknown fields, and anything else needs a
  protocol version bump.

