# sccp wire protocol, version 1 (M0–M2 subset)

Status: **normative** for the parts implemented in milestones M0, M1 and M2
(canonical encoding, game hash, PGN contract, opening book, engine fingerprints,
epoch beacons, pairing, certificates, transcripts and win-proof verification).
Sections for transactions, blocks, proofs-on-chain and the node API land in later
milestones.

Reference implementation: `src/sccp/protocol/`, `src/sccp/chess/` and
`src/sccp/serialization.py`. The plan that motivates these rules is
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md), sections 5.2–5.10.

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
   pairing and engine metadata; transcript hashes are a separate construction
   (M2).

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
| `Termination` | `checkmate`, `draw` or `resignation` |
| `MatchID` | Match identifier from the certificate |
| `Epoch`, `RoundNumber` | Non-negative integers; must equal the certificate |
| `SeatWhite`, `SeatBlack` | Bech32 addresses; must equal the certificate |
| `EngineWhite`, `EngineBlack` | Engine fingerprints; must equal the certificate |
| `OpeningID`, `OpeningHash` | Book line id and its opening hash |
| `Rules` | Exactly `chess` |

Every tag must be present and non-empty. When a certificate (M2) is supplied,
`MatchID`, `Epoch`, `RoundNumber`, `SeatWhite`, `SeatBlack`, `EngineWhite`,
`EngineBlack`, `OpeningID` and `OpeningHash` are compared exactly; any mismatch
is a validation error.

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
| No automatic outcome | decisive | `resignation` |
| Anything else (time forfeit, abandoned, unterminated) | rejected | rejected |

**M2 limitation.** There is no chess clock in the implemented protocol yet, so a
time forfeit cannot be represented. The planned two-clock time control and
bonded timeout settlement are specified in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) section 5.7.1; this table will
gain `time_forfeit` when that milestone lands.

Notes:

- `claim_draw=False` deliberately excludes claimable 50-move and threefold
  draws. A game stopped by one of those is only valid as a resignation.
- For a resignation the side to move is the side that resigned, so a White
  resignation is `0-1`.
- A resignation is additionally subject to the quality policy (M5) through
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
`DrandVerifier` rather than pulling in a second crypto stack; M4 supplies the
verifier pinned to the quicknet chain key. A verifier that has not been wired
rejects `drand_used = 1` instead of accepting it blindly.

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
 "start_signatures": {"white": "hex", "black": "hex"}}
```

Both seats sign this payload with `H("sccp/v1/certificate", …)`:

```json
{"network": "...", "match_id": "...", "epoch": 3, "round": 0,
 "seats": ["<sorted seat A>", "<sorted seat B>"],
 "colors": {"white": "<address>", "black": "<address>"},
 "opening_hash": "hex",
 "engine_fingerprints": {"white": "...", "black": "..."}}
```

`start_signatures` is excluded from the payload. A certificate is valid only
when both signatures verify against the addresses' registered Ed25519 keys, the
colour draw matches the beacon, and `match_id` recomputes from the seats.

The planned clock extension adds `time_control = {initial_ms, increment_ms}` to
the certificate and its signed payload, and adds `time_forfeit` to
`termination`; M2 emits neither. See `IMPLEMENTATION_PLAN.md` section 5.7.1.

### 8.2 Result attestation

When the board is terminal or a seat resigns, both seats sign:

```json
{"network": "...", "match_id": "...", "game_hash": "hex",
 "result": "1-0" | "0-1" | "1/2-1/2",
 "termination": "checkmate" | "draw" | "resignation",
 "plies": 87,
 "result_signatures": {"white": "hex", "black": "hex"}}
