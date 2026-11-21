# Testnet operations runbook

This is the short, operational companion to the design docs. It covers the
stack in `docker-compose.yml`: one node, one relay, the deterministic bootstrap,
two Stockfish miners and the two traffic generators that keep the chain in use.
For deployment setup and cost, see
[`../setup/TESTNET_DEPLOYMENT.md`](../setup/TESTNET_DEPLOYMENT.md). For the
cloud-side resource checklist, see [`../setup/GCP_RESOURCE_SETUP.md`](../setup/GCP_RESOURCE_SETUP.md).

When the stack is *broken* rather than merely busy, this page is the reference for
how it is put together; [`README.md`](README.md) is the triage index and
[`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) is the process around it. Reach for
those first when something is down: this page explains the stack, they explain
what to do about it.


## Health checks

From the VM or a local Compose stack:

```bash
curl -fsS http://127.0.0.1:8645/v1/status   # node height, tip, mempool
curl -fsS http://127.0.0.1:8646/v1/status   # beacon epoch, matches, round, witness
curl -fsS http://127.0.0.1:8646/v1/matches  # per-game status and ply count
# The witnesses are not published on the host; ask them from inside the network.
docker compose exec -T witness-2 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8647/v1/status').read().decode())"
docker compose ps
docker compose logs --tail=100 node bootstrap relay miner-1 miner-2 drip traffic
```

From anywhere, check the public node endpoint and the page:

```bash
curl -fsS http://app.proofofchess.com:8645/v1/status
curl -fsS http://app.proofofchess.com:8646/v1/status
curl -fsSI https://app.proofofchess.com/ | head -1   # Caddy TLS + app
curl -fsSI https://proofofchess.com/ | head -1      # Caddy TLS + landing
```

The hash routes (`#/wallet`, `#/play`, `#/dex`, `#/bridge`) are client-side, so
open them in a browser; `curl` always returns the same document.

Healthy after a deploy means:

- node `height` is at least 19 (the bootstrap chain);
- relay `matches` is at least 1, and its `epoch` follows the node (3 right after
  the bootstrap, 4 once the first post-bootstrap block settles);
- relay `/v1/status` reports a `witness` address, not `null`: a relay without a
  witness key signs no receipts, so no timeout can be claimed;
- each witness `/v1/status` reports `audited` matches with no `refusals` once a
  game has been flagged, and `quorum` 1 on testnet;
- `miner-1`/`miner-2` log an engine fingerprint (the Stockfish SHA-256), not an
  engine crash; `synthetic-miner-2` logs GNU Chess's binary hash;
- `drip` logs an occasional `dripped` line and `traffic` logs a `cycle N: ...`
  line every few minutes, including the extra-miner line
  (`miners: started ...; online: synthetic-miner-1, synthetic-miner-2`);
- when a game is decisive, the node log contains `mined height 20: 1 proof(s) ...`
  (draws produce no proof).

`deploy/check_testnet_stack.py` automates the first two checks. The deploy
workflow runs it inside the node container, then runs it a second time inside the
`traffic` container with `--fleet-only`, which waits for
`/stack/synthetic-fleet.json` to report both extra seats
(`--expect-seats 2 --expect-roles slow,gnuchess`). The generator's wallet
traffic is still watched rather than gated on: it paces itself in minutes.

For charts rather than raw JSON, `analysis/testnet_stats.ipynb` reads the same public
API: throughput and block intervals, the token distribution, the ten traffic wallets'
balances over time, and the wallet and miner graphs. It is read-only, caches its
snapshots under `analysis/cache/`, and is documented in
[`analysis/README.md`](../../analysis/README.md).

## Synthetic traffic

Two containers keep the chain busy, so a testnet nobody is watching still
exercises the paths a user takes:

- `drip` (`deploy/drip_testnet_traffic.py`) signs one faucet transfer whenever the
  pool is empty. An epoch is five blocks and the four-miner roster usually supplies
  eight availability/reveal transactions, but a restarted miner or a slow reveal
  can leave a boundary without enough ledger work; the drip closes that gap.
- `traffic` (`deploy/synthetic_testnet_traffic.py`) is ordinary usage on top of
  that. It creates its whole pool of ten wallets on startup and funds them in the
  first cycle. Each later cycle picks a random 2-10 of them, tops up the ones that
  are short, waits for its grants to be mined, sends 1-10 transfers between the
  pool's wallets, then bridges an HTLC and exercises the DEX when the wallets hold
  what those paths need, and finally idles for 1-5 minutes. Both side paths are
  best-effort: a chain with no C960 holder logs a DEX skip rather than failing the
  cycle, and `--no-bridge` / `--no-dex` turn either off. `--bridge-coins` and
  `--dex-coins` size those transactions, while `--dex-asset` pins the variant a
  newly created pool pairs with ZQWX.

Because `drip` refills the pool as soon as it drains, the generator never waits for
an empty mempool: it tracks the txids it submitted and waits for those to leave.

```bash
docker compose logs -f traffic   # one "cycle N: ..." or "burst N: ..." line per phase
docker compose logs --tail=20 drip
```

The `traffic` line reports how each grant was paid (`faucet` or `key`). The public
faucet allows five grants per client per hour and one per address, and the
generator is one container IP, so a wide cycle spends that budget and signs the
rest with the published faucet key -- the bypass `sccp faucet send` already uses.
That line is expected, not an error; the endpoint is retried on the next cycle.

One more segment can appear on that same line, and it is on by default in
`docker-compose.yml`:

```
cycle 7: 6 wallets, funded 6 (2 faucet, 4 key), 5 transfers, 11 confirmed | bridge: locked 1.00000000 ZQWX and claimed htlc 1a2b3c4d5e6f | dex: swapped 1.00000000 ZQWX for 0.99003187 C960 in pool 693fa9c228d7 | miners: started extra-miner-1; online: extra-miner-1
```

- `| bridge: ...` is an HTLC lock plus either a claim (odd cycles, revealing the
  preimage) or a refund (even cycles, after the timelock). A cycle that cannot
  fund the lock reports the skip instead.
- `| dex: ...` is a pool creation or a swap/add/remove against an existing pool.
  The generator needs a variant asset to seed the first pool, so an early chain
  with no C960 reports `dex: skipped (...)` without failing the cycle.
- `| miners: ...` comes from `deploy/synthetic_miner_creation.py`. The generator
  keeps the two extra seats derived from published seeds under
  `/data/synthetic-miners`, with fixed roles rather than a churn
  (`--no-extra-miner-churn`): `synthetic-miner-1` is deliberately **slow**
  (36 s a move, no resignation) so a 10+0 game is really flagged and the
  timeout path runs on live evidence, and `synthetic-miner-2` plays **GNU Chess**
  (`--depth 5`) so the roster is not six copies of one engine. They are real
  `sccp miner run` children: they register, commit availability, play what the
  relay assigns and announce their proofs, and the generator funds them through
  the same faucet gateway as the wallets. A seat that exits is repaired at the
  start of the next cycle, and
  `/stack/synthetic-fleet.json` is rewritten with what is seated.
  Capacity is measured, not assumed: half a core and 256 MiB per miner, one core
  reserved for the node and relay, so an `e2-standard-2` (2 vCPU, 8 GB) fits both
  seats. A host that
  cannot fit one logs
  `miners: no change; online: none (the host has ... leave room for 0)` and runs
  wallet traffic only; `--extra-miners 0` removes the seats entirely.

The deploy reads the same file. The workflow's "Verify the two extra seats" step
runs `check_testnet_stack.py` inside the traffic container and prints the report
as it changes:

```bash
docker compose exec -T traffic python /app/deploy/check_testnet_stack.py \
    --fleet /stack/synthetic-fleet.json --fleet-only \
    --expect-seats 2 --expect-roles slow,gnuchess --wait-seconds 1800
```

```text
waiting up to 1800s for 2 extra seat(s) (roles: slow, gnuchess) in /stack/synthetic-fleet.json
30s: seating, 0/2 seat(s) online (roles: none)
90s: online, 2/2 seat(s) online (roles: slow, gnuchess)
extra miners ready: 2 extra seat(s) online: ...
```

`phase` is `starting` before the first cycle, `seating` while the seats register,
`online` once they run and `starved` when the budget refused them (`failed`
carries what could not start, `updated` is when the file was written). A cause
waiting cannot clear -- a budget below two seats, or a missing engine binary --
ends the check at once instead of spending the whole wait, and a check that times
out while still `seating` is telling you the registrations never confirmed, which
is a chain problem and not a seat problem.

The loop cannot drain the faucet. Transfers run between the pool's own wallets, so
the coins circulate; the faucet is tapped only for new wallets (at most
`--max-wallets`, ten by default, at 10 coins each) and for fees (1,000 units per
transfer). Against the genesis faucet's 1,000,000 coins that is a rounding error,
and the generator reuses its wallets instead of minting new ones every cycle.
Because of that reuse, its addresses are derived from fixed seeds: nothing has to
be persisted, which is what makes it safe under `docker compose down -v`.

Keep it in the same Compose project as the chain. Unlike the explorer deploy -- a
separate workflow precisely because it must survive the chain being reset -- a
traffic generator that outlived `down -v` would send with nonces and balances that
no longer exist.

## Burst traffic

Every twelfth cycle (an hour or so at the default idle) `traffic` stops being a
trickle and fires a burst: it tops the pool up so each wallet can cover its
share, then queues 80 transfers over five waves, one second apart, and only then
waits for them to be mined. The point is a deep mempool across several blocks
and the ordering and eviction paths that a handful of transactions never reach.
The burst signs with the published operator key instead of the faucet endpoint:
those addresses already took their one public grant, and the endpoint would
refuse to fund them again. Ordinary cycles still try the faucet first.

The burst's line starts with `burst 12:` instead of `cycle 12:`. A burst that
cannot finish logs `burst after cycle 12: ...` on stderr; the ordinary cycle has
already completed and counted. The knobs are `--burst-every N` (`0` or
`--no-burst` disables it), `--burst-transfers`, `--burst-rounds`,
`--burst-interval-seconds`, `--burst-grant-coins` and
`--burst-min-balance-coins`. The stats notebook marks the burst windows and
groups consecutive minutes above `max(5, median + 6 * scaled MAD)` into one
window, so a burst reads as one event instead of five. The top-up needs the
operator key; without it the burst reports the failure instead of sending with
empty wallets.

## Restart and redeploy


A full redeploy intentionally wipes the chain and relay state. The bootstrap can
only start from height 0 (or verify an already-complete bootstrap chain):

```bash
docker compose down -v --remove-orphans
SCCP_IMAGE=ghcr.io/OWNER/REPO:testnet docker compose pull
SCCP_IMAGE=ghcr.io/OWNER/REPO:testnet docker compose up -d --no-build
```

If only one process is unhealthy, restart it without dropping volumes:

```bash
docker compose restart node
docker compose restart relay
docker compose restart miner-1
```

Restarting the node replays SQLite from the last snapshot. Restarting the relay
replays its JSON store. Do not delete `/stack/relay.json`: the bootstrap writes it
from the chain and the relay needs the matching beacon and roster.

## The stack will not start

`docker compose up` reporting `container ... is unhealthy` with every other
service `not started` means the node exited before it bound the port: the
dependents are a symptom, not the fault. Read the node log first:

```bash
docker compose logs --tail=40 node
```

An immediate container `Error` and a log line like

```text
error: 'sccpt1q...' is not a valid testnet address
```

is a **stale address literal in the compose file**, not a chain problem. `node
run` validates `--miner-address` before it starts serving, so an address written
before an address-scheme change stops the whole stack. Deploy the compose file
from the same commit as the image (the deploy workflow uploads it) and reset:

```bash
docker compose down -v --remove-orphans
SCCP_IMAGE=ghcr.io/OWNER/REPO:testnet docker compose up -d --no-build
```

`tests/unit/test_deploy_compose.py` pins that literal to the address the `init`
service writes for stack miner-1, and `tests/unit/test_docs_addresses.py` parses
every address the guides quote, so this class of failure fails a test rather than
a deploy.

## Faucet

The public endpoint exists only on `testnet` and `devnet`. Defaults are:

| Limit | Default |
| --- | --- |
| Grant | 1,000 test coins |
| Maximum per request | 1,000 test coins |
| Same address cooldown | 3,600 seconds (one hour) |
| Client grants | 5 per 3,600-second window |

`GET /v1/faucet` returns the live policy. `POST /v1/faucet` takes
`{"address": "sccpt1...", "amount": 1000}` and returns the txid. A refused
address or client gets HTTP 429 with `Retry-After` in seconds. Invalid addresses
and out-of-range amounts get HTTP 400.

The limits are **in memory**. Restarting the node forgets every cooldown and
client history. That is acceptable for a valueless testnet; do not treat the
faucet as a security boundary. The published faucet key is also available to the
offline `sccp faucet send` command, so an operator can bypass the HTTP limits
when recovering a test wallet.

`--no-faucet` disables the endpoint on a node (it answers HTTP 404). Use it when a
deployment must not give away coins.

For a copy-paste walkthrough that creates a keyfile, calls the faucet and checks
the resulting balance, see
[`CREATE_A_TESTNET_WALLET.md`](../guides/CREATE_A_TESTNET_WALLET.md).

## Mining and the bootstrap window

`node run --mine --miner-address ADDRESS` starts a background settler. It does
not search for games, own a bond, or invent proofs: it takes an eligible proof
already announced to the mempool and builds the next block around it.

The flag that matters for a pre-built chain is:

```text
--mine-bootstrap / --no-mine-bootstrap
```

Bootstrap blocks may be empty up to `bootstrap_end_height` (height 19 on
testnet). A node started on a fresh `--data-dir` can use the default
`--mine-bootstrap` to extend that window. A deployment that feeds the node a
pre-built bootstrap chain (this stack) must pass `--no-mine-bootstrap`; otherwise
the background miner can race the bootstrap and build a competing block at the
same height.

The other flag a deployment sets is the settler's pace:

```text
--mine-interval-seconds 30
```

A block's timestamp is the wall clock, and the difficulty schedule reads those
timestamps every `retarget_interval` blocks (heights 10, 20, 30 on testnet),
comparing their span with `target_block_seconds * (retarget_interval - 1)`. A
settler that builds a block on every poll makes a 30-second chain look
one-second fast, and the retarget answers by raising `wins_required` -- one block
then has to carry two verified wins by the same miner, which the roster cannot
supply in one epoch, so the chain stops there. `30` is testnet's
`target_block_seconds`: one block per half minute keeps the retarget window in its
neutral band and `wins_required` at `1`. `GET /v1/difficulty` reports the current
values and the retarget history. The bootstrap script paces its synthesized
timestamps the same way.

After height 19 a block needs a proof, or exactly one transaction when it is a
proofless heartbeat block. The stack's
miners commit and reveal availability every epoch, so those transactions carry
the chain forward even when a round ends in a draw; without them (a miner started
without `--nonce-secret-file`) the chain can only advance when somebody plays a
decisive game. The stack's `drip` service covers the remaining gap: an epoch is
five blocks and the roster generates four transactions, so
`deploy/drip_testnet_traffic.py` submits one ordinary faucet transfer whenever the
pool is empty.

## Stuck chain or mempool

- **Height 19 after a fresh bootstrap, with the mempool filling up:** blocks pace
  on the chain's own timestamps, not on the wall clock alone. `node run --mine`
  builds the next block only once
  `clock() - tip_timestamp >= --mine-interval-seconds`, so a tip stamped *ahead*
  of the clock freezes the chain until real time catches up -- and nothing can
  confirm in that window: registrations, availability reveals and grants all wait
  in the pool. A bootstrap that walked forward from `now` produced exactly that
  gap (a full window, 9.5 minutes on testnet, which is what left a fresh deploy
  sitting at height 19 with its traffic bot's seats unregistered); the bootstrap
  now backdates its window to end at the wall clock. Compare `date +%s` with the
  tip's `timestamp` in `GET /v1/blocks?limit=1`: the difference is how long the
  wait still has to run. The same symptom appears if the host clock is stepped
  backwards (NTP) after a block was stamped.
- **Height 19 forever, relay games active:** check the miners are running and
  their logs show moves. CPU-starved miners make slow progress; the 10+0 clock is
  real even on testnet.
- **Height 19 forever, relay games drawn:** draws earn nothing, so no proof
  arrives. The miners' availability/reveal transactions are the heartbeat that
  still produces blocks; if `GET /v1/mempool` is empty, restart the miners with
  `--nonce-secret-file` (section 4.3 of
  [`RUN_A_TESTNET_MINER.md`](../setup/RUN_A_TESTNET_MINER.md)).
- **The tip stops mid-epoch with an empty mempool:** a block needs a proof or a
  transaction, and a proofless block takes exactly one transaction, so an epoch
  of `epoch_length` blocks needs that much ledger work to end. The two stack
  miners supply four items per epoch (availability plus reveal each), so one
  decisive game -- or a timeout claim, which is a transaction too -- carries the
  epoch; a roster of three or more miners is self-sustaining. The stack's `drip`
  service refills an empty pool with one ordinary transfer, so a pool that is
  empty *and* stays empty means the drip is not running (`docker compose logs -f
  drip`). `GET /v1/mempool` shows which proof ids and tx ids are still waiting,
  and the relay log shows rounds that never end.
- **The tip stops with `wins_required` above what the roster can win:** check
  `GET /v1/difficulty`. The chain was mined faster than `target_block_seconds`,
  the retarget raised `k` at an epoch boundary, and one block now has to carry `k`
  wins by the same miner at the same height. Blocks with transactions still
  advance (a heartbeat block needs no proof) but no proof can settle, so the
  stack has to be mined at the target pace: `--mine-interval-seconds 30`. `k`
  only moves at an epoch boundary and only outside the neutral band, so a chain
  that already raised it needs blocks averaging slower than 4/3 of the target to
  bring it back down; on a stack you control, `docker compose down -v` and a
  fresh bootstrap is quicker.
- **Every game in the epoch finished as a draw:** a draw has no winner, so it
  yields no proof and leaves the pool with only the roster's own transactions.
  Two engines of comparable strength draw long games, so give the miner
  `--resign-after-plies 60`: the side to move resigns, both seats co-sign a
  `resignation` result, and the winner's proof carries the epoch. Check with
  `GET /v1/matches`: `"termination": "draw"` on every session is the symptom,
  and `--resign-after-plies` on either miner is the fix.
- **The relay keeps matching epoch N while the node is on a later epoch:** start
  the relay with `--node http://<node>:8645`. It logs
  `relay moved to epoch N+1 with 2 matchable miner(s)` and swaps in the chain's
  beacon and roster; proofs for the old epoch are refused as
  `epoch ... is not matchable`.
- **A miner's proofs are all refused with "committed no availability":** that
  miner was not running two epochs earlier, so it has no commitment for this
  epoch. Availability is committed `availability_lead` epochs ahead; the miner
  rejoins the roster by itself one epoch after it starts committing.
- **A transaction never confirms:** check `GET /v1/mempool` and the node log.
  The pool expires transactions after the network's TTL; resubmit after the
  expiry if the sender still has the nonce.

## Relay, beacon and drand

The testnet relay's beacon is derived from the on-chain availability reveals, not
from drand. A relay error such as `epoch 3 was never opened` means it was started
against a chain that has not run the bootstrap, or with a stale `relay.json`.
Rebuild it with:

```bash
python deploy/bootstrap_testnet_stack.py --stack /stack --node http://node:8645
```

The bootstrap is idempotent when the node is already past height 19: it rebuilds
the deterministic chain in memory and rewrites the relay config without posting
duplicate blocks.

## Clock witnesses and time forfeits

Three witnesses are registered in the bootstrap chain: the relay itself
(`witness-1`) and two independent services (`witness-2` on 8647, `witness-3` on
8648). All three bond one coin and record the same hybrid key files in
`/stack/keys/witness-*.key`/`.pub`.

```bash
docker compose logs -f relay witness-2 witness-3
curl -fsS http://127.0.0.1:8646/v1/status | jq '{witness, matches, open}'
# Neither witness publishes a host port: they are evidence sources for the
# miners inside the compose network, not a public API.
docker compose exec -T witness-2 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8647/v1/status').read().decode())" | jq '{address, polls, matches, audited, refusals}'
docker compose exec -T witness-2 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8647/v1/matches').read().decode())" | jq '.[] | {status, flagged, audited, audit}'
```

What each piece does:

- the **relay** signs a receipt per accepted move and a flag receipt when a clock
  reaches zero (`--witness-key`). This is what makes a `timeout_claim` possible
  at all; without the key, receipts are unsigned and every claim is refused.
- the **two witnesses** poll the relay every two seconds, verify the relay's
  signature, re-run the protocol's clock arithmetic, and co-sign each receipt and
  the evidence checkpoint with their own keys. They serve the bundle at
  `GET /v1/matches/{id}/receipts`, so a miner can settle a timeout even if the
  relay cannot produce its own bundle. Their `audit` endpoint answers whether the
  bundle they signed verifies under the protocol's verifier.

Race and reporting:

- **the relay refuses to open a round** with a message like `epoch 12 has 2
  active clock witness(es) and needs 3; refusing to pair`. That is the pairing
  gate, not a bug: a flag signed by fewer witnesses than `clock_witness_quorum`
  can never settle a `timeout_claim`, so the relay stops instead of producing
  unplayable games. `curl -fsS http://127.0.0.1:8645/v1/status | jq .witnesses`
  shows the count it is reading. Bring a witness back (`docker compose up -d
  witness-2 witness-3`) or re-run the stack bootstrap, which re-posts the
  `register_witness` transactions, and the next poll opens the round by itself.
  Never "fix" this on testnet or mainnet with `--allow-unwitnessed-rounds`; that
  flag is for a devnet, which runs no witness service at all.
- **a claim the chain refuses** means quorum was not met or a receipt did not
  verify: read the miner log for `the match was flagged but no witness has a
  complete bundle` (no witness polled the flag) or `evidence does not verify`
  on the witness itself.
- **`refusals` mentioning `unsigned`** means the relay was restarted without
  `--witness-key`. Fix the config; the witnesses keep auditing but their bundles
  are the only attestations.
- **`refusals` mentioning `no registered public key`** means a witness was given
  the wrong `--relay-witness-key-file`.

The deliberate time forfeit comes from the extra fleet: `synthetic-miner-1`
waits 36 seconds before every move, so a 10+0 clock runs out after about
seventeen of its own moves. Do not expect a flag before roughly ten minutes
after the match starts. When it happens the opponent files a bonded claim and
the node settles it; check with:

```bash
docker compose logs --tail=50 traffic | grep -E 'claim|slow'
docker compose logs --tail=50 miner-1 miner-2 | grep -i timeout
curl -fsS http://127.0.0.1:8645/v1/status | jq '.mempool'
```

## The read-only mirror

A second VM (`deploy/edge/`) runs a node that pull-syncs this chain and serves it
over HTTPS with GET-only access. It is a separate Compose project:

```bash
curl -fsS "https://mirror.example.com/v1/status" | jq '{height, tip_hash, peers}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://mirror.example.com/v1/tx" -d '{}'
```

`405` is correct: the edge refuses every write by design. A mirror whose `height`
stops advancing is failing to reach `SCCP_PRIMARY_URL`; its node log names the
peer and the reason. The mirror is disposable — delete `node-data` and let one
sync rebuild it.

`.github/workflows/deploy-edge.yml` redeploys the mirror after every successful
`deploy-testnet-miners` run, but only when `vars.EDGE_DEPLOY_HOST` is set: with
no variable the mirror is simply absent, and the primary's deploy gate never
waits on it. That workflow also repairs the case this stack exists for: the
primary's redeploy resets its chain, so the workflow compares block 1 and, when
the mirror is holding the old chain, deletes the node's data (never the
certificate) and lets one sync rebuild it. CI still does not create the VM, its
static address, its firewall rules or its `A` record — those are the `gcloud`
block in [`deploy/edge/README.md`](../../deploy/edge/README.md) — so a missing
mirror VM is expected on a stack whose owner never ran it. When the workflow is
green it has already checked the mirror's genesis hash against the primary's and
waited for its height to reach the primary's; a mirror that is green there and
stale later is up but not polling, which is the log's story.

The two extra miner seats are children of the `traffic` container on the primary
host; the mirror holds no miners and no relay.

## Engine crashes

The miners use `/usr/games/stockfish` from the image. A crash normally appears as
`cannot start engine` or a `UCI` error in one miner's log. Restart that miner; if
it repeats, rebuild the image and confirm the binary runs:

```bash
docker compose exec -T miner-1 /usr/games/stockfish <<<'uci'
```

The engine fingerprint is part of the match certificate. Changing the image's
Stockfish version changes that fingerprint, so a redeploy must also rebuild the
bootstrap registration (the workflow's `down -v` does this). Do not mix images
between the bootstrap, the relay config and the miners.

## Reorgs

Reorgs are possible on a shared testnet but unusual on this single-node stack.
The node keeps orphaned blocks out of the main chain, rebases the mempool after a
reorg and rebroadcasts transactions and proofs from the discarded branch. If a
relay still references a match whose proof was orphaned, it will re-announce the
proof when the miner retries; no operator action is normally needed. The
restart, partition, reorg, disk-pressure and clock-skew drills are automated in
`tests/e2e/test_testnet_resilience.py`.
