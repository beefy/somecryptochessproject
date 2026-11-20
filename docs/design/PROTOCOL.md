# sccp wire protocol, version 1

Status: **normative** for protocol v1 (canonical encoding, game hash, PGN
contract, opening book, engine fingerprints, epoch beacons, pairing,
certificates, transcripts, clocks and clock evidence, stream commitments,
win-proof verification, the ledger — transaction envelopes, account state, block
headers and bodies, difficulty, fork choice and genesis documents — and the node:
HTTP API, gossip envelopes, peer scoring
and pull-sync). The clock layer covers the off-chain primitives *and* on-chain
settlement.

There is exactly **one signature scheme**: hybrid Ed25519 + ML-DSA-65. Every
address, account, transaction, certificate, result and piece of evidence is
post-quantum by construction; nothing in this document describes a classical
fallback, because there is none (section 10.5 lists what was removed and the
grep-level invariants that keep it removed).

Reference implementation: `src/sccp/protocol/`, `src/sccp/chess/`,
`src/sccp/chain/`, `src/sccp/node/`, `src/sccp/miner/`, `src/sccp/devnet.py` and
`src/sccp/serialization.py`. This document is the normative source for these
rules; forward work is tracked in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

---

## 1. Conventions

- **Hash function.** `H(domain, payload) = SHA-256(domain.encode("utf-8") ||
  0x00 || canonical_json(payload))`, where `canonical_json` is sorted-key,
  compact, UTF-8, NFC-normalized JSON with no floats and integers in
  `[-2**63, 2**64 - 1]`. A string carrying a surrogate code point is rejected
  rather than encoded: UTF-8 has no representation for one, so no such string
  can have a canonical form. `src/sccp/serialization.py` is the only encoder.
  The beacon and pairing-score constructions in sections 6 and 7 are byte-level
  concatenations instead and specify their own input encoding. Section 8.5
  defines one more non-`H` construction, the stream fold, byte for byte.
- **Domains.** Every domain starts with `sccp/v1/`. Chess uses `game` and
  `opening`; matchmaking uses `beacon`, `beacon-commit`, `pair`, `match`,
  `certificate`, `result` and `proof`; evidence uses `commitment`.
- **Digests.** Lowercase hex, 32 bytes (64 characters).
- **Signatures.** Two encodings, one scheme, and **no union type at runtime**:

  | Role | Encoding | Hex length | Where |
  | --- | --- | --- | --- |
  | ledger, identity, commitments | hybrid `0x01 \|\| ed25519_sig(64) \|\| mldsa65_sig(3309)` | 6748 | transactions, certificates, results, stream checkpoints |
  | live streams | Ed25519 `64` bytes | 128 | move messages, clock receipts, gossip envelopes |

  The stream signatures are the *inner half* of the same key and are anchored by
  a hybrid signature over a commitment (section 8.5), so no claim ever rests on
  Ed25519 alone. A verifier chooses the check from the field it is reading, never
  from a length or from unauthenticated data.
- **Ints, never floats.** All numeric protocol values are integers. No
  wall-clock value enters a game hash.
- **Network binding.** `network_id` (`testnet`, `mainnet`, `devnet`) is part of
  every game hash and every address hash, so a testnet game can never be
  replayed as a mainnet one.
- **No encryption.** Nothing in this protocol encrypts anything. Addresses,
  move streams, receipts and proofs are public data by design, so there is no
  key-exchange message, no ML-KEM and no ciphertext on the wire. Keyfiles are the
  one place a secret exists at rest, and they use scrypt + AES-256-GCM locally
  (`src/sccp/crypto/wallet.py`), not a network protocol.

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
4. `start_fen` is `rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1`
   for standard chess. A Chess960 game uses one of the 960 legal positions in
   Shredder-FEN, carries `SetUp "1"` and `Variant "Chess960"`, and adds
   `"variant": "chess960"` to the hash payload. A standard PGN carrying any
   other `FEN` (or an ambiguous `KQkq` Chess960 FEN) is rejected.
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
| `Rules` | `chess` for standard, `chess960` for Fischer Random |
| `FEN`, `SetUp`, `Variant` | Optional for standard; required for Chess960, with `SetUp "1"` and `Variant "Chess960"` |

Every tag must be present and non-empty. When a certificate is supplied,
`MatchID`, `Epoch`, `RoundNumber`, `SeatWhite`, `SeatBlack`, `EngineWhite`,
`EngineBlack`, `OpeningID`, `OpeningHash` and `TimeControl` are compared
exactly; any mismatch is a validation error. The `TimeControl` form is strict:
decimal seconds with no underscores or whitespace, initial time at least one
second, and the worst case `initial_ms + increment_ms * max_plies` may not exceed
`MAX_CLOCK_MS` (24 hours).

### 3.2 Replay

- Replay starts from the standard position, or from the certificate's Shredder
  FEN for Chess960 with `Board(fen, chess960=True)`. Every mainline move must
  satisfy `Board.is_legal(move)` **before** it is pushed; `Board.push()` does not
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
fixed 10+0 control for every match; matchmaking does not offer per-match
choices. See section 8.2 below and the clock implementation in
`src/sccp/protocol/clocks.py`.

Notes:

- `claim_draw=False` deliberately excludes claimable 50-move and threefold
  draws. A game stopped by one of those is only valid as a resignation.
- For a resignation the side to move is the side that resigned, so a White
  resignation is `0-1`.
- A resignation may be checked by a caller through
  `validate_pgn(..., resignation_check=...)`, which must confirm the position is
  lost. The node does not supply that check: judging a position is not a
  consensus rule, and a resignation is accepted as written (section 9.2).
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
 "time_control": {"initial_ms": 600000, "increment_ms": 0},
 "start_signatures": {"white": "hex", "black": "hex"}}
```

Both seats sign this payload with `H("sccp/v1/certificate", …)`:

```json
{"network": "...", "match_id": "...", "epoch": 3, "round": 0,
 "seats": ["<sorted seat A>", "<sorted seat B>"],
 "colors": {"white": "<address>", "black": "<address>"},
 "opening_hash": "hex",
 "time_control": {"initial_ms": 600000, "increment_ms": 0},
 "engine_fingerprints": {"white": "...", "black": "..."}}
