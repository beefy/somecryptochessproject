# Explore the chain

`src/web` is the browser client: a block explorer and a table that is one real
seat in a real match. It is React Native for Web rendered through Material Design,
built with Vite, and it talks to the node's `/v1` JSON API and, from `#/play`, to
the matchmaking relay — every number on the page is a field one of them sent.

For the day-to-day commands — `npm ci`, `npm run dev` against a node, port and
proxy variables, troubleshooting — see [`src/web/README.md`](../src/web/README.md).
This page is the tour.

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

The page holds one seat, not two. It registers that key with a signed `register`
transaction (bond, endpoint, engine fingerprint), commits availability for the
epoch two ahead, reveals the committed nonce in the epoch before it opens, and
then waits: the chain puts the address on the epoch's roster and the relay pairs
that roster from the beacon with `pair()`.

Paired, the page is the client a Stockfish miner is:

1. it signs the certificate the relay built (`sccp/v1/certificate`), which is the
   start signature that lets both clocks run;
2. it publishes each move with its ply and signature (`sccp/v1/move`) — the relay
   replays it, charges the mover's clock and signs a receipt;
3. it co-signs the result automatically (`sccp/v1/result`), which is what makes
   the transcript available to either seat.

When the game is over the page builds the `WinProof` from that co-signed
transcript (`build_win_proof`'s exact shape: opening ref, PGN, both result
signatures, `low_ply`, the proof id) and verifies it in the browser against the
epoch's real beacon and roster. The rules it runs are the ones in
`sccp/protocol/verification.py` that need nothing else: network and epoch
binding, certificate consistency, the match id, the colour draw, the fixed 30+0
control, the opening hash, the PGN replay and result, the canonical game hash,
both start signatures, both result signatures, `low_ply` and the proof id. If
every rule passes, the proof goes to `POST /v1/proofs`; the node's settlement
miner (`node run --mine`) puts it in a block.

What it cannot do is as important: a browser cannot open a trusted clock, so a
flag settles through the winner's bonded `timeout_claim` and the relay's witness
receipts, not from the page; and with no analysis engine, a network that samples
quality gets `verdict: "unknown"`, which a validator may challenge. The reference
implementation of the same flow in one process is
`python scripts/play_miner.py --on-chain`.

The move count, the SAN list, every hash and every signature match the Python
implementation; the TypeScript ports in `src/web/src/lib` are pinned to it by
generated vectors:

```bash
python scripts/make_web_vectors.py            # regenerate src/web/src/lib/vectors.json
python -m pytest tests/unit/test_web_vectors.py
cd src/web && npm test                        # vitest: canonical, keys, tx, proofs, verifier
```

## Limits

- The explorer never imports a wallet: the table's key lives in the tab's memory,
  so a refresh forgets it, and nothing else on the site signs anything.
- Routes live in the hash (`#/blocks/12`), so the bundle works from any static
  path without a server-side rewrite.
- A tab left open across a network switch keeps showing the documents it fetched;
  the header's network and tip are the ones to trust.
