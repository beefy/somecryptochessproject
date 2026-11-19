# Production operations

This folder is for the days after the deploy worked: the chain is up, it holds
value or someone depends on it, and something is now wrong. If the thing does
not exist yet — a new VM, a first certificate, a workflow that has never run —
that is [`../setup/`](../setup), not here.

Every page below follows the same shape, because the order matters more than the
wording: **how to tell it is this**, **which sub-case it is**, **what to do right
now**, **what to do so it does not happen again**, **how to prove it is fixed**.
A page that says "restart it" without saying how to tell whether the restart
helped is not a runbook.

## What is actually running

| Piece | Where | If it dies |
| --- | --- | --- |
| the chain: node, relay, witnesses, four miners, bootstrap, traffic | one GCP VM, `docker-compose.yml`, project `sccp-testnet`, `/opt/sccp-testnet` | no new blocks, no settlement, no faucet |
| the web origins: nginx on `8080`/`8081` behind Caddy on `80`/`443` | same VM, `deploy/explorer/`, project `sccp-explorer` | the pages 404 or the certificate errors; the API is unaffected |
| the read-only mirror | its own `e2-micro`, `deploy/edge/`, project `sccp-edge`, `/srv/sccp-edge` | reads keep working from the primary; nothing else notices |
| the images | `ghcr.io/chessproofofwork/somecryptochessproject:testnet` | nothing today, until the next deploy needs to pull |

Two of these are cheap to lose and one is not. The mirror is disposable — one
sync rebuilds it. The web origins hold no state but a certificate. The primary
holds the chain **and the `stack` volume**, which is the only copy of the miner,
relay, witness and faucet keys and the passphrase they are wrapped with. A run
that wipes volumes has destroyed identities, not just data; see
[`DATA_LOSS.md`](DATA_LOSS.md).

## Which page

| Symptom | Page |
| --- | --- |
| the site or the API does not answer at all | [`PRIMARY_DOWN.md`](PRIMARY_DOWN.md) |
| the API answers but `height` has not moved in ten minutes | [`CHAIN_STALLED.md`](CHAIN_STALLED.md) |
| a container is gone, a volume is empty, the node refuses to open its database | [`DATA_LOSS.md`](DATA_LOSS.md) |
| certificate expired, TLS handshake fails, `curl` exit 35, ACME errors | [`TLS_AND_DNS.md`](TLS_AND_DNS.md) |
| a deploy went red, or went green and changed nothing | [`DEPLOY_FAILURES.md`](DEPLOY_FAILURES.md) |
| the mirror is stale or down, the primary is fine | [`MIRROR_OUTAGE.md`](MIRROR_OUTAGE.md) |
| the faucet is empty, a key is missing, a key may have leaked | [`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md) |
| unexpected traffic, an open port, a compromised host or credential | [`SECURITY_INCIDENT.md`](SECURITY_INCIDENT.md) |
| "how do I check the stack", restart, faucet and reorg mechanics | [`RUNBOOK.md`](RUNBOOK.md) |

If the symptom is not in the table, start at [`RUNBOOK.md`](RUNBOOK.md#health-checks)
and decide from there; the triage order below is the same for all of them.

## Severity

The number decides how much you are allowed to break while fixing it. Say it out
loud when you declare an incident, because it is what tells a second person to
stop and help.

| Severity | Definition | Response |
| --- | --- | --- |
| SEV1 | consensus is wrong: two tips, a chain that cannot continue, value at risk, or a key known to be compromised | all other work stops; one person on the fix, one on evidence and comms; change freeze |
| SEV2 | the primary cannot serve or cannot advance, but data is intact | fix now, single operator, keep every command in the incident log |
| SEV3 | a single public surface is down or wrong: TLS, the web origins, the faucet | fix within the day; the chain does not wait |
| SEV4 | cosmetic or redundant: the mirror, a stale page, a dashboard | fix when convenient; say so in the log |

A green-looking network is not evidence of SEV4: the first move is always to
establish whether the *chain* is advancing, not whether pages render.

## The first five minutes

```bash
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash, peers}'
curl -fsS https://mirror.proofofchess.com/v1/status | jq '{height, tip_hash, peers}'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose ps -a'
ssh sccp@proofofchess.com 'df -h / && df -i / && free -m'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --tail=50 --no-color node relay'
```

Four answers split the whole problem space:

1. **Does the API answer?** No → [`PRIMARY_DOWN.md`](PRIMARY_DOWN.md).
2. **Is `height` moving?** Compare two calls a minute apart. Flat →
   [`CHAIN_STALLED.md`](CHAIN_STALLED.md).
3. **Do the primary and the mirror agree on the genesis hash?** They must, always.
   They disagree → the mirror joined another chain and is lying to readers; stop
   serving it ([`MIRROR_OUTAGE.md`](MIRROR_OUTAGE.md)).
4. **Do they agree on block 1 but not on the tip?** The mirror is just behind;
   harmless, and expected for up to a poll interval.

Disk and memory belong in the first five minutes because both look like
everything else: a full disk makes SQLite fail in ways that read like corruption,
and a 2 vCPU host under memory pressure kills a miner, which reads like a
stalled chain.

## What you may always do

- restart one service (`docker compose restart node`), or the whole stack
  (`docker compose up -d`) — a restart replays `chain.sqlite` and reconnects
  every seat;
- roll the web release symlink back ([`DEPLOY_FAILURES.md`](DEPLOY_FAILURES.md));
- delete the mirror's `node-data` volume and resync it;
- stop the mirror, or block it in DNS, while you work;
- re-run a failed workflow, once you know which step failed.

## What you may never do during an incident

- `docker compose down -v` on the primary. The `-v` deletes `node-data` **and
  `stack`**: the chain resets to the bootstrap and every key is gone. The deploy
  workflow does this on purpose, on a network whose data is worthless. During an
  incident it turns a SEV2 into a SEV1.
- delete `chain.sqlite` to "clear a lock" or a corrupt-row error, before
  [`DATA_LOSS.md`](DATA_LOSS.md) says it is the last resort, and before there is a
  copy on another disk;
- change consensus parameters (difficulty, `wins_required`, bond, epoch length)
  to make a stall go away;
- rotate the genesis on a network other people have used — the hash is the
  network's identity, and their nodes will refuse it;
- move a key from `stack` to a second host to "share the load". Two nodes signing
  for one seat is equivocation, and on a value-bearing chain it is slashable;
- restart Caddy in a loop while ACME fails. Five duplicate certificates a week
  per name is Let's Encrypt's limit and it does not reset when you do.

## Detection

There is no pager today. What exists is three checks that run only when something
deploys: the stack smoke test in `deploy-testnet-miners.yml`, the mirror check in
`deploy-edge.yml`, and the operator pages in `README.md`. Everything else is a
person noticing, which is fine for a testnet and is not fine for the network this
folder is written for.

Before mainnet, add the two cheap ones: an external uptime check on
`https://app.proofofchess.com/v1/status` that asserts `height` **increased**
(not that the page loaded), and the same on the mirror with a genesis-hash
comparison. They catch the two failures a status page cannot: a frozen node and a
mirror serving a different chain.

## Reading order for a new operator

1. [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) — roles, the order of
   operations, and what a postmortem has to contain.
2. [`RUNBOOK.md`](RUNBOOK.md) — how the stack is put together and what healthy
   looks like.
3. The scenario pages, in the order a real outage tends to visit them:
   primary down, chain stalled, data loss.
4. The setup page for the thing you are about to touch, so you know what the
   command was *supposed* to do: [`../setup/`](../setup).

