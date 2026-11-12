# sccp wire protocol, version 1 (M1 subset)

Status: **normative** for the parts implemented in milestones M0 and M1
(canonical encoding, game hash, PGN contract, opening book, engine
fingerprints). Sections for matchmaking, transactions, blocks and proofs land in
later milestones.

Reference implementation: `src/sccp/chess/` and `src/sccp/serialization.py`.
The plan that motivates these rules is
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md), sections 5.2, 5.6 and 5.8.

---

## 1. Conventions

- **Hash function.** `H(domain, payload) = SHA-256(domain.encode("utf-8") ||
  0x00 || canonical_json(payload))`, where `canonical_json` is sorted-key,
  compact, UTF-8, NFC-normalized JSON with no floats and integers in
  `[-2**63, 2**64 - 1]`. `src/sccp/serialization.py` is the only encoder.
- **Domains.** Every domain starts with `sccp/v1/`. The chess domains are
  `sccp/v1/game` and `sccp/v1/opening`; M2 adds `certificate`, `match`,
  `result` and the matchmaking domains.
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

## 6. Change control

- Adding a header tag, changing a hash payload key, changing result or
  termination semantics, or changing selection all require a protocol version
  bump (`sccp/v2`) and a golden-vector update.
- Golden vectors for the game hash and opening hash live in
  `tests/fixtures/chess_vectors.json`; changing them is a deliberate consensus
  change, never a test fix.

