# `src/web` — the SCCP explorer

The browser client: a block explorer and a table that is one real seat in a real
match. React Native for Web rendered through Material Design
(`react-native-paper`), bundled by Vite, TypeScript in strict mode.

It talks to two things: the node's `/v1` JSON API and, from `#/play`, the
matchmaking relay. Every number on screen is a field one of them sent and every
response is typed in [`src/types.ts`](src/types.ts). There is no fixtures mode:
with no node reachable, the pages say so rather than invent data.

## Requirements

- **Node.js 20.19+ or 22.12+** — Vite 8's engine range (`node -v`).
- **npm**, using the committed `package-lock.json` (`npm ci`).
- **The `sccp` CLI** for the node the page reads: `pip install -e .` from the
  repository root, or `.venv/bin/sccp`. Not needed if you only point a build at a
  node running somewhere else.

## Quick start: let the node serve the explorer

The shortest path, and the way a testnet runs it: one process serves the bundle at
`/` and the API at `/v1`, so the page and its data share an origin.

```bash
cd src/web
npm ci
npm run build          # tsc --noEmit && vite build -> src/web/dist

cd ../..
sccp --network testnet --data-dir data node run --web
# explorer  http://127.0.0.1:8645/
# API       http://127.0.0.1:8645/v1/status
```

`--web` mounts `src/web/dist`. `SCCP_WEB_DIST=/srv/explorer` mounts a build
somewhere else — it is an override, not a hint: if that directory holds no
`index.html` the node exits with status 2 and tells you to build, instead of
serving some stale bundle it found elsewhere. Add `--host 0.0.0.0` to expose it
beyond loopback.

`--network devnet --data-dir data-devnet` works the same way and starts from an
empty chain (height 0), which is the least surprising thing to click around in.
Each network wants its own data directory: the SQLite file is bound to the network
it was created for.

## Development: Vite with hot reload

Two terminals, node first:

```bash
# 1. a node to read
sccp --network devnet --data-dir data node run          # 127.0.0.1:8645

# 2. the dev server
cd src/web
npm ci
npm run dev                                            # http://localhost:5173
```

The dev server proxies `/v1` to `SCCP_NODE` (default `http://127.0.0.1:8645`), so
the browser only ever talks to `localhost:5173` and nothing needs CORS:

```bash
SCCP_NODE=http://127.0.0.1:9000 npm run dev    # node somewhere else
npm run dev -- --port 5273                     # dev server port busy
```

`npm run preview` serves the built `dist/` on `http://localhost:4173` with the
same `/v1` proxy — the fastest way to check a production build against a local
node.

## Against a node on another origin

`VITE_SCCP_API` sets the API base at **build time** (empty means "same origin"):

```bash
VITE_SCCP_API=https://testnet.example.com npm run build
```

Two things to know before doing that:

- The value is compiled into the bundle. Change it by rebuilding, not by editing
  anything at runtime.
- The node sends no CORS headers, so a browser refuses a cross-origin `fetch` from
  a statically hosted build. Publish `dist/` behind the same origin as the API —
  one nginx with `location /` for the files and `location /v1` proxied to the node
  — or keep using the node's own `--web`.
- The relay is a second origin again. `VITE_SCCP_RELAY` (default
  `http://127.0.0.1:8646`) sets where `#/play` looks for it, and the relay must
  allow the page: `sccp matchmaker run --origin http://your.explorer.example`.
  Without that flag the relay answers no preflights at all, which keeps a
  relay-only deployment from advertising itself to browsers.

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite dev server on `:5173`, `/v1` proxied to `SCCP_NODE` |
| `npm run build` | `tsc --noEmit`, then `vite build` into `dist/` |
| `npm run preview` | serves `dist/` on `:4173` with the same proxy |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest over `src/**/*.test.ts`; offline, no node needed |

## Configuration