```

`start_signatures` is excluded from the payload. Both signatures are **hybrid**
(section 10.5): a certificate is valid only when both verify against the
addresses' registered 1985-byte hybrid keys, the colour draw matches the beacon,
and `match_id` recomputes from the seats. Signing the certificate is what binds
each seat's ML-DSA-65 key to the match, so a quantum attacker cannot impersonate
a player for the moves that follow.

The certificate commits to `time_control = {initial_ms, increment_ms}` and the
signed payload covers it. Every match uses one protocol-fixed **10+0** control
(`initial_ms = 600_000`, `increment_ms = 0`); matchmaking never offers or
accepts a per-match choice. See section 8.4 for the timed-session API.

### 8.2 Result attestation

When the board is terminal or a seat resigns, both seats sign:

```json
{"network": "...", "match_id": "...", "game_hash": "hex",
 "result": "1-0" | "0-1" | "1/2-1/2",
 "termination": "checkmate" | "draw" | "resignation" | "time_forfeit",
 "plies": 87,
 "move_commitment": "hex",
 "result_signatures": {"white": "hex", "black": "hex"}}
```

The signature domain is `sccp/v1/result`; `result_signatures` is excluded from
the signed payload. `game_hash` is the canonical game hash from section 2, so the
two seats also attest the exact move list. `move_commitment` is the folded root
of the post-opening move stream (section 8.5): because it is *inside* the signed
payload, one hybrid result signature covers every ply as well as the score.

A checkmate may be proven without the loser's result signature: the winner
supplies its own signature plus a `move_evidence` bundle — the committed plies
and a witness checkpoint over their root (section 8.5) — and the verifier
replays them to checkmate. A time forfeit likewise needs only the winner's
signature when the proof carries the `timeout_claim_id` and complete
`clock_evidence`; the evidence is a bounded receipt window plus a witness
checkpoint over the window and the flag. Resignation and agreed draws still need
both signatures. Exactly one missing result signature is tolerated, and the
winner must be the seat that signed. The offline verifier checks the actual
signature bytes against the registered keys.

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
the fixed **10+0** control (`TIMED_SESSIONS` in `sccp.miner.matchmaker`), charged
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
  flagged clock at zero). A witness signs each receipt with the Ed25519 half of
  its key — one cheap signature per ply — and additionally hybrid-signs a
  **clock checkpoint** over the receipt window and the flag, which is what makes
  a forged receipt useless as evidence (section 8.5). A relay without a witness
  key still runs the clock and returns receipts with no signatures and a `null`
  checkpoint: it observes without attesting, and a claim built from its bundle
  will fail the quorum check.
- A live clocked session is never abandoned by `timeout_seconds`: that reaper
  only applies to pending joins, because abandoning a clocked game would let the
  losing seat escape the flag.
- After a restart the relay re-bases every live clock to "now" for the side to
  move. Monotonic timestamps from the previous process are meaningless here, and
  flagging on them would punish a seat for the relay's downtime.

| Method | Path | Body |
| --- | --- | --- |
| POST | `/v1/rounds` | `{"round": 0}` — derive and create that round's sessions |
| GET | `/v1/matches` | all sessions, with live clocks; repeat `?status=pending&status=active` to select open states |
| GET | `/v1/matches/{id}` | one session: certificate, SAN list, result, PGN, clocks |
| GET | `/v1/matches/{id}/receipts` | the `ClockBundle`: time control, clocks, every receipt, the flag, the clock checkpoint |
| GET | `/v1/matches/{id}/transcript` | co-signed transcript, 404 until both signs |
| POST | `/v1/matches/{id}/join` | `{"seat", "public_key", "signature"}` |
| POST | `/v1/matches/{id}/move` | `{"seat", "san"}`, or the signed form below |
| POST | `/v1/matches/{id}/resign` | `{"seat"}` |
| POST | `/v1/matches/{id}/result` | `{"seat", "signature"}` |
| GET | `/v1/status`, `/v1/reputation` | relay status (timed fields, round, witness) and no-show counts |

`MatchView` carries `time_control`, `white_ms`, `black_ms`, `turn`, `last_ply`,
`flagged` and `receipts` (a count; fetch the bundle for the objects).
`ClockBundle.receipts` is the full observed stream; `ClockBundle.flag` is the
final flag receipt and `ClockBundle.checkpoint` is the relay's hybrid clock
checkpoint over the evidence window, or `null` when it has no witness key. A
`TimeoutClaim` carries `time_control`, `final`, a *suffix* of the receipts
(section 8.5) and the checkpoint.

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
[--round-seconds 900] [--sweep-seconds 1] [--allow-unwitnessed-rounds]` reads:

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
 "open_round": 0,
 "require_witnesses": true}
```

`engine` is the fingerprint the seat committed to at registration; `open_round`
is optional and opens one round at startup. `round_seconds` (or
`--round-seconds`) is relay policy, not consensus: while the relay serves, a
background loop sweeps the clocks and opens the next round once the interval has
passed, up to `rounds_per_epoch`. `--witness-key` makes the relay sign its
receipts. A miner given the matching `--relay-witness-key` (the witness's public
key in hex) verifies those receipts locally before it builds a `timeout_claim`;
without it the claim is refused rather than guessed at.

`require_witnesses` (default `true`) is also relay policy: a relay started with
`--node` reads the node's `/v1/status` on every poll, and while the epoch's
active clock-witness count is *known* to be below the network's
`clock_witness_quorum` it refuses to open a round. A match played below the
quorum can produce a flag that no `timeout_claim` can ever settle (step 1 of
section 10.4 needs `clock_witness_quorum` witness signatures), so the seat that
would lose on time gains by stalling — refusing to pair is the honest answer,
and play resumes when coverage returns. An unknown count is not zero: nodes that
predate the field, and relays that are not following a node, do not gate.
`--allow-unwitnessed-rounds` (or `"require_witnesses": false`) is the devnet
escape hatch, where no witness service runs at all.

### 8.5 Stream commitments

Live play cannot afford a 3374-byte signature per ply, and evidence cannot afford
2 MB per game. Both problems have the same answer: sign the *stream* once.

A seat signs each move message with the Ed25519 half of its key (section 8.4), and
a witness signs each clock receipt the same way. Those signatures are cheap
authentication for a live peer. The **claim** is different: both seats (or the
witness quorum, when a seat refuses) fold the whole stream into one 32-byte root
and hybrid-sign that root exactly once.

**The fold.** Leaves are folded in order with a single SHA-256 chain. This is the
one construction in the protocol that is not `H(domain, payload)`, so it is
defined byte for byte:

```text
root_0     = 00 * 32
root_{i+1} = SHA-256("sccp/v1/commitment" || 0x00 || root_i || canonical_json(leaf_i))
```

`canonical_json` is the encoder of section 1, the domain string is UTF-8, and
`root_i` is the raw 32-byte digest (not its hex). An empty stream folds to
`00 * 32`. A browser needs only `sha256` and the canonical encoder to reproduce
this, which is why the web library ships it (`src/web/src/lib/commitment.ts`)
even though it cannot sign.

**Move leaves.** A move leaf is

```json
{"network": "...", "match_id": "hex", "seat": "<address>", "ply": 12, "san": "Nf6"}
```

with `seat` fixed by the ply (`white` on even plies) and `ply` the zero-based SAN
index in the *full* game. The committed stream is the post-opening plies only:
`ply >= len(certificate.opening.moves)`.

`move_commitment` in a game result (section 8.2) is the fold of exactly those
leaves for the whole game. The relay folds the same leaves as it accepts moves, so
its checkpoint and a result's commitment are the same 32 bytes.

**Checkpoints.** A `StreamCheckpoint` is

```json
{"network": "...", "kind": "moves" | "clock", "match_id": "hex",
 "ply": 87, "root": "hex",
 "witnesses": [{"witness": "<address>", "signature": "6748-hex"}, ...]}
