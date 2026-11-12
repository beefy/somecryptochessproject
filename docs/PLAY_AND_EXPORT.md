# Play a game on-chain and export it

This is the end-to-end manual for playing a timed chess game against a miner,
mining the finished game's proof into a local devnet chain, and exporting that
game from the chain as a PGN file.

It uses two scripts:

- [`scripts/play_miner.py`](../scripts/play_miner.py) with `--on-chain` — builds
  or resumes a local devnet chain, seats you through the relay API and mines the
  finished game's `WinProof`.
- [`scripts/export_block_games.py`](../scripts/export_block_games.py) — asks a
  running node for a block, replays and validates every proof PGN in it, and
  writes one `.pgn` file per game plus a `manifest.json`.

The exporter reads the node's HTTP API, so it must talk to a node serving the
same chain that `play_miner.py` wrote. This guide uses the default devnet data
directory, `./data-play`, and the default node port, `8645`.

---

## 1. Prepare the workspace

Run every command from the repository root.

```bash
cd somecryptochessproject
source .venv/bin/activate          # if the environment is not active already
pip install -e ".[dev]"            # first time only
```

The default opponent is the deterministic `mock` engine, so a real Stockfish
binary is not required. If you want to play Stockfish instead, see
[Optional: play a stronger opponent](#optional-play-a-stronger-opponent).

The two scripts need different things:

| Command | What it needs |
| --- | --- |
| `play_miner.py --on-chain` | Python and this repository. It bootstraps its own chain. |
| `export_block_games.py` | A running `sccp node run` serving the chain. |

---

## 2. Play and mine one game

Start the on-chain game:

```bash
python scripts/play_miner.py --on-chain --color auto
```

What happens:

1. The script creates a fresh devnet chain in `./data-play` if that directory
   has no chain yet. The bootstrap ends at height 3.
2. It funds and registers your seat and three opponent miners, then opens the
   current epoch.
3. It seats you through the same relay API used by the other modes. The game
   uses the real **30+0** timed session, with witness-signed move receipts and
   automatic flagging.
4. When the game ends, it co-signs the result and mines the winner's proof into
   the next block. On a fresh chain the first game is usually **block 4**.

`--color auto` takes whichever colour the pairing gives you. That is also the
right choice when you resume a chain: a later epoch may not deal you the colour
you used in an earlier game.

### During the game

The relay plays the opening line selected by the beacon, so the first position
you see is after the book moves, not the standard start position. The script
prints that line and the board before asking for your move.

Moves may be SAN or UCI:

```text
Nf3
O-O
exd5
e8=Q
g1f3
```

These commands are available at the prompt:

| Command | Meaning |
| --- | --- |
| `help` | Show the command list. |
| `board` | Redraw the current position. |
| `moves` | Print the whole game so far. |
| `clocks` | Print both clocks and whose turn it is. |
| `receipts` | Print the relay's witness receipts for this match. |
| `resign` | Resign on your turn (a decisive result). |
| `quit` | Leave without a finished result, so **no block is mined**. |

### What makes a game produce a block

A game is mined only when it produces a valid, decisive proof:

- the result must be decisive (`1-0` or `0-1`), not a draw;
- both seats must co-sign the result;
- the game must have at least devnet's **20 plies** (10 full moves); and
- the termination must be ordinary co-signed play, not a time forfeit.

Losing is not a problem. If the engine wins, the engine's proof is mined. If
you win, your proof is mined. A strong opponent does not prevent a block; a draw,
a flag, quitting or a game shorter than 20 plies does.

> **Time forfeits:** the relay flags a clock correctly, but this demo does not
> yet have the on-chain claim transaction for `time_forfeit`. If you are about to
> be flagged and want a block, resign after the 20-ply minimum instead of letting
> the clock reach zero.

---

## 3. Read the settlement report

When the game ends, look for output like this:

```text
result   1-0 by checkmate after 48 plies: you won
...
chain    co-signing the result and mining the proof…
chain    mined block 4 7c1d0a91b2e3…
proof    2eaf7b2c… (winner white: sccpt1…, network devnet)
node     sccp --data-dir data-play node run --network devnet
```

The important values are:

- **block** — the height that contains this game's proof. This is what you pass
  to the exporter.
- **proof** — the proof identifier, and the winning seat/address.
- **node** — the command that serves this chain.

If the game did not mine, the report says why instead:

```text
chain    not mined: a draw has no winner to prove
         chain stays at height 3; a decisive game is required for a proof
```

Common reasons are a draw, a time forfeit, fewer than 20 plies, quitting, or a
result the opponent would not co-sign. In all of those cases the chain does not
advance, so there is nothing to export for that game.

Write down or copy the reported block height. You will use it in
[step 5](#5-export-the-game-from-the-chain).

## 4. Play more games on the same chain

You do **not** need to delete `data-play` between games. Running the same
command again resumes the chain at its next epoch:

```bash
python scripts/play_miner.py --on-chain --color auto
```

To play several games in one sitting:

```bash
python scripts/play_miner.py --on-chain --games 3 --color auto
```

Each decisive game mines its own block. On a fresh chain the three games are
usually blocks 4, 5 and 6, but always use the height printed by each settlement
report rather than assuming a height.

Useful options:

| Option | Effect |
| --- | --- |
| `--chain-dir DIR` | Use a different data directory (default: `./data-play`). |
| `--fresh` | Delete the existing chain in `--chain-dir` and bootstrap again. |
| `--games N` | Play `N` games on the same chain (on-chain only). |
| `--color auto` | Take the colour the pairing assigns. Recommended on resumes. |
| `--pgn-out FILE` | Also write the relay PGN; only valid when playing one game. |

`--fresh` discards the chain, so use it deliberately. To preserve an old chain
for inspection, move it first:

```bash
mv data-play data-play-old
python scripts/play_miner.py --on-chain --fresh
```

The chain files are:

```text
data-play/
├── chain.sqlite   # the chain itself
└── play.json      # sidecar recording which human seed owns this chain
```

---

## 5. Export the game from the chain

### 5.1 Start a node on the same chain

The exporter does not read SQLite directly. It asks a node, so start one in a
second terminal and leave it running:

```bash
sccp --data-dir data-play node run --network devnet
```

This is the command printed by `play_miner.py` at settlement. The global
`--data-dir` selects the chain and `node run --network` selects its network.

This serves the API on `http://127.0.0.1:8645` by default. Confirm the node is
serving the expected chain:

```bash
curl -s http://127.0.0.1:8645/v1/status
```

You should see `"network":"devnet"` and the height of your latest proof block.

> **Important:** if another `sccp node run` is already using port 8645, it may
> be serving a different chain (for example the default testnet `./data`
> directory). Stop it first, or use another port as shown in
> [Troubleshooting](#7-troubleshooting). Exporting block 0 with `(0 games)` is
> the usual symptom of talking to the wrong node.

### 5.2 Export the block that contains your game

In a third terminal, export the block from the settlement report:

```bash
python scripts/export_block_games.py --block 4 --out block-games/game-1
```

The output looks like this:

```text
node     http://127.0.0.1:8645 devnet tip 5 caa5d258ab93…
block    4 7c1d0a91b2e3… (1 game)
  block-games/game-1/game-<game_hash>.pgn  <white address> vs <black address>  1-0 by checkmate (48 plies)
manifest block-games/game-1/manifest.json
```

The first line is a safety check: it names the node, its network and its tip.
If that says `testnet` or `tip 0`, you are connected to the wrong chain.

`--block` accepts either a height or a full block hash. You can also export the
node's current tip:

```bash
python scripts/export_block_games.py --out block-games/tip
```

When exporting several games, use a separate `--out` directory per block. The
PGN filenames differ, but each export writes its own `manifest.json`, and a
shared directory would leave only the last manifest.

### 5.3 If port 8645 is already in use

Run the node on another port:

```bash
sccp --data-dir data-play node run --network devnet --port 8646
```

Then point the exporter at that port:

```bash
python scripts/export_block_games.py \
    --node http://127.0.0.1:8646 \
    --block 4 \
    --out block-games/game-1
```

---

## 6. Inspect the export

The export directory contains:

```text
block-games/game-1/
├── game-<game_hash>.pgn
└── manifest.json
```

The PGN is the proof's co-signed game record. The manifest is the index:

```bash
python -m json.tool block-games/game-1/manifest.json
```

It records:

- the network and the block height, hash, parent, miner and epoch;
- the proof id, game hash and match id;
- both seat addresses and engine fingerprints;
- the result, termination, ply count and opening.

The exporter replays each PGN against the certificate and proof before writing
anything. It refuses to write a file when the game hash, result, winner or ply
count disagrees, so a successful export is also a verification pass.

To export several blocks in one go, repeat the command with a different
`--block` and `--out`:

```bash
python scripts/export_block_games.py --block 5 --out block-games/game-2
python scripts/export_block_games.py --block 6 --out block-games/game-3
```

## 7. Troubleshooting

### The exporter reports block 0 with 0 games

This means the node is serving a different chain, usually the testnet genesis in
`./data`, not your devnet chain in `./data-play`.

The new report makes this explicit:

```text
node     http://127.0.0.1:8645 testnet tip 0 491b3938…
block    0 491b3938… (0 games)
note: this node's tip is genesis (block 0), so it has no games; if you mined a
game, start the node on that chain (--network/--data-dir) or point --node at
the right port
```

Fix it by stopping the old node and starting the devnet one:

```bash
sccp --data-dir data-play node run --network devnet
```

If you cannot stop the old node, serve the devnet chain on another port and pass
that URL with `--node`.

### The exporter cannot connect

Start the node first. The exporter does not start one for you:

```bash
sccp --data-dir data-play node run --network devnet
```

If you used `--chain-dir ./my-play-data`, use that path in `--data-dir` too.

### Port 8645 is already in use

Use another port for the node and the matching URL for the exporter:

```bash
sccp --data-dir data-play node run --network devnet --port 8646
python scripts/export_block_games.py --node http://127.0.0.1:8646 --block 4
```

The exporter's default is `http://127.0.0.1:8645`.

### The game finished but no block was mined

Read the `chain    not mined:` line. The usual causes are:

| Message or cause | Meaning | Fix |
| --- | --- | --- |
| `a draw has no winner to prove` | Draws are not proof-of-win blocks. | Play a decisive game. |
| `time forfeits have no claim transaction…` | The flag is valid, but this demo has no on-chain claim path yet. | Resign before the flag, after 20 plies, if you need a block. |
| `<N> plies is below devnet's 20-ply proof minimum` | The game ended too quickly. | Play at least 20 plies (10 full moves). |
| `opponent has not co-signed…` or another co-sign refusal | A proof needs both result signatures. | Finish normally; do not quit or force a result. |
| You typed `quit` | There is no finished result to settle. | Play until checkmate or resign. |

A loss is still fine: the engine's winning proof is mined. The block is about a
decisive, co-signed game, not about you winning.

### `--pgn-out` is rejected with `--games`

`--pgn-out` names one file, so it cannot represent several games:

```bash
# Valid: one game, one PGN
python scripts/play_miner.py --on-chain --pgn-out game.pgn

# Invalid: three games, one --pgn-out path
python scripts/play_miner.py --on-chain --games 3 --pgn-out game.pgn
```

The proof PGNs are written by the exporter; `--pgn-out` is just a convenience
copy of the relay's record for a single game.

### The next run says it cannot seat you as the requested colour

A resumed chain's next epoch may assign the other colour. Use `--color auto`:

```bash
python scripts/play_miner.py --on-chain --color auto
```

`--fresh` also starts over, but it deletes the chain and its games.

### Export fails while validating a PGN

The exporter replays every PGN against its certificate and proof. A mismatch
usually means the node is serving a different chain or a block from a different
network. Confirm the `node … <network> tip …` line, then export the height from
the same chain's settlement report.

---

## Optional: play a stronger opponent

The default `mock` engine needs no download. To play Stockfish:

```bash
python scripts/fetch_engine.py --engine stockfish-19 --dest ./bin
python scripts/play_miner.py --on-chain \
    --engine stockfish-19 \
    --engine-path ./bin/<binary-printed-by-fetch_engine>
```

`fetch_engine.py` verifies the archive's pinned SHA-256 before installing the
binary and prints the resulting path. Use that path for `--engine-path`.

---

## Related reference

- [README quickstart](../README.md#6-quickstart) — command overview for the
  whole project.
- [`docs/PROTOCOL.md`](PROTOCOL.md) — proof, PGN and block formats.
- `python scripts/play_miner.py --help` — every play option.
- `python scripts/export_block_games.py --help` — every export option.
