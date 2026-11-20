# The chain is not advancing

| | |
| --- | --- |
| Severity | SEV1 if it cannot recover on its own; SEV2 if a restart or a game settles it |
| Blast radius | new blocks, settlement, every proof and transfer in the mempool |
| First move | is `height` flat, is the mempool empty, and is anyone actually finishing a game |

A block is minted by a decisive chess game, not by the clock. `--mine-interval-seconds
60` paces blocks so the difficulty retarget stays neutral; it does not create them.
Ten quiet minutes are therefore normal between decisive results, and a page that
treats a five-minute gap as an incident will cry wolf every night. The question is
never "is the height moving right now", it is **"can this stack still produce a
block, and if not, which of the four inputs is missing"**.

## How to tell

```bash
NODE=https://app.proofofchess.com
curl -fsS $NODE/v1/status | jq '{height, tip_hash, peers, mempool}'
curl -fsS $NODE/v1/blocks | jq -r '.blocks[:8][] | "\(.height) \(.timestamp) \(.epoch)"'
curl -fsS $NODE/v1/difficulty | jq '{wins_required, seal_bits, retarget_interval, retarget_enabled}'
curl -fsS $NODE/v1/games | jq -r '.games[:5][] | "\(.result) plies=\(.plies) epoch=\(.epoch) variant=\(.variant)"'
```

Call `/v1/status` twice a minute apart and compare `height`. Then read the block
timestamps: if the last ten intervals are all near 30 s, the chain is healthy and
you are watching a gap between games. If the intervals before the gap were
trending up — 90 s, 200 s, 400 s — the stack is losing the ability to find a proof,
and that is the real symptom.

The four inputs a block needs, in the order they usually fail:

| Input | Where it shows |
| --- | --- |
| a game that ends decisively | `/v1/games` — every recent `result` is `draw`, or the rows have no `height` at all |
| a miner that announces the proof | `docker compose logs miner-1 miner-2 miner-3 miner-4` — a `proof` line at the end |
| a node that accepts it | node log: `mined height N: 1 proof(s)` when it lands, a rejection reason when it does not |
| a difficulty the roster can meet | `/v1/difficulty` — `wins_required` against how many games the seats win per epoch |


## The cases, and how to tell them apart

**1. Nobody is finishing a game.** The relay shows matches in progress and
`plies` climbing, but nothing ends. Chess960 and standard seats share the host;
look for a crashed engine, a seat that was never registered, or a clock that
cannot flag:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --tail=80 --no-color miner-1 miner-3 | grep -iE "engine|error|resign|flag|timeout"'
ssh sccp@proofofchess.com "curl -fsS http://127.0.0.1:8646/v1/matches | jq -r '.[] | \"\\(.status) \\(.san_moves | length) plies\"'"
```


The deliberate time forfeit in the traffic fleet takes about ten minutes of play
by design (`synthetic-miner-1` waits 36 s per move, so its 10+0 clock is gone
after roughly seventeen of its own moves); a forfeit that never arrives means the
witness quorum is not signing, so no bonded claim can be made. The relay must
report a `witness` address, not `null` — a relay without one signs no receipts
and no timeout can ever be claimed.

**2. Games end, but as draws.** Draws produce no proof, by consensus. If the
seats are too weak or too evenly matched, `--nodes-per-move 5000` at 60 plies of
resign tolerance can draw a run of games and the height simply stops for an hour.
This is the one case where the remedy is a parameter, not a repair: raise the
search or lower `--resign-after-plies` so games resolve, and treat it as a
capacity change rather than an incident.

**3. Proofs are announced and rejected.** The node log carries the reason, and the
reason is the diagnosis:

| Log line | Means |
| --- | --- |
| `stale` / `superseded` | the miner announced after another proof settled the block; harmless, and it retries |
| `not registered` / `bond` | the seat's key in `stack` no longer matches the registration the chain holds |
| `bad signature` / `invalid` | the miner and the relay disagree on the certificate — usually a mixed image |
| nothing at all | the node is not mining: check it was started with `--mine` |

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --tail=200 --no-color node | grep -iE "reject|invalid|stale|bond|registered"'
```