```

Each witness hybrid-signs `H("sccp/v1/commitment", {network, kind, match_id, ply,
root})`. `ply` is the number of plies the root commits to, so a checkpoint also
pins the game's length. Verification is: the checkpoint names this network and
match, `ply` matches the evidence, `root` recomputes from the evidence, and at
least `clock_witness_quorum` *distinct registered* witnesses produced a valid
hybrid signature over the payload.

**Move evidence.** A refused-checkmate proof carries

```json
{"moves": [{"ply": 4, "seat": "sccpt1...", "san": "Qh5"}, ...],
 "checkpoint": {"kind": "moves", "ply": 7, "root": "hex", "witnesses": [...]}}
```

`moves` covers every post-opening ply exactly once, in order, each authored by
the seat whose turn it was. There are **no per-ply signatures**: with the
checkpoint in place they would be 2,000-odd redundant hex characters per ply.
The verifier replays `san` values, folds the leaves, compares `root`, then
verifies the witness quorum. A quantum attacker who can forge the loser's Ed25519
move signatures still cannot produce a root covered by an honest witness's
ML-DSA-65 signature, which is exactly the gap this section closes.

**Clock evidence.** A flag bundle carries

```json
{"time_control": {...},
 "final": {"kind": "flag", "ply": 400, "seat": "<flagged>", ...},
 "receipts": [<MoveReceipt>, ...],
 "checkpoint": {"kind": "clock", "ply": 400, "root": "hex", "witnesses": [...]}}
```

`receipts` is a **window**, not the whole game: at most
`CLOCK_EVIDENCE_PLIES = 64` consecutive receipts ending at the flag, and at least
one whenever the game has a post-opening move. The first receipt in the window
carries absolute `white_ms`/`black_ms` and is the arithmetic anchor; every later
receipt is checked as a delta from it (section 8.4's charging rule), so a suffix
is sufficient evidence and a 400-ply game no longer overflows the transaction
cap. `checkpoint.root` is the fold of the window followed by the flag receipt
leaf, and the same witness quorum hybrid-signs it.

Receipt leaves use the exact payload the witness signs:

```json
{"network": "...", "kind": "move" | "flag", "match_id": "hex", "ply": 12,
 "seat": "<address>", "san": "Nf6", "received_at_ms": 1700000000000,
 "white_ms": 590000, "black_ms": 600000}
```

(`san` is absent on a flag leaf.) The chain-level `timeout_claim` validator
recomputes `checkpoint.root` and verifies the quorum before it opens a bonded
record, so a forged flag receipt cannot start a claim even if every per-ply
Ed25519 signature on it is fake.

**Why this is enough.** Every value that moves money or reputation is covered by
a hybrid signature over a commitment to the data it depends on:

| Claim | Anchored by |
| --- | --- |
| this game was played by these two keys | hybrid certificate signatures (8.1) |
| this is the game that was played | `move_commitment` inside the hybrid result payload (8.2, 8.5) |
| the loser refused, but these are the moves | witness-quorum hybrid move checkpoint (8.5) |
| this seat's clock reached zero | witness-quorum hybrid clock checkpoint + window (8.5) |

---

## 9. Win proofs and offline verification

A proof wraps the co-signed transcript:

```json
{"network": "testnet", "proof_id": "hex", "game_hash": "hex",
 "match_id": "hex", "epoch": 3, "round": 0,
 "winner": "sccpt1...", "loser": "sccpt1...", "winner_seat": "white",
 "result": "1-0", "termination": "checkmate", "plies": 87,
 "move_commitment": "hex", "low_ply": false,
 "opening": {...}, "pgn": "[Event ...] ...",
 "certificate": {...}, "result_signatures": {"white": "hex", "black": "hex"},
 "clock_evidence": null, "move_evidence": null, "timeout_claim_id": null}