```

The signature domain is `sccp/v1/result`; `result_signatures` is excluded from
the signed payload. `game_hash` is the canonical game hash from section 2, so the
two seats also attest the exact move list.

In the planned clock protocol a checkmate may be proven without the loser's
result signature: the winner supplies its own signature plus the signed move
messages, and the verifier replays them to checkmate. Resignation and agreed
draws still need both signatures. M2 has no signed move messages and keeps the
both-signatures rule.

### 8.3 PGN assembly

A transcript PGN is the game plus the required tags of section 3.1, with
`White`/`Black` = seat addresses, `MatchID`, `Epoch`, `RoundNumber`,
`SeatWhite`, `SeatBlack`, `EngineWhite`, `EngineBlack`, `OpeningID`,
`OpeningHash` and `Rules = chess`. `Termination` and `Result` come from the
result attestation. The opening prefix is included in the move list.

### 8.4 Relay API

The relay is untrusted. It derives the round from public data and stores
signatures; it never signs a result. In M2 it also does not enforce a chess
clock: `timeout_seconds` only abandons a silent session, and that is not
evidence of a loss. The planned clock receipts, signed move messages and bonded
timeout claim are described in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) section 5.7.1.

| Method | Path | Body |
| --- | --- | --- |
| POST | `/v1/rounds` | `{"round": 0}` — derive and create that round's sessions |
| GET | `/v1/matches` | all sessions |
| GET | `/v1/matches/{id}` | one session: certificate, SAN list, result, PGN |
| GET | `/v1/matches/{id}/transcript` | co-signed transcript, 404 until both signs |
| POST | `/v1/matches/{id}/join` | `{"seat", "public_key", "signature"}` |
| POST | `/v1/matches/{id}/move` | `{"seat", "san"}` |
| POST | `/v1/matches/{id}/resign` | `{"seat"}` |
| POST | `/v1/matches/{id}/result` | `{"seat", "signature"}` |
| GET | `/v1/status`, `/v1/reputation` | relay status and no-show counts |

The M2 move body is exactly `{seat, san}`; the relay verifies the turn and SAN
legality, but the move carries no ply or signature. The planned clocked move
body is `{seat, ply, san, signature}` signed with `sccp/v1/move`; moves remain
off-chain and only the final proof (or timeout claim) reaches a block. Because
each move is signed, a checkmate can later be proven without the loser's result
signature. See `IMPLEMENTATION_PLAN.md` section 5.7.1.

Illegal moves, wrong turns, duplicate joins and bad signatures are rejected with
HTTP 400 and never change the session. A relay may abandon a silent session after
its timeout; that is a liveness choice, not evidence. Restart-safe state is a
JSON list of sessions (certificate, SAN list, result, signatures); pending
sessions replay their SAN list on load.

`sccp matchmaker run --config relay.json` reads:

```json
{"network": "testnet",
 "beacon": {"network": "testnet", "epoch": 3, "hash": "hex", "block_hash": "hex",
             "reveals": [...], "drand_used": false},
 "roster": {"sccpt1...": {"public_key": "hex", "engine": "sha256-or-mock-id",
                           "max_games": 3}},
 "timeout_seconds": 300,
 "open_round": 0}
```

`engine` is the fingerprint the seat committed to at registration; `open_round`
is optional and opens one round at startup.

## 9. Win proofs and offline verification

A proof wraps the co-signed transcript:

```json
{"network": "testnet", "proof_id": "hex", "game_hash": "hex",
 "match_id": "hex", "epoch": 3, "round": 0,
 "winner": "sccpt1...", "loser": "sccpt1...", "winner_seat": "white",
 "result": "1-0", "termination": "checkmate", "plies": 87,
 "opening": {...}, "pgn": "[Event ...] ...",
 "certificate": {...}, "result_signatures": {"white": "hex", "black": "hex"},
 "quality_summary": null}
```

`proof_id = H("sccp/v1/proof", {game_hash, match_id, winner})`. The optional
`quality_summary` belongs to M5 and is ignored by structural verification.

Verification runs cheap checks first and stops at the first typed failure:

| Reason | Check |
| --- | --- |
| `network` | proof, certificate and beacon all name the verifier's network |
| `beacon` | epoch matches and `B_e` recomputes from reveals/availability/drand |
| `certificate` | match id, epoch, round, seats, winner seat, colour draw |
| `opening` | line hash, canonical SAN, book version and beacon-selected line |
| `roster` | both seats are in the verifier's roster |
| `pairing` | the matchup is exactly what the beacon and roster produce |
| `signature` | both start and both result signatures verify and cover these fields |
| `pgn` | section 5.8 replay, headers and opening prefix |
| `game-hash` | the replayed SAN list hashes to `game_hash` |
| `result` | result, termination, plies and winner seat match the replay |
| `proof-id` | `proof_id` recomputes from its fields |
| `duplicate` | neither hash has already been seen by the caller |

A proof that passes is structurally and cryptographically valid. It says nothing
about how well the moves were played; quality sampling and challenges are M5.

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
offline verification until M4 wires the pinned verifier.

## 10. Change control

- Adding a header tag, changing a hash payload key, changing result or
  termination semantics, or changing selection all require a protocol version
  bump (`sccp/v2`) and a golden-vector update.
- Golden vectors for the game hash and opening hash live in
  `tests/fixtures/chess_vectors.json`; changing them is a deliberate consensus
  change, never a test fix.

