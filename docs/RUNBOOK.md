# Testnet operations runbook

This is the short, operational companion to the design docs. It covers the
stack in `docker-compose.yml`: one node, one relay, the deterministic bootstrap
and two Stockfish miners. For deployment setup and cost, see
[`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md).

## Health checks

```bash
curl -fsS http://127.0.0.1:8645/v1/status   # node height, tip, mempool
curl -fsS http://127.0.0.1:8646/v1/status   # beacon epoch, matches, round
curl -fsS http://127.0.0.1:8646/v1/matches  # per-game status and ply count
docker compose ps
docker compose logs --tail=100 node bootstrap relay miner-1 miner-2
```

Healthy after a deploy means:

- node `height` is at least 19 (the bootstrap chain);
- relay `epoch` is 3 and `matches` is at least 1;
- `miner-1`/`miner-2` log an engine fingerprint (the Stockfish SHA-256), not an
  engine crash;
- when a game is decisive, the node log contains `mined height 20: 1 proof(s) ...`
  (draws produce no proof).

`deploy/check_testnet_stack.py` automates the first two checks. The deploy
workflow runs it inside the node container.

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

After height 19 the settler only acts when a proof is pending. If the node stays
at 19 and the relay shows finished **draws**, that is expected: draws earn
nothing, so there is no proof to settle. The next round (every 60 seconds, up to
the epoch quota) gives the miners another game.

## Stuck chain or mempool

- **Height 19 forever, relay games active:** check the miners are running and
  their logs show moves. CPU-starved miners make slow progress; the 30+0 clock is
  real even on testnet.
- **Height 19 forever, relay games drawn:** no proof exists. Wait for the next
  round or redeploy; draws are deliberate no-ops.
- **Height 20 and no further blocks:** the epoch's three games are exhausted on
  this bootstrap. Redeploy resets the demonstration chain.
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