| Variable | Read by | Default | Meaning |
| --- | --- | --- | --- |
| `SCCP_NODE` | `npm run dev`, `npm run preview` | `http://127.0.0.1:8645` | proxy target for `/v1` |
| `VITE_SCCP_API` | the bundle, at build time | empty (same origin) | absolute API base |
| `SCCP_WEB_DIST` | `sccp node run --web` | `src/web/dist` | directory to mount |

## Routes

| Route | Page |
| --- | --- |
| `#/` | overview: height, tip age, epoch, peers, pool sizes, newest blocks and games |
| `#/blocks` | blocks newest first, paged by the node's `before_height` cursor |
| `#/blocks/12`, `#/blocks/<hash>` | one block: header, coinbase, transactions, proofs |
| `#/games` | proofs in the node's order: pending first, then newest settlement |
| `#/games/<game_hash>` | settlement state, the match certificate, the PGN |
| `#/accounts/<address>` | balances, nonce, miner registration, that seat's games |
| `#/play` | a seat in a real match: register, get paired, play, announce the proof |

Routing lives in the hash, so the bundle works from any static path with no
server-side rewrite rule.

## The table is a miner seat

`#/play` is not a demo board. The key it holds goes through the same four steps a
Stockfish miner goes through, as signed ledger transactions:

| Step | Transaction | Why |
| --- | --- | --- |
| register | `register` | bond, endpoint, and the engine fingerprint the certificate will carry |
| commit | `availability` | a commitment for the epoch `availability_lead` ahead |
| reveal | `reveal` | the committed nonce, valid only in epoch `E - 1` |
| join | (signed certificate, posted to the relay) | the start signature; the second one activates the match and starts both clocks |

From there the page is a relay client: it publishes each move with its ply and
signature, co-signs the result automatically when the game ends, and then builds
the `WinProof` from the co-signed transcript. A TS port of the offline verifier
runs every rule that needs only the proof and the epoch beacon — network and
epoch binding, the match id, the colour draw, the opening commitment, the PGN
replay, both start signatures, both result signatures, the game hash, `low_ply`
and the proof id — and shows the table. If it all passes, the proof goes to
`POST /v1/proofs`; settlement is the node's `--mine`.

Two limits are real and shown on the page rather than hidden:

- **You cannot choose your opponent.** `pair()` sorts the whole epoch roster by
  the beacon and pairs neighbours, so the page waits for a round whose pairing
  includes its seat. A seat that is not on the roster, or is the odd one out,
  sits the round out.
- **A browser cannot win on time or analyse the game.** The relay owns the clock,
  so a flag settles through a bonded `timeout_claim` the winning miner opens — the
  page can only record it. And with no analysis engine, a network that samples
  quality gets `verdict: "unknown"`, which a validator may challenge.

The reference implementation of the same flow, in one process and with a
scripted opponent, is `python scripts/play_miner.py --on-chain`: it funds and
registers a human seat, seats it through the relay, co-signs the result and mines
the proof into a block. The browser needs that stack split into processes:

```bash
# 1. the node: serves the page and settles announced proofs
sccp --network devnet --data-dir data-devnet node run --web --mine \
     --miner-address <any-valid-address>
#    (the settlement miner tries each announced winner as the block's miner
#     first, so this address only matters for empty heartbeat blocks)

# 2. the relay: follows that node's epochs, signs clock receipts, allows the page
sccp --network devnet matchmaker run --config relay.json \
     --node http://127.0.0.1:8645 --witness-key relay.key \
     --origin http://127.0.0.1:8645

# 3. an opponent seat
sccp --network devnet miner run --key opponent.key \
     --relay http://127.0.0.1:8646 --node http://127.0.0.1:8645 \
     --engine-profile mock
```

`relay.json` is the deployment's relay config (beacon, roster, engines); `--node`
replaces its beacon and roster with each later epoch the chain opens. With the
relay on another origin, build with `VITE_SCCP_RELAY=http://127.0.0.1:8646` and
pass that origin to `--origin`. A node on `testnet` or `devnet` has a faucet; a
`mainnet` node has none, and the page shows the node's refusal.

