# The mirror is stale or down

| | |
| --- | --- |
| Severity | SEV4 — the primary is unaffected; this is a loss of redundant reads |
| Blast radius | `mirror.proofofchess.com` only |
| First move | compare genesis hashes, then block 1, then the tip — in that order |

The mirror is a second failure domain, not a backup and not half of a cluster. It
pull-syncs the primary every 30 seconds, serves GET only, holds no faucet key and
mines nothing. Everything on this page is therefore *fixable by throwing the
replica away*, which is not true of anything else in this folder.

## How to tell

```bash
curl -fsS https://mirror.proofofchess.com/v1/status | jq '{height, tip_hash, peers}'
curl -fsS https://app.proofofchess.com/v1/status   | jq '{height, tip_hash}'
curl -fsS https://mirror.proofofchess.com/v1/blocks/0 | jq -r .hash
curl -fsS https://app.proofofchess.com/v1/blocks/0   | jq -r .hash
curl -fsS https://mirror.proofofchess.com/v1/blocks/1 | jq -r .hash
curl -fsS https://app.proofofchess.com/v1/blocks/1   | jq -r .hash
```

| Observation | Meaning | Page |
| --- | --- | --- |
| `curl: (7)` or timeout | Caddy or the node is not running, or the VM is off | this page, case 1 |
| `(35)` TLS error | certificate on the edge host | [`TLS_AND_DNS.md`](TLS_AND_DNS.md) |
| genesis differs from the primary | the mirror is on another chain and must not serve reads | this page, case 3 |
| genesis equal, block 1 differs | the primary was reset and the mirror kept the old chain | this page, case 3 |
| genesis and block 1 equal, tip behind | normal, up to one poll | nothing |
| tip equal, but `height` stuck for hours with the primary advancing | the poll loop is broken, not slow | this page, case 2 |

A mirror that is behind by a few blocks is working. A mirror that is behind by
thousands is not "catching up" — with a 30-second poll and a chain that adds one
block a minute, a replica that misses an hour has to fetch thousands of blocks in
sequence, and the honest answer is to wipe it rather than watch it.

## Case 1: nothing answers

```bash
gcloud compute instances describe sccp-edge --zone=<zone> \
  --format='value(status,networkInterfaces[0].accessConfigs[0].natIP)'
ssh -i ~/.ssh/sccp_edge_ed25519 sccp@136.119.76.215 'cd /srv/sccp-edge && docker compose ps -a'
```

`TERMINATED` → start the instance; the reserved address `sccp-edge-ip` keeps DNS
and the certificate valid across it. Running but nothing on 443 → Caddy is down
or the certificate volume is gone ([`TLS_AND_DNS.md`](TLS_AND_DNS.md)). Node
container restarting → its log says why, and on an `e2-micro` the answer is
usually memory: this host has 1 GB and runs a chain node next to Caddy.

```bash
ssh sccp@136.119.76.215 'free -m; dmesg -T | grep -i oom | tail -5'
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && docker compose logs --tail=60 --no-color node'
```

A restart loop that starts **right after a stack deploy** is not memory. The node
replays its SQLite chain before it binds the port, under the running build's
consensus parameters, so a release that moves one — the 30 s cadence doubled the
maturity, registration, dispute and beacon windows — cannot apply blocks the
previous build wrote. The tail of the log is a validation error about the header
state root, not a kill:

```
BlockValidationError header state root is <a>, the block applies to <b>
```

`deploy-edge.yml` handles this: it waits for health, prints that tail, wipes the
store and starts once more, leaving a `::warning::` in the run. By hand it is the
same three steps — the container is the only writer, so stop it first:

```bash
ssh sccp@136.119.76.215 'cd /srv/sccp-edge \
  && docker compose stop node \
  && docker compose run --rm --no-deps --entrypoint sh node -c "rm -rf /data/*" \
  && docker compose up -d --no-build'
```

Nothing is lost: no keys, no faucet, and no block the primary does not also have
— the same reasoning as case 3. A loop that survives the rebuild is the image,
not the store.

## Case 2: it answers and never advances

The node's log names the peer and the reason, because the sync is a plain poll of
`SCCP_PRIMARY_URL`:

```bash
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && cat .env'
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && docker compose logs --tail=100 --no-color node | grep -iE "peer|sync|error"'
curl -fsS https://app.proofofchess.com/v1/status | jq -r .height
```