**4. The node is not mining.** `docker compose ps` says `Up`, the API answers, the
faucet pays, and no block is ever proposed because the process has no `--mine`
flag. This happens after someone runs `sccp node run` by hand to "check
something". Compare the running command to the compose file:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose config node | sed -n "/command:/,/environment:/p"'
docker inspect sccp-testnet-node-1 --format '{{json .Config.Cmd}}'
```

**5. The difficulty outran the roster.** The retarget reads block timestamps,
which are wall clocks. Blocks produced faster than `target_block_seconds` make the
chain look fast, the retarget raises `wins_required`, and once it is above what
four seats can win per epoch, no epoch closes and the chain cannot continue — a
halt that looks identical to "no games", except the difficulty document shows a
`wins_required` no roster could meet. `--mine-interval-seconds 30` exists to keep
this neutral; a hand-run node without it is the usual way in.

The remedy is the *pacing*, not the difficulty: restart the node with the compose
flags. Editing the stored difficulty or `wins_required` is a consensus change on a
running network, and every other node will reject the result.

**6. The clock moved.** A large NTP correction rewrites the next block's
timestamp; intervals go negative or enormous and the retarget misfires. Nothing in
the store is wrong, and a restart does not help:

```bash
ssh sccp@proofofchess.com 'timedatectl; chronyc tracking 2>/dev/null | head -5'
```

Let the schedule window re-target with a correct clock; note it in the incident log
so the odd interval is explained later.

## Two tips: is it a fork or two chains?

A stall is sometimes reported as "the mirror disagrees with the primary", and the
answer is decided by one comparison, in this order:

```bash
curl -fsS https://app.proofofchess.com/v1/blocks/0 | jq -r '.hash'
curl -fsS https://mirror.proofofchess.com/v1/blocks/0 | jq -r '.hash'
curl -fsS https://app.proofofchess.com/v1/blocks/1 | jq -r '.hash'
curl -fsS https://mirror.proofofchess.com/v1/blocks/1 | jq -r '.hash'
```

- **Genesis differs** → the mirror is on another chain. It is not a fork; a node
  verifies the genesis before adopting a peer's blocks, so this means the mirror
  was pointed at a different primary or kept data across a primary reset that
  changed the genesis. Stop serving it, rebuild it:
  [`MIRROR_OUTAGE.md`](MIRROR_OUTAGE.md).
- **Genesis equal, block 1 differs** → the primary was reset (the deploy workflow
  bootstraps a fresh chain) and the mirror still holds the old one. The mirror is
  stale, not forkable; same page.
- **Genesis and block 1 equal, tips differ** → the mirror is simply behind by up to
  one poll (30 s). Nothing to do. A divergence *above* block 1 with the same
  genesis is a real fork, and on a single-miner network it means two nodes were
  both mining: find the second `--mine` process before anything else.

```bash
curl -fsS https://app.proofofchess.com/v1/status | jq -r .tip_hash
curl -fsS https://mirror.proofofchess.com/v1/status | jq -r .tip_hash
```

## Prove the store is not the problem

Before blaming the roster, replay the chain. `sccp chain verify` re-validates every
stored block through full consensus and reports the tip; it is the one check that
separates "the block store is unsound" from "the stack cannot find a proof".

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain info'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain verify'
```

`info` prints height, tip, genesis and the current difficulty target; `verify`
exits non-zero on the first block that no longer validates, which is
[`DATA_LOSS.md`](DATA_LOSS.md) territory, not this page.

## Verifying a recovery

One block is not a recovery — the pacing is 30 s and the next game may take
minutes. Watch two blocks land, then confirm the invariants:

```bash
NODE=https://app.proofofchess.com
curl -fsS $NODE/v1/status | jq '{height, tip_hash}'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --since=15m --no-color node | grep "mined height"'
EPOCH=$(curl -fsS $NODE/v1/blocks | jq -r '.blocks[0].epoch')
curl -fsS "$NODE/v1/epochs/$EPOCH" | jq
```


The mirror should follow within a poll interval; the web app's blocks page is the
same data through nginx, so a page that still shows the old height with a moving
API is a proxy cache, not a chain.

## What makes it worse

- `docker compose down -v` "to get mining going": that is a chain reset, and it
  destroys `stack` too. Every seat loses its bond and every reader's history is
  gone.
- lowering `wins_required` or the target in the store to unstick an epoch — the
  next honest node rejects the chain from that block on;
- starting a second `--mine` process on another host with a copied key: two
  signers for one seat is equivocation, and the network cannot tell it from an
  attack;
- restarting the relay in the middle of a claimed timeout. The claim is a signed
  receipt; a relay restart does not lose it, but the witnesses' bundles are what
  settle it, and confusing the two sends you looking in the wrong log.

## Prevention

The generator that produces blocks is a chess engine, so the health of the stack
is a roster property. What makes a stall less likely and shorter:

- the external check from [`README.md`](README.md#detection), alerting on
  `height` unchanged for 30 minutes — long enough not to fire on a gap, short
  enough to be a page rather than an archaeology project;
- an alert if `/v1/status`'s `mempool.proofs` stays above zero for the same window:
  that is the signature of proofs being rejected rather than not produced, and it
  is the case that never recovers on its own;

- keeping `--mine-interval-seconds` equal to the network's `target_block_seconds`
  in every run of the node, hand-run ones included.