## Checks

```bash
# in src/web
npm run typecheck
npm test                     # vitest: canonical JSON, bech32, keys, protocol,
                             # transactions, certificates, proofs, the verifier

# in the repository root
python scripts/make_web_vectors.py             # regenerate src/lib/vectors.json
python -m pytest tests/unit/test_web_vectors.py
```

The TypeScript ports in `src/lib` are held to the Python implementation by
generated vectors (`src/lib/vectors.json`), so a change to canonical JSON, the hash
domains, bech32, the match id, the colour draw, the game hash, a transaction
envelope, a certificate payload or a whole win proof fails on whichever side moved
first. `test_web_vectors.py` re-reads the same file against `sccp` itself, so the
document cannot go stale in the other direction either.

CI runs exactly those steps on every push and pull request — the `web` job in
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml), on Node 22 and 24,
with `npm ci` against the committed lock file — and uploads `dist/` as the
`web-dist` artifact.

## Where things live

```
src/
├── main.tsx           # createRoot + StrictMode
├── App.tsx            # theme and node providers, header, routes, lazy-loaded Play
├── components/        # Chrome (layout, offline banner), block and game tables
├── views/             # Dashboard, Blocks, BlockDetail, Games, GameDetail,
│                      # AccountView, Play, NotFound
├── lib/               # canonical JSON, hashing, bech32, keys, protocol ports,
│                      # transactions (tx), certificates, proofs, verifier,
│                      # relay client, formatters, generated vectors
├── api.ts             # typed /v1 client
├── types.ts           # every response shape
├── router.ts          # hash routing
├── theme.ts           # Material light and dark themes
└── useResource.ts     # fetch with abort, refresh interval, wall clock
```

## When it does not work

- **`no built explorer found`** (exit 2) — `sccp node run --web` found no
  `src/web/dist/index.html`. Run `npm ci && npm run build` in `src/web`, or point
  `SCCP_WEB_DIST` at the build you have.
- **Blank page, 404s for `/assets/*`** — a static host is serving the wrong
  directory; `dist/` must be the document root. The bundle's `base` is `./`, so a
  subdirectory works too.
- **"cannot reach the node at this origin"** — no node behind the page. Start one,
  or fix `SCCP_NODE` (dev and preview) / `VITE_SCCP_API` (a build); the panel's hint
  names the command and the variable.
- **"the faucet is disabled on mainnet"** — expected; a mainnet seat cannot be
  funded from the page.
- **"cannot reach the relay at http://127.0.0.1:8646"** — `#/play` talks to the
  relay directly. Start it (`sccp matchmaker run`), pass it an `--origin` that
  matches this page, and rebuild with `VITE_SCCP_RELAY` if it lives elsewhere.
- **"no session lists this seat yet"** — the relay has no session for this
  address. Check that the seat is on the *current* epoch's roster (the page shows
  the roster size), that its availability was revealed in epoch `E - 1`, and that
  the relay is following the same chain (`--node`).
- **`EADDRINUSE` on 8645** — something is already listening there; the compose
  stack in `deploy/` publishes 8645. Use `--port 8650`, then either open
  `http://127.0.0.1:8650/` or `SCCP_NODE=http://127.0.0.1:8650 npm run dev`.
- **Vite warns that `optimizeDeps.esbuildOptions` is deprecated** — harmless: Vite 8
  optimizes with Rolldown and still honours the option, which react-native-web's
  untranspiled JSX relies on.
- **A tab left open across a network switch** keeps showing the documents it
  fetched; the header's network id and tip are the ones to trust.

For the explorer's role in the wider system, see
[`docs/EXPLORE_THE_CHAIN.md`](../../docs/EXPLORE_THE_CHAIN.md).