| Log says | Means |
| --- | --- |
| connection refused / timeout to the primary | the mirror cannot reach the primary over the public API — the primary is down ([`PRIMARY_DOWN.md`](PRIMARY_DOWN.md)) or the edge VM lost egress |
| `genesis` mismatch | case 3, below; it will never sync, by design |
| nothing at all about peers | `SCCP_PRIMARY_URL` is empty or wrong in `.env`; the container refuses to start with it unset, so this is a stale file |
| repeated `409`/`bad block` | the replica's store is ahead of a reset primary — case 3 |

Egress is worth ruling out explicitly, because a VPC without a Cloud NAT or an
external address leaves a host that answers on 443 and reaches nothing:

```bash
ssh sccp@136.119.76.215 'curl -fsS -o /dev/null -w "%{http_code}\n" https://app.proofofchess.com/v1/status'
```

## Case 3: the replica holds a chain nobody else has

This is the one case where the mirror is actively harmful: readers trust the name
and get a chain that does not exist. Take it out of service first, then decide.

```bash
# Stop serving the wrong chain before diagnosing it.
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && docker compose stop node'
```

The rebuild is the whole remedy, and it is safe because the mirror owns nothing:

```bash
ssh sccp@136.119.76.215 'cd /srv/sccp-edge \
  && docker compose down \
  && docker volume rm sccp-edge_node-data \
  && docker compose up -d --no-build'
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && docker compose logs -f node'
```

The node refuses a peer whose genesis differs, so a resync against a *reset*
primary is the only way forward — and if the genesis itself changed, the mirror's
Caddy keeps serving the same name and the data underneath is simply the new chain.
There is nothing to preserve: no keys, no faucet, no history that the primary does
not also have.

## Rebuilding through the workflow instead

`deploy-edge.yml` is not only a deploy. It waits for the new image's node to
become healthy and, when it cannot replay the store it inherited, wipes
`node-data` and starts it once; it also compares the mirror's block 1 with the
primary's and, when they disagree, deletes the mirror's `node-data` itself before
restarting — the case above, automated. So after the primary has been reset, or
after a release that changed a consensus parameter, the cheapest path is often to
let the next successful stack deploy run the mirror job:

```bash
gh run list --workflow=deploy-edge.yml --limit 5
gh run view <run-id> --log-failed
```

The workflow is skipped entirely when `vars.EDGE_DEPLOY_HOST` is unset, and its
run is green in that case ([`DEPLOY_FAILURES.md`](DEPLOY_FAILURES.md)). A mirror
that exists but never redeploys after stack deploys is that variable, not the
mirror.

## What the mirror is allowed to be

Keep the boundaries from `deploy/edge/` intact while fixing it; each one is a
deliberate refusal that is easy to undo by accident:

- **no `--mine` and `--no-faucet`.** A replica that can mint or spend is a second
  authority on the network.
- **the node binds `127.0.0.1` and nothing publishes `8645`.** Do not add a
  firewall rule for it while debugging "just to test": the API is reachable
  through Caddy, and that is the only surface the design allows.
- **Caddy answers `405` to every method except `GET`.** A `POST` that starts
  working is a bug, not a feature, and it is the exact thing the mirror exists to
  make impossible.
- **`SCCP_IMAGE` must be a published tag** (`:testnet` or `:$GITHUB_SHA`), never a
  local build. A mirror built from a different commit replicates a different
  implementation of the chain rules and will diverge from the primary it copies.

## Verifying a recovery

```bash
curl -fsS https://mirror.proofofchess.com/v1/status | jq '{height, tip_hash}'
curl -fsS https://app.proofofchess.com/v1/status   | jq '{height, tip_hash}'
curl -fsS https://mirror.proofofchess.com/v1/blocks/0 | jq -r .hash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mirror.proofofchess.com/v1/tx -d '{}'
```

The two heights should differ by at most a few blocks, the genesis hashes must be
identical, and the write attempt must be `405`. A mirror that matches on all three
and still looks stale in a browser is a caching page or a stale tab — the proxy
sends `Cache-Control: no-store`, so the browser is the suspect.

## Prevention

1. Leave `deploy-edge.yml` wired to run after every successful stack deploy. It is
   the only thing that repairs the primary-reset case without a human.
2. Alert on the mirror's `height` falling more than 10 blocks behind the primary's
   for 15 minutes — not on the mirror being unreachable, which is SEV4 noise.
3. Rotate the image tag on the mirror with every stack deploy so the two run the
   same commit; a mirror one commit behind is a slow-motion divergence.
4. Do not let the mirror accumulate responsibilities. Every "while we are here, it
   could also serve writes/keep a faucet key/mine" is a second primary with none of
   the primary's protections.

