# Explore the chain

`src/web` is the browser client: a block explorer and a table where you can play a
game and watch the protocol sign it. It is React Native for Web rendered through
Material Design, built with Vite, and it talks only to the node's `/v1` JSON API —
every number on the page is a field the node sent.

## Build it

```bash
cd src/web
npm ci
npm run build          # tsc --noEmit && vite build -> src/web/dist
```

`sccp node run --web` mounts `src/web/dist` at `/` next to the API, so one process
serves the explorer and the chain it describes:

```bash
sccp --network testnet --data-dir data node run --web
# open http://127.0.0.1:8645/
```

`SCCP_WEB_DIST` points the node at a different build directory. It is an
override, not a hint: a directory without an `index.html` means "no explorer"
rather than a stale bundle from somewhere else.

## Develop against a node

```bash
cd src/web
SCCP_NODE=http://127.0.0.1:8645 npm run dev   # http://localhost:5173
```

The dev server proxies `/v1` to `SCCP_NODE` (default `http://127.0.0.1:8645`).
`VITE_SCCP_API` overrides both, which is what a hosted static build uses to point
at a remote node:

```bash
VITE_SCCP_API=https://testnet.example.com npm run build
```

## What the pages show

- **Overview** — height, tip age, epoch, peer count, pool sizes and the newest
  blocks, games and open challenges.
- **Blocks** — newest first, paged with the node's `before_height` cursor rather
  than an offset, so a reorg cannot make a page skip a block.
- **A block** — every header field (parent, epoch, beacon, miner, difficulty,
  seal nonce, chain work, merkle/state/roster roots), the coinbase, transactions
  with their payloads, and one card per win proof: game hash, opening and PGN.
- **Games** — proofs in the node's own order: pending first, then newest
  settlement. A row says whether the proof is in a block or still in the mempool
  and how the chain currently judges it.
- **A game** — settlement state, the match certificate (both seats, opening,
  engines, start signatures) and the PGN, when this node has cached the
  transcript.
- **An account** — spendable, immature and bonded balances, the next nonce, miner
  registration and the games this node lists with that seat.

## The table (`#/play`)

Both seats are played from one tab. The page generates two Ed25519 keypairs in
the browser, derives their bech32 addresses under the node's network id, draws
colours with the protocol's own beacon rule and signs each move with the exact
payload a seat signs (`sccp/v1/move`). A finished game produces the canonical
game hash and both signatures over `sccp/v1/result`.

What it does **not** do: submit a proof. A game played here has no book opening
and no opponent, so it could never pass verification. The only request the table
makes is a `POST /v1/faucet` for the white seat's address, which is a real
transfer into the node's mempool.

The move count, the SAN list and the hashes match the Python implementation; the
TypeScript ports in `src/web/src/lib` are pinned to it by generated vectors:

```bash
python scripts/make_web_vectors.py            # regenerate src/web/src/lib/vectors.json
python -m pytest tests/unit/test_web_vectors.py
cd src/web && npm test                        # vitest: canonical, bech32, keys, protocol
```

## Limits

- The explorer is read-only: there is no wallet import, and nothing is signed
  except the throwaway table keys.
- Routes live in the hash (`#/blocks/12`), so the bundle works from any static
  path without a server-side rewrite.
- A tab left open across a network switch keeps showing the documents it fetched;
  the header's network and tip are the ones to trust.
