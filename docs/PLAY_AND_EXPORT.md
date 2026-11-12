# Play a game on-chain and export it

There are four commands in this workflow:

| Step | Command | What it does |
| --- | --- | --- |
| 1 | `python scripts/play_miner.py --on-chain` | Starts a local devnet, plays your game, and mines its proof into a block. |
| 2 | `sccp --data-dir data-play node run --network devnet` | Serves the finished chain so the exporter can read it. |
| 3 | `python scripts/export_block_games.py --block <height>` | Writes the block's game as a PGN plus `manifest.json`. |
| 4 | `curl -s http://127.0.0.1:8645/v1/blocks/<height> \
  | python -c 'import json,sys; print(json.load(sys.stdin)["block"]["proofs"][0]["pgn"])'` | Read the PGN directly from the block |

`play_miner.py --on-chain` **starts the devnet itself**. You do not need to start
a separate devnet or relay first.

In short: step 1 creates the chain, step 2 serves it over HTTP, and step 3 reads
it through the node API. The node is a server for the existing chain, not a
second devnet.

The only server you start separately is the node in step 2. It is needed only
after the game, because `export_block_games.py` reads the node's HTTP API rather
than reading `chain.sqlite` directly.

This guide uses the defaults:

- chain directory: `./data-play`
- node URL: `http://127.0.0.1:8645`

---

## 1. Prepare

Run commands from the repository root:

```bash
cd somecryptochessproject
source .venv/bin/activate       # if not already active
pip install -e ".[dev]"         # first time only
```

The default opponent is the built-in `mock` engine, so Stockfish is not needed.

---

## 2. Play and mine a game

```bash
python scripts/play_miner.py --on-chain --color auto
```

On the first run this:

1. creates the devnet chain in `./data-play`,
2. starts the relay and opponent in-process,
3. seats you,
4. plays a real 30+0 timed game, and
5. co-signs the result and mines the winner's proof into a block.

On a fresh chain the first game is usually **block 4**.

`--color auto` takes whichever colour the pairing assigns. It is also the best
choice when resuming a chain.

### Controls

Type a SAN move or a UCI move:

```text
Nf3
O-O
exd5
e8=Q
g1f3
```

Useful commands at the prompt:

| Command | Meaning |
| --- | --- |
| `help` | Show all commands. |
| `board` | Redraw the position. |
| `moves` | Print the game so far. |
| `clocks` | Print both clocks. |
| `resign` | Resign on your turn. |
| `quit` | Leave without finishing; no block is mined. |

### A game produces a block only when

- the result is decisive (`1-0` or `0-1`);
- both seats co-sign it;
- it lasts at least 20 plies (10 full moves); and
- it does not end by time forfeit.

A loss is fine: the winner's proof is mined, even when the winner is the engine.
A draw, a flag, `quit`, or a game shorter than 20 plies produces no block.

> If you are about to be flagged and want a block, resign after the 20-ply
> minimum instead of letting the clock reach zero.

---

## 3. Note the block height

At the end the script prints something like:

```text
result   1-0 by checkmate after 48 plies: you won
chain    mined block 4 7c1d0a91b2e3…
proof    2eaf7b2c… (winner white: sccpt1…, network devnet)
node     sccp --data-dir data-play node run --network devnet
```

Copy the block height (`4` in this example). That is the `--block` value for the
export.

If the game did not mine, the script prints the reason instead. In that case
there is no new block to export for that game.

### More games on the same chain

Running the play command again resumes `./data-play` and plays the next game.
To play several games in one sitting:

```bash
python scripts/play_miner.py --on-chain --games 3 --color auto
```

Each decisive game mines its own block; use the height printed by each
settlement report. `--fresh` deletes the chain and starts over.

---

## 4. Serve the chain with a node

Start this **after** the game. Leave it running in a second terminal:

```bash
sccp --data-dir data-play node run --network devnet
```

This is the node that `export_block_games.py` will call. It must use the same
`--data-dir` and network as the game (`data-play` and `devnet` by default).

Check it quickly:

```bash
curl -s http://127.0.0.1:8645/v1/status
```

The response should contain `"network":"devnet"` and your chain height.

If port 8645 is busy, use another port:

```bash
sccp --data-dir data-play node run --network devnet --port 8646
```

Then pass `--node http://127.0.0.1:8646` to the exporter.

---

## 5. Export the block

In another terminal (or back in the first, now that the game has finished),
export the height from the settlement report:

```bash
python scripts/export_block_games.py --block 4 --out block-games/game-1
```

Expected output:

```text
node     http://127.0.0.1:8645 devnet tip 4 7c1d0a91b2e3…
block    4 9f3a02c4d5e6… (1 game)
  block-games/game-1/game-<game_hash>.pgn  <white> vs <black>  1-0 by checkmate (48 plies)
manifest block-games/game-1/manifest.json
```

The `node` line is your safety check. It must show `devnet` and the same chain
you played on. If it shows `testnet` or `tip 0`, you are exporting from the wrong
node.

Other forms:

```bash
# Export the node's current tip
python scripts/export_block_games.py --out block-games/tip

# Export a block from a node on another port
python scripts/export_block_games.py \
    --node http://127.0.0.1:8646 \
    --block 4 \
    --out block-games/game-1
```

Use a separate `--out` directory per block. Each export writes its own
`manifest.json`.

---

## 6. Inspect the export

```text
block-games/game-1/
├── game-<game_hash>.pgn
└── manifest.json
```

- The `.pgn` is the proof's co-signed game record.
- `manifest.json` records the block, proof, match, seats, engines, result,
  termination, ply count and opening.

The exporter replays and validates every game before writing it. A successful
export is also a verification pass.

Inspect the manifest with:

```bash
python -m json.tool block-games/game-1/manifest.json
```

### Read the PGN directly from the block

The PGN is stored in the block body at `block.proofs[*].pgn`, so you can read it
from the node API without exporting. There is no HTML explorer yet.

With the node from step 4 still running:

```bash
curl -s http://127.0.0.1:8645/v1/blocks/5 \
  | python -c 'import json,sys; print(json.load(sys.stdin)["block"]["proofs"][0]["pgn"])'
```

Change `5` to your block height. A block can contain more than one proof; print
all of them with:

```bash
curl -s http://127.0.0.1:8645/v1/blocks/5 \
  | python -c 'import json,sys; print("\n".join(p["pgn"] for p in json.load(sys.stdin)["block"]["proofs"]))'
```

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `block 0` / `0 games` | The node is serving a different chain, usually testnet in `./data`. Stop it and start `sccp --data-dir data-play node run --network devnet`, or point `--node` at the correct port. |
| Cannot connect | The node is not running. Start it first. |
| Game produced no block | Check the `chain    not mined:` line. Draws, time forfeits, games under 20 plies, `quit`, and missing co-signatures produce no block. |
| Wrong colour after resuming | Use `python scripts/play_miner.py --on-chain --color auto`. |
| `--pgn-out` rejected with `--games` | `--pgn-out` names one file. Omit it for multi-game runs; the exporter writes the proof PGNs. |

---

## Quick reference

```bash
# 1. Play and mine
python scripts/play_miner.py --on-chain --color auto

# 2. Serve the same chain (second terminal)
sccp --data-dir data-play node run --network devnet

# 3. Export the reported block (another terminal)
python scripts/export_block_games.py --block 4 --out block-games/game-1
```

Related references:

- [README quickstart](../README.md#6-quickstart)
- [`docs/PROTOCOL.md`](PROTOCOL.md)
- `python scripts/play_miner.py --help`
- `python scripts/export_block_games.py --help`