```

`proof_id = H("sccp/v1/proof", {game_hash, match_id, winner})`.
`clock_evidence` is required for every `time_forfeit` and carries the time
control, a window of witness-signed move receipts, the final flag receipt and the
witness checkpoint over them (section 8.5). `timeout_claim_id` binds a
single-signature proof to its bonded timeout claim; it is null for every other
termination.
`move_evidence` is the committed post-opening move stream plus its witness
checkpoint for a single-signature `checkmate`. Both evidence fields must be absent
for other terminations.

`move_commitment` is re-derived from the replayed SAN list and must equal both the
proof's field and the value inside the signed result payload. A proof whose PGN
does not fold to that root is rejected even if every result signature verifies.

Verification runs cheap checks first and stops at the first typed failure:

| Reason | Check |
| --- | --- |
| `network` | proof, certificate and beacon all name the verifier's network |
| `beacon` | epoch matches and `B_e` recomputes from reveals/availability/drand |
| `certificate` | match id, epoch, round, seats, winner seat, colour draw |
| `opening` | line hash, canonical SAN, book version and beacon-selected line |
| `roster` | both seats are in the verifier's roster |
| `pairing` | the matchup is exactly what the beacon and roster produce |
| `signature` | both hybrid start signatures and every present hybrid result signature verify; a missing loser signature requires matching clock or move evidence |
| `pgn` | section 3 replay, headers including `TimeControl`, and opening prefix |
| `game-hash` | the replayed SAN list hashes to `game_hash` |
| `commitment` | the replayed post-opening plies fold to `move_commitment`, which is the value inside the signed result payload |
| `result` | result, termination, plies and winner seat match the replay; clock and move evidence are rooted in a witness checkpoint and checked against the replay for `time_forfeit` and `checkmate` |
| `proof-id` | `proof_id` recomputes from its fields |
| `duplicate` | neither hash has already been seen by the caller |

A proof that passes is structurally and cryptographically valid. It says nothing
about how well the moves were played, and deliberately so: section 9.2 explains
why that is not a consensus question.

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

### 9.2 Why quality is not arbitrated on chain

Protocol v1 has no quality summary, no challenge transaction and no adjudication
panel. Earlier drafts of this document described all three; they were built,
exercised on a testnet, and then removed. This section records what the layer
did, and why the useful part of it belongs off chain. It is normative only in
the negative: a proof carries no quality field, a block cannot change because of
how well a move was played, and `challenge`/`panel_vote` are not transaction
types (section 10.2). The change is wire-incompatible: a chain whose blocks
carry the old field must be rebuilt from genesis, because `WinProof` forbids
unknown fields.

**The record is already complete.** A proof carries the PGN, both result
signatures, the move commitment and — for a clock or single-signature
settlement — witness-signed clock or move evidence. Anyone can replay the game
and check every field. A `quality_summary` added nothing a verifier could not
derive itself: consensus touched it only to require its *presence* on a
"sampled" network and never inspected its contents. It was an unverified cache
with an unbounded lie surface, and the only consensus effect a false summary
could have was to create a dispute about a field that did not matter.

**Intent is not in the record.** "Lost on purpose" and "lost badly" produce the
same legal moves, the same signatures and the same PGN. Every rule of the form
"this game was too bad to count" is therefore a weakness tax: it prices honest
weak engines out of the roster without ever catching the colluding miner, who
controls both seats and can produce a decisive game at any quality threshold.

**The economics favour self-play, not fraud detection.** With `wins_required=3`
over five rounds, an epoch mints `3 * subsidy`; one miner holding both seats
collects all of it for two bonds, which is the same per-bond return as the median
honest miner, better than any below-median miner, and variance-free. A bond that
comes back when an accusation sticks, and pays no bounty, does not change that
arithmetic.

**The decidable faults were already elsewhere.** Forged signatures, illegal
moves, a PGN that does not fold to `move_commitment`, a proof from an unseated
pair, a clock claim without receipts, a duplicate proof id — all are rejected
inline by section 9, and none needs a bond, a window or a panel. That is the
whole set of claims that can be decided from the record alone.

**What the layer cost.** As shipped it had votes that were attested, never
verified (a panel member's engine was trusted, so a majority could invalidate
any proof); a panel filtered only on "bonded and not a party", with the roster's
`funder`/`subnet`/`asn` fields never populated, so every pairing-diversity rule
was inert; a challenger bond that was refunded on success, making a smear
campaign cheap; no liveness rule, so one unavailable adjudicator left a
challenge open and the challenger's bond locked indefinitely; and two different
trigger policies in one tree (the node's sampler challenged a `fail` verdict,
the testnet script challenged a summary mismatch). None of those is a bug a
better implementation would remove: they follow from asking a chain to judge
chess.

**Where a quality signal belongs.** In an off-chain indexer that reads the PGNs
the chain already stores, re-runs whatever engine it likes and publishes a label
or a rating. It may be wrong, and may disagree with another indexer, without
invalidating a block; it needs no bond, and no miner's money is at stake. If a
quality dispute ever had to become consensus, the minimum shape would be: a
fraud-only trigger on a decidable claim; the verdict re-derived on chain from
submitted counters rather than attested; a bounty paid out of the slashed bond;
a hard voting deadline with a liveness fallback; funder/subnet diversity read
from populated roster fields; one open case per challenger; and a tolerant
summary comparison. That is a different project, and this one does not need it.

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
| `signature` | 6748 hex chars | hybrid Ed25519 + ML-DSA-65 over the txid (section 10.5); `null` only for a coinbase |

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

The stored key is *always* the 1985-byte hybrid blob (3970 hex). There is one
address version, one key length and one signature length, so a validator never
infers a scheme from unauthenticated data:

| Check | Rule |
| --- | --- |
| program | `0x01` and exactly 33 bytes; anything else is not an address |
| address | `H("sccp/v1/address", {algorithm, network, public_key})` over the full 32-byte digest |
| key | exactly 1985 bytes, scheme byte `0x01` |
| key ↔ address | the blob must re-encode to `from`; a different key or address fails `signature` |
| signature | exactly 3374 bytes, scheme byte `0x01`, both halves verify over `sha256("sccp/v1/pq-hybrid" \|\| 0x00 \|\| txid)` |

Because the address hash is not truncated, a quantum adversary gets no shortcut
from Grover's algorithm beyond the generic 128-bit preimage margin, and because
the algorithm name is inside the hash, a future scheme is a new address version
rather than a guess.

The relay roster and the epoch records carry the same blob. Verifiers of the
high-volume streams (moves, receipts) project its Ed25519 half explicitly
(`sccp.crypto.pq.ed25519_public_key_hex`); that projection is a documented part of
the one scheme, not a second way to spend.

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
| `slash_evidence` | `evidence`, `proof_id`, `public_key`\* | burns an equivocating clock witness (10.4); `proof_id` is refused here |
| `create_pool` | `asset_a`?, `amount_a`, `asset_b`?, `amount_b`, `public_key`\* | creates the ordered pair's first pool; both sides positive; the omitted asset key means native SCCP; shares are `isqrt(amount_a * amount_b)` |
| `add_liquidity` | `pool_id`, `amount_a`, `amount_b`, `public_key`\* | deposits both sides; mints `min(amount_a * shares / reserve_a, amount_b * shares / reserve_b)` shares |
| `remove_liquidity` | `pool_id`, `shares`, `public_key`\* | burns the sender's shares and pays both reserves pro rata; a full redemption deletes the pool |
| `swap` | `pool_id`, `asset_in`?, `amount_in`, `min_amount_out`, `public_key`\* | moves `amount_in` into one reserve and the constant-product output out of the other; omitted `asset_in` means SCCP; rejects an output below `min_amount_out` |
| `htlc_lock` | `hashlock`, `timelock_height`, `beneficiary`, `amount`, `asset`?, `public_key`\* | moves `amount` into consensus escrow under `sha256(preimage) == hashlock`; `timelock_height > height`; omitted `asset` means native SCCP |
| `htlc_claim` | `htlc_id`, `preimage`, `public_key`\* | signed by `beneficiary`, valid only before `timelock_height`, checks the preimage, pays the escrow and records the revealed secret |
| `htlc_refund` | `htlc_id`, `public_key`\* | signed by the original locker, valid only at or after `timelock_height`; mutually exclusive with a claim |

\* optional: only needed for an account whose public key the chain does not
already know (10.1).

`htlc_id = H("sccp/v1/htlc", {from, nonce})`. A claimed or refunded record stays
in state (and in `state_root`) so an explorer and the counterparty can read the
preimage; a lock has no preimage until a claim reveals it. A native claim or
refund may pay its network fee out of the amount being released, which lets a
bridge taker who holds no SCCP claim the first leg. The hash is plain SHA-256,
not a domain hash, because the Ethereum contract checks the same 32 bytes.

The DEX's swap fee is `dex_fee_bps` (30 basis points by default). It is charged
inside the curve and stays in the input reserve, so it belongs to the pool's
liquidity providers rather than to the miner. Every DEX transaction still pays
its ordinary fee in native SCCP.

### 10.3 Coinbase

Every block above genesis carries exactly one coinbase: an *unsigned* `transfer`
from the miner to itself with `nonce = 0`, `fee = 0` and
`payload = {to: miner, amount: native_subsidy + fees}`. When the block settles a
non-standard variant, the payload also carries
`variant_rewards = {asset_id: amount}` and no native subsidy is minted for that
proof. Its authorisation is the block seal, not a signature, so it is never
applied as an ordinary transaction and consumes no nonce.
`native_subsidy = params.subsidy >> (height // halving_interval)`, clamped so
minted supply never exceeds `max_supply`; each variant has the same schedule on
its own asset. The credited amounts mature after `coinbase_maturity` blocks
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
4. **The stall slash.** The bond makes a *false* claim expensive; the stall
   slash makes a *real* stall expensive. When finalisation happens, the accused
   seat pays `min_bond * 1000 / 10000` — 10% of the protocol's miner bond, 50
   SCCP on mainnet — which is taken from its bond first and from its spendable
   balance after that, so unregistering before the claim lands does not escape
   the charge. The record remembers the charge (`slashed`), so re-finalising the
   same claim cannot debit twice, and a proof whose loser is not the claim's
   `accused` seat is refused. The proceeds split 50/30/20
   (`STALL_CLAIMANT_BPS`, `STALL_QUORUM_BPS`, `STALL_BURN_BPS`): half to the
   claimant, 30% in equal parts to the witnesses whose final flag receipt
   settled the claim, and the last 20% burned — burned by *not* crediting it, so
   a scan of the ledger sees supply shrink rather than a burn counter move. A
   seat with nothing left to take pays what it has, and the claim is still
   finalised: the slash is a cost, not a precondition.

`slash_evidence` covers clock-witness equivocation only: it carries
`evidence = {witness, receipts}` with exactly two receipts of the same
`(match_id, kind, ply)`, different contents and valid signatures from the named
witness. The witness must be registered with a non-zero bond; the bond is burned,
the witness is de-registered and `banned_until = height + unbond_delay`.
Equivocation about proofs (`proof_id` in the payload) is refused here: the only
on-chain invalidation there is a rejected proof, which block validation already
handles (section 9).

### 10.5 One post-quantum identity

A key is hybrid Ed25519 + ML-DSA-65 (NIST FIPS 204, category 3). Two encodings are
self-describing by a leading scheme byte:

```text
public key = 0x01 || ed25519_pub(32) || mldsa65_pub(1952)   = 1985 bytes
signature  = 0x01 || ed25519_sig(64) || mldsa65_sig(3309)   = 3374 bytes
```

Both halves are derived from one 32-byte master seed with HMAC-SHA-256 domain
separation (`sccp/v1/pq/ed25519` and `sccp/v1/pq/mldsa65`), so a mnemonic backs
up the whole account and neither half can be replayed as the other.

A **transaction signature** covers the txid digest of section 10 through
`sha256("sccp/v1/pq-hybrid" || 0x00 || digest)` with the FIPS 204 context
`sccp/v1/hybrid-ed25519-mldsa65`. Verification is the conjunction: invalid unless
*both* halves verify, so an attacker must break Ed25519 and ML-DSA-65.
**Certificates, results and stream checkpoints** use the same construction over
their own `H(domain, payload)` digests. **Moves, clock receipts and gossip
envelopes** use Ed25519 over `H(domain, payload)` directly, because they are
per-ply data; each is anchored by a hybrid commitment (section 8.5).

The keyfile (`src/sccp/crypto/wallet.py`) is `version: 2` with `"scheme": "ed25519-mldsa65"`, and
it is the only version. A version-1 (Ed25519-only) keyfile, or a `scheme` other
than `ed25519-mldsa65`, is refused with a typed error rather than upgraded in
place: silent upgrading would be a downgrade attack waiting to happen.

**Nothing classical is left.** The dual-path plumbing of the earlier optional
feature is gone, not deprecated:

| Removed | Why it was a seam |
| --- | --- |
| address version `0` and its truncated hash | two program shapes, two hash domains |
| `ADDRESS_VERSION` dispatch in `encode_address` / `decode_address` | a scheme choice inside address parsing |
| `HybridSignatureHex` / `TxSignatureHex` unions | a classical signature could satisfy a hybrid field |
| scheme dispatch in `chain/tx.py` (key length → verifier) | an attacker aims at the branch |
| `SCHEMES`, `SCHEME_ED25519`, `KEYFILE_VERSION_PQ`, `DEFAULT_KDF` | two keyfiles, two KDF profiles |
| `--scheme` / `--kdf-n` scheme selection | an operator could mint a weak account |
| `SigningKey = Keypair \| HybridKeypair` | a type-level invitation to dual paths |
| web `HYBRID_ADDRESS_VERSION` refusal and `isHybridSignatureHex` | a second address/signature shape in JS |
| "a hybrid key cannot mine" guard | the opposite of the point |

The acceptance invariants, checked by `tests/unit/test_pq.py` and the codebase
itself:

```text
SCHEMES, SCHEME_ED25519, SigningKey, ADDRESS_PQ, encode_hybrid_address  -> 0 hits
ADDRESS_VERSION                                                        -> exactly 1 (value 1)
sccp wallet new --scheme ...                                           -> exit 2 (no such option)
keyfile                                                                -> version 2, scheme ed25519-mldsa65
transaction signature                                                  -> always 6748 hex
```

**Fail closed on a backend without ML-DSA.** Verification returns `False` for
everything when the OpenSSL build exposes no ML-DSA provider, and key generation
raises `PQUnavailableError`. A node or miner must refuse to start rather than run
a chain whose signatures it cannot make or check; `sccp node run`, `sccp miner
run` and `sccp devnet up` check `pq_available()` at startup.

**Browsers.** `src/web` re-implements canonical JSON, bech32 (version 1), both
halves of the hybrid key, addresses, tx bodies/txids and the commitment fold. It
can therefore create and derive an account, play moves (the Ed25519 half is
HMAC-derived from the same master seed), verify every stream signature, verify a
result's `move_commitment`, and *produce* hybrid signatures: `keys.ts` derives
ML-DSA-65 through `@noble/post-quantum`, whose FIPS 204 key generation is
byte-identical to `cryptography`'s for the same sub-seed, so the bytes the tab
registers and signs are the bytes the node and the relay verify. Ledger
transactions (`signTransaction`), certificate start signatures
(`signCertificate`) and result co-signatures are all hybrid; moves stay Ed25519
because section 8.5 keeps that stream classical. `verifyProof` runs the two
hybrid signature rules against the roster's keys rather than skipping them, and
`sccp wallet sign-result --payload <hex>` remains an alternative for a key that
is not in a browser.

### 10.6 Liquidity pools

A DEX pool is consensus state, not a contract account. It has an ordered pair
`(asset_a, asset_b)` — the empty string means native SCCP — two integer reserves
and an LP share ledger keyed by address. `pool_id` is
`H("sccp/v1/pool", {asset_a, asset_b})`, so both nodes derive the same id from
either side order.

The curve is `x * y = k`. A swap with `fee_bps = dex_fee_bps` pays

```text
amount_out = amount_in * (10000 - fee_bps) * reserve_out
             / (reserve_in * 10000 + amount_in * (10000 - fee_bps))
```

rounded down. The fee remains in the input reserve. An initial deposit mints
`isqrt(amount_a * amount_b)` shares; later deposits mint the minimum of the two
proportional share counts, so a deliberately imbalanced deposit cannot mint value
out of the pool. Removing shares pays each reserve pro rata, rounded down. The
chain refuses a redemption that would pay zero of either side, and deletes the
pool when the last share is burned.

All reserves are integer base units. Fees for DEX transactions are still paid in
native SCCP, so an account swapping a variant token needs a small SCCP balance.
Creating a pool costs nothing beyond the ordinary transaction fee: there is no
pool-creation deposit and no protocol-owned minimum liquidity, so the first
deposit is the whole of `k` and a zero-share initial deposit is refused rather
than burned.

**Execution model.** A swap carries `min_amount_out` and nothing else: there is no
`deadline_height` and no per-block clearing price. Transactions execute in block
order against the reserves the earlier transactions in that block left, and the
only bound on the price a swap receives is its own `min_amount_out`, checked when
it applies. Two consequences follow and are deliberate for v1:

- a builder who orders a swap before another can move the price the later one
  pays, up to that transaction's bound;
- a transaction delayed in a mempool executes at whatever the curve yields when
  it is included, because nothing expires it.

Both are open design items rather than oversights; `BIG_PICTURE.md` section 6.2
records the alternatives (a deadline field, batch clearing, an opt-in
commit-reveal) and `THREAT_MODEL.md` section 4 carries the residual risk.

## 11. Ledger state

The state is a deterministic, integer-only object (`src/sccp/chain/state.py`);
nothing in it reads a clock, a random source or the network. It holds one
`Account` per address:

| Account field | Meaning |
| --- | --- |
| `balance`, `immature` | spendable and maturing native SCCP base units |
| `assets`, `immature_assets` | spendable and maturing variant-token balances keyed by asset id |
| `bonded` | locked bond (registration or witness) |
| `nonce` | next valid transaction nonce |
| `public_key` | the spend key, once known (10.1) |
| `variant` | the miner's registered chess variant (`standard` is native) |
| `registered_until_epoch`, `endpoint`, `engine`, `unbonding_height`, `banned_until` | registration and bond lifecycle |

alongside the proof and match registries (`game_hash -> proof_id`, match id ->
epoch/round/seats), per-miner stats (wins, games, last opponent epoch, which is
what the opponent cooldown reads), per-epoch records (beacon, roster, quotas,
matchable flag), the availability commitments and reveals (by epoch), the clock
witness registry, open/closed timeout claims, and the DEX pools
(`pool_id -> {asset_a, asset_b, reserve_a, reserve_b, total_shares, shares}`).

`state_root` is the merkle root (section 1) over `address -> account` leaves,
where an account leaf commits to every field of the table above. When the chain
has DEX pools, it also commits one leaf per pool under the key `pool:<pool_id>`,
so reserves and LP positions are as authenticated as balances. `roster_root` is
the same construction over the epoch's roster, committing only
`{public_key, max_games}` per member plus `variant` for non-standard members.
Both are recomputed rather than trusted.

Snapshots are JSON with the top-level keys `network`, `height`, `tip_hash`,
`accounts`, `proofs`, `proof_ids`, `matches`, `miner_stats`, `epochs`,
`availability`, `reveals`, `witnesses`, `timeout_claims`, `difficulty`,
`last_k_change_height`, `timestamps`, `minted` and, when non-empty, `pools`. A
snapshot is a cache: a node replaying blocks checks the snapshot's `state_root`
against the state it rebuilds, so a corrupted snapshot cannot become consensus.

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

### 12.5 Size budgets

Every cap is a consensus constant in `NetworkParams`, chosen so the largest legal
object fits with room to spare. All three networks share the same numbers because
the signature sizes are the same:

| Cap | Value | Bounds |
| --- | --- | --- |
| `max_tx_bytes` | 131 072 (128 KiB) | one transaction, including its 6748-hex signature |
| `max_pgn_bytes` | 262 144 (256 KiB) | proof PGN text, and therefore `max_plies` at the 16-character SAN limit |
| `max_block_bytes` | 1 048 576 (1 MiB) | header + coinbase + transactions + proofs |
| `CLOCK_EVIDENCE_PLIES` | 64 | receipts a `clock_evidence` bundle may carry |

The largest transaction the protocol can produce is a 400-ply `timeout_claim`.
`tests/unit/test_tx_budget.py` builds exactly that — a real certificate, a real
window of witness-signed receipts, a three-witness hybrid checkpoint and the
claim — and asserts the result:

```text
window: 64 receipts (3 witness signatures each)    ~55 000 B
final flag receipt + 3-signature hybrid checkpoint
unsigned transaction, before the claim signature    53 030 B   (measured)
cap                                              131 072 B
headroom                                          78 042 B
```

The same claim carrying all 396 receipts instead of the window is **142 236
bytes**; the test asserts that too, so the window cannot be removed by accident.
The old cap was already too small for a 400-ply flag bundle with Ed25519-only
signatures — a latent bug that had nothing to do with post-quantum sizes — and
the hybrid checkpoint is what let the evidence shrink enough to fit without a
block-size change.

A `move_evidence` bundle is smaller still: 400 plies of `{ply, seat, san}` are
about 36 KiB, plus one 3.4 KiB checkpoint signature per quorum member.

## 13. Genesis documents

Genesis is the only place where supply can exist before a block subsidy. Its
block hash is pinned in `NetworkParams` (`genesis_hash`), and a pinned network
refuses a document that hashes to anything else, so two nodes cannot disagree
about height 0.

```json
{"version": 2,
 "network": "mainnet",
 "timestamp": 1735689600,
 "miner": "sccp1...",
 "allocations": [
   {"address": "sccp1...", "public_key": "hex", "amount": 4200000000000},
   {"address": "sccp1...", "public_key": "hex",
    "asset": "<64-hex C960 id>", "amount": 52500000000000}
 ],
 "pools": [
   {"creator": "sccp1...", "amount_a": 500000000, "amount_b": 500000000,
    "asset_b": "<64-hex C960 id>"}
 ],
 "htlcs": [
   {"sender": "sccp1...", "beneficiary": "sccp1...", "hashlock": "64 hex",
    "amount": 100000000, "timelock_height": 1000, "status": "claimed",
    "preimage": "64 hex", "created_at_height": 0, "closed_at_height": 0,
    "nonce": 1000000}
 ],
 "hash": "64 hex chars"}
```

The `hash` shown above is illustrative; a document may omit it, and a pinned
network always checks the computed hash against `NetworkParams.genesis_hash`.

`version` must be `2` and `network` must name the parameter set. `miner` is
optional and defaults to the first allocation. Each `allocations` entry needs a
positive integer `amount` in base units, a valid address for this network, and a
`public_key` that **derives** that address — Ed25519 is not key-recoverable, so
an allocation without a matching key would be unspendable. The entry is native
SCCP when `asset` is omitted and otherwise names a variant asset id. One address
may appear once per asset; a duplicate `(address, asset)` pair is rejected.

`pools` and `htlcs` are optional and are applied after the allocations, in that
order. A genesis pool debits its creator's allocation exactly like `create_pool`
and records the creator's `isqrt(amount_a * amount_b)` LP shares. A genesis HTLC
uses `htlc_id = H("sccp/v1/htlc", {from: sender, nonce})`; `locked` holds the
amount in escrow, `claimed` debits the sender and credits the beneficiary, and
`refunded` has no net ledger effect. A claimed record must carry the preimage
that opens its hashlock. Genesis pools and HTLCs are limited to height 0 and are
part of `state_root`.

Mainnet allocates **1 basis point of the SCCP cap** — 2,100 SCCP — at genesis as
a bootstrap grant, and **no variant supply**: every C960 in existence is mined
(or, on testnet only, seeded as demo liquidity). The public address and basis
point live in `genesis/mainnet-premine.json`; the private key is not part of the
repository. `scripts/make_genesis.py` refuses to generate a document unless the
manifest's tranches total exactly that one basis point, so neither an accidental
mint nor a silently dropped grant can change the chain.

Testnet also seeds an SCCP/C960 pool with 5 coins per side and one completed
1-SCCP HTLC, using published demo keys, so `GET /v1/pools` and `GET /v1/htlcs`
are useful on a fresh deployment. The Ethereum leg remains an operator
deployment.

Genesis state is the allocation accounts, any seeded pools and HTLCs, the
initial difficulty, height 0, the genesis timestamp as the first median-time-past
sample, an open epoch 0 record and an empty body. `genesis/devnet.json` is the
devnet chain used by the test suite; `genesis/testnet.json` and
`genesis/mainnet.json` are the pinned networks. Test-network faucet keys come
from published seeds, which is why those networks are safe to hand out and
mainnet must never gain one.

## 14. Node API and gossip

### 14.1 HTTP API

Every response is a frozen pydantic model with `extra="forbid"` and carries the
network id where the plan asks for it, so a wallet can refuse a node on the
wrong network. Errors map as: `NotFoundError` → 404, any other `SccpError` → 400,
an orphan block on `POST /v1/blocks` → 202.

| Method | Path | Response |
| --- | --- | --- |
| GET | `/v1/status` | `NodeStatus`: network, protocol version, height, tip, genesis, peers, syncing, mempool sizes, native and variant-token minted totals, and the epoch's active clock-witness count (`witnesses`, which the relay gates pairing on) |
| GET | `/v1/params` | the network parameters, including each variant and the asset it mints |
| GET | `/v1/blocks` | `BlockList`: newest first (`?limit=`, default 25, max 200; `?before_height=` for the next page), with proof counts per variant |
| GET | `/v1/blocks/{height-or-hash}` | `BlockView`: network, hash, parent, height, work, main-chain flag, block |
| POST | `/v1/blocks` | `BlockView`, or 202 when the parent is unknown |
| GET | `/v1/accounts/{address}` | `AccountView`: network, native and per-asset balances, immature queues, bonded, nonce, registration variant, endpoint, engine, key, ban height |
| GET | `/v1/pools` | `PoolList`: every DEX pool with reserves, total shares, fee, and each LP address's shares |
| GET | `/v1/pools/{pool_id}` | `PoolView`: one DEX pool (404 when the chain has not created it) |
| GET | `/v1/bridge` | `BridgeView`: the Ethereum chain id/name, HTLC contract, confirmation depth and SCCP margin for this network |
| GET | `/v1/htlcs` | `HtlcList`: SCCP hash locks filtered by `?address=` or `?hashlock=` |
| GET | `/v1/htlcs/{htlc_id}` | `HtlcView`: one lock, its status and the preimage once claimed |
| GET | `/v1/orders` | `OrderList`: open signed sell-SCCP orders (`?maker_sccp=` filter) |
| GET | `/v1/orders/{order_id}` | `OrderDetail`: one order and its take, if any |
| POST | `/v1/orders` | `OrderAccepted`: publish a signed order in the off-consensus book |
| POST | `/v1/orders/{order_id}/takes` | `TakeAccepted`: attach a signed take after the ETH leg is locked |
| GET | `/v1/epochs/{epoch}` | `EpochView`: beacon, roster, availability, quotas, matchable flag |
| GET | `/v1/epochs/{epoch}/beacon` | `Beacon` (404 when the chain has none) |
| GET | `/v1/difficulty` | `DifficultyView`: network, current `wins_required`/`seal_bits` and retarget history |
| GET | `/v1/ratings` | `RatingsView`: derived Glicko-2 rating per miner, strongest first (`?limit=`, default 100), each row naming the miner's registered variant, plus the fixed-bucket distribution |
| GET | `/v1/ratings/{address}` | `MinerRatingDetail`: rating, deviation, volatility, record, rank, registered variant and one history point per epoch (the 1500 prior when the seat has no settled game) |
| GET | `/v1/games` | `GameList`: pooled proofs first, then newest settle (`?limit=`, default 25, max 200; `?before_height=` and `?before_game_hash=` for the next page); every row names its variant |
| GET | `/v1/games/{game_hash}` | `GameView`: proof/match ids, winner, variant, height, mempool flag |
| GET | `/v1/games/{game_hash}/pgn` | `GamePgn`: the move text the proof for that game carries (404 when this node holds no block body for it) |
| GET | `/v1/matches/{match_id}` | `MatchInfo`: epoch, round, seats, variant, certificate, transcript |
| GET | `/v1/matches/{match_id}/transcript` | `Transcript` |
| GET | `/v1/mempool` | `MempoolView`: pending txids and proof ids |
| GET | `/v1/transactions/{txid}` | `TransactionRecord`: type, height, block hash, mempool flag |
| POST | `/v1/tx` | `TxAccepted`: the recomputed txid |
| POST | `/v1/proofs` | `ProofAccepted`: the proof id |
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

A running node serves that schema at `/openapi.json`, with Swagger UI at `/docs`
and ReDoc at `/redoc`. The pages' group names (node, chain, ledger, chess, DeFi)
and prose are documentation only, so they stay out of the snapshot while the
routes, parameters and models stay in it. The two HTML pages load their assets
from a CDN; a client that cannot reach one should read `/openapi.json` instead.

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

### 14.4 Derived miner ratings (Elo)

`GET /v1/ratings` and `GET /v1/ratings/{address}` expose a Glicko-2 rating for
every address that has settled a game. The rating is **derived, not committed**:

- It is replayed from the proofs on the adopted branch (`block.proofs`) in block
  order, and each proof is one game for both named seats. Only proofs the chain
  already accepted (section 9) contribute.
- Nothing in a block, a transaction or the API accepts a rating from a miner.
  There is no `rating` field, no `rating` transaction and no `POST` route: the
  only way to move a rating is to settle a verified game.
- It is not in `state_root`. A node that has not synced a proof simply has a
  shorter history to replay; two nodes at the same tip replay the same blocks in
  the same order, and the node caches that replay by tip hash.
- Each leaderboard row and detail view also names the miner's current registered
  variant (`standard` or `chess960`, from the account), so a reader can tell
  which discipline the rating belongs to. The Glicko-2 replay itself remains
  global across variants.

The parameters are the paper's: the prior is 1500 with deviation 350 and
volatility 0.06, `tau = 0.5`, and one rating period is one block epoch. Every
game in a period is evaluated against the opponents' pre-period ratings, so the
update is simultaneous; two games against the same opponent in a period are
averaged into one score, which is the paper's prescription. A miner that sits
out a period keeps its rating and its deviation grows by `sigma^2` per period,
which is why an idle miner's rating becomes visibly wider on its page. A rating
is marked `provisional` until it has 10 settled games.

The arithmetic for one period is delegated to the pinned `glicko2` package (an
MIT-licensed reference implementation of the paper). `sccp/ratings.py` owns
everything the paper does not: the replay in block order, the epoch boundary,
the prior, the duplicate-opponent averaging, validation and the presentation. A
library upgrade can therefore move a number on the explorer; it cannot move a
block, and it is a deliberate dependency bump rather than an incidental one.

The package is the reference, with one known quirk: its volatility objective
squares the pre-period rating (`mu^2`) where the paper squares the pre-period
deviation (`phi^2`). It reproduces the paper's worked example, but results for
ratings far from 1500 can differ slightly from a literal transcription. A node
follows this package, and since the value is derived, that difference is a
display detail rather than a consensus one; `sccp/ratings.py` records it.

The distribution is a fixed histogram: `<1000`, 100-wide bands from 1000 to
1999, then `>=2000`. It always covers every rated miner, even when `?limit=`
truncates the leaderboard; fixed bounds keep two polls and two networks
comparable.

Three limits are worth stating plainly:

- A rating measures results, not intent. Two colluding seats can trade wins and
  inflate each other; pairing's identity heuristics make that harder, but no
  number derived from legal games can prove a game was contested.
- The arithmetic is floating point. That is acceptable only because the value is
  never committed: a node on another platform may differ in the last decimal.
- Like `GET /v1/games`, the replay reads proof bodies. This implementation keeps
  every block, including after a snapshot; a future pruning mode would shorten
  the history rather than change a settled game.

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
- The **one-scheme** rule is not a versioned option. These must stay true, and
  each has a test or a grep invariant behind it:
  - `SCHEMES`, `SCHEME_ED25519`, `SigningKey`, `ADDRESS_PQ` and
    `encode_hybrid_address` do not exist;
  - there is exactly one `ADDRESS_VERSION` (value `1`) and one keyfile version
    (`2`, scheme `ed25519-mldsa65`);
  - a transaction signature is always 6748 hex characters, and
    `sccp wallet new --scheme …` exits 2 because the option is gone;
  - `node run`, `miner run`, the relay and `devnet up` refuse to start when
    `pq_available()` is false.
- Golden vectors for the game hash and opening hash live in
  `tests/fixtures/chess_vectors.json`; the crypto/address vectors live in
  `tests/fixtures/crypto_vectors.json` and the web vectors in
  `src/web/src/lib/vectors.json` (generated by `scripts/make_web_vectors.py`).
  Changing them is a deliberate consensus change, never a test fix. Hybrid
  signature *bytes* are hedged (randomized), so the web vectors pin payloads,
  commitments and the deterministic Ed25519-half signatures, and verify the
  hybrid blobs rather than comparing them.
- Genesis documents are generated by `scripts/make_genesis.py` and pinned in
  `NetworkParams`; regenerate both together.
- The gossip envelope fields, the signed payload and the documented HTTP paths
  and response fields are part of this document: adding a field is a compatible
  change only if clients ignore unknown fields, and anything else needs a
  protocol version bump.

