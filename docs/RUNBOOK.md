# Testnet operations runbook

This is the short, operational companion to the design docs. It covers the
stack in `docker-compose.yml`: one node, one relay, the deterministic bootstrap,
two Stockfish miners and the two traffic generators that keep the chain in use.
For deployment setup and cost, see
[`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md). For the cloud-side resource
checklist, see [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md).

## Health checks

From the VM or a local Compose stack:

```bash
curl -fsS http://127.0.0.1:8645/v1/status   # node height, tip, mempool
curl -fsS http://127.0.0.1:8646/v1/status   # beacon epoch, matches, round
curl -fsS http://127.0.0.1:8646/v1/matches  # per-game status and ply count
docker compose ps
docker compose logs --tail=100 node bootstrap relay miner-1 miner-2 drip traffic
```

From anywhere, check the public node endpoint:

```bash
curl -fsS http://testnet.somecryptochessproject.com:8645/v1/status
curl -fsS http://testnet.somecryptochessproject.com:8646/v1/status
```

Healthy after a deploy means:

- node `height` is at least 19 (the bootstrap chain);
- relay `matches` is at least 1, and its `epoch` follows the node (3 right after
  the bootstrap, 4 once the first post-bootstrap block settles);
- `miner-1`/`miner-2` log an engine fingerprint (the Stockfish SHA-256), not an
  engine crash;
- `drip` logs an occasional `dripped` line and `traffic` logs a `cycle N: ...`
  line every few minutes;
- when a game is decisive, the node log contains `mined height 20: 1 proof(s) ...`
  (draws produce no proof).

`deploy/check_testnet_stack.py` automates the first two checks. The deploy
workflow runs it inside the node container and then prints the `traffic` log;
the generator paces itself, so it is watched rather than gated on.

## Synthetic traffic

Two containers keep the chain busy, so a testnet nobody is watching still
exercises the paths a user takes:

- `drip` (`deploy/drip_testnet_traffic.py`) signs one faucet transfer whenever the
  pool is empty. An epoch is five blocks and a two-miner roster only produces four
  availability/reveal transactions, so without this the chain would run a block
  short at an epoch boundary that has no decisive game.
- `traffic` (`deploy/synthetic_testnet_traffic.py`) is ordinary usage on top of
  that. It creates its whole pool of ten wallets on startup and funds them in the
  first cycle. Each later cycle picks a random 2-10 of them, tops up the ones that
  are short, waits for its grants to be mined, sends 1-10 transfers between the
  pool's wallets, waits for those to be mined, then idles for 1-5 minutes.

Because `drip` refills the pool as soon as it drains, the generator never waits for
an empty mempool: it tracks the txids it submitted and waits for those to leave.

```bash
docker compose logs -f traffic   # one "cycle N: ..." line per cycle
docker compose logs --tail=20 drip
```

The `traffic` line reports how each grant was paid (`faucet` or `key`). The public
faucet allows five grants per client per hour and one per address, and the
generator is one container IP, so a wide cycle spends that budget and signs the
rest with the published faucet key -- the bypass `sccp faucet send` already uses.
That line is expected, not an error; the endpoint is retried on the next cycle.

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
[`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md).

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
--mine-interval-seconds 60
```

A block's timestamp is the wall clock, and the difficulty schedule reads those
timestamps every `retarget_interval` blocks (heights 10, 20, 30 on testnet),
comparing their span with `target_block_seconds * (retarget_interval - 1)`. A
settler that builds a block on every poll makes a 60-second chain look
two-second fast, and the retarget answers by raising `wins_required` -- one block
then has to carry two verified wins by the same miner, which a two-miner roster
cannot supply, so the chain stops there. `60` is testnet's
`target_block_seconds`: one block per minute keeps the retarget window in its
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

- **Height 19 forever, relay games active:** check the miners are running and
  their logs show moves. CPU-starved miners make slow progress; the 30+0 clock is
  real even on testnet.
- **Height 19 forever, relay games drawn:** draws earn nothing, so no proof
  arrives. The miners' availability/reveal transactions are the heartbeat that
  still produces blocks; if `GET /v1/mempool` is empty, restart the miners with
  `--nonce-secret-file` (section 4.3 of
  [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md)).
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
  stack has to be mined at the target pace: `--mine-interval-seconds 60`. `k`
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
proof when the miner retries; no operator action is normally needed.
