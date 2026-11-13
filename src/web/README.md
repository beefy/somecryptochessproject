# `src/web` — the SCCP explorer

The browser client: a block explorer and a table where you can play a game and
watch the protocol sign it. React Native for Web rendered through Material Design
(`react-native-paper`), bundled by Vite, TypeScript in strict mode.

It talks to exactly one thing, the node's `/v1` JSON API. Every number on screen is
a field the node sent and every response is typed in [`src/types.ts`](src/types.ts).
There is no fixtures mode: with no node reachable, the pages say so rather than
invent data.

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
| `#/play` | the table: both seats played from one tab |

Routing lives in the hash, so the bundle works from any static path with no
server-side rewrite rule.

## The table needs a faucet

`#/play` generates two throwaway Ed25519 keys in the browser and funds the white
seat with `POST /v1/faucet`. A node on `testnet` or `devnet` has a faucet
automatically; a `mainnet` node has none and `--no-faucet` hides it — in both cases
the button shows the node's own refusal. Nothing else on that page talks to the
node, and it submits no proof: a game with no book opening and no opponent could
never verify.

## Checks

```bash
# in src/web
npm run typecheck
npm test                                       # vitest: canonical, bech32, keys, protocol

# in the repository root
python scripts/make_web_vectors.py             # regenerate src/lib/vectors.json
python -m pytest tests/unit/test_web_vectors.py
```

The TypeScript ports in `src/lib` are held to the Python implementation by
generated vectors (`src/lib/vectors.json`), so a change to canonical JSON, the hash
domains, bech32, the match id, the colour draw or the game hash fails on whichever
side moved first.

## Where things live

```
src/
├── main.tsx           # createRoot + StrictMode
├── App.tsx            # theme and node providers, header, routes, lazy-loaded Play
├── components/        # Chrome (layout, offline banner), block and game tables
├── views/             # Dashboard, Blocks, BlockDetail, Games, GameDetail,
│                      # AccountView, Play, NotFound
├── lib/               # canonical JSON, hashing, bech32, keys, protocol ports,
│                      # formatters, generated vectors
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
- **"the faucet is disabled on mainnet"** — expected; the table cannot be funded.
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
