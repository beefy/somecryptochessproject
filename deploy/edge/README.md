# The second node: a public read-only mirror on its own VM

The testnet's primary VM runs the only authoritative node. Everything the chain
does — mining, matches, settlement, the faucet — happens there. That is one
failure domain for reads as well as writes, and a single node proves nothing
about replication.

This deployment is the second failure domain: an `e2-micro` running one node in
a separate Compose project, pull-syncing the primary, serving the same JSON API
at its own HTTPS name, and refusing every write. It is the smallest host that can
hold the chain, and it is deliberately boring: no miner, no relay, no faucet, no
Docker socket, no inbound port from the internet except 443 (and 80 for the ACME
challenge).

## What it is for

- **Replication is tested, not assumed.** `--peer` with `--peer-poll-seconds 30`
  keeps pulling the primary's blocks; `sync_peers` checks the genesis hash, the
  parent chain and the proof rules before adopting anything.
- **Reads survive the primary.** Blocks, matches, proofs and status stay
  answerable while the primary is down or being redeployed.
- **Public exposure is one narrow surface.** The proxy speaks TLS, allows GET,
  caps request bodies and answers every other method — HEAD and all writes
  included — with 405 and `Allow: GET`.

It is *not* a second authority: it never mines, never signs, and never accepts a
transaction. Writes go to the primary node (the app already reaches it through
`https://app.proofofchess.com/v1`).

## Create the VM

The firewall for this VM is separate from the primary's: only 80/443 are open and
SSH is over IAP, so there is no inbound `22`, `8645` or `8646`. Pass `--network`
*and* `--subnet` explicitly: the rules below are created in `$NETWORK`, and an
instance created without them lands in `default`, where none of them apply — the
symptom is a mirror that is up, healthy on loopback and unreachable from the
internet. The VPC is custom-mode (one subnet per region), so naming the network
alone is not enough either.

```bash
export PROJECT_ID="chessproofofwork"   # the project the primary VM runs in
export NETWORK="sccp-testnet"          # the VPC the primary VM and these rules use
export REGION="us-central1"
export ZONE="us-central1-a"
export SUBNET="sccp-testnet-${REGION}" # custom-mode VPC: one subnet per region
export PRIMARY_TAG="sccp-node"         # the primary VM's network tag
export EDGE="sccp-edge"
export EDGE_TAG="sccp-edge"
export EDGE_IP="sccp-edge-ip"

gcloud compute addresses create "$EDGE_IP" --region="$REGION" --project="$PROJECT_ID"

gcloud compute instances create "$EDGE" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --network="$NETWORK" \
    --subnet="$SUBNET" \
    --machine-type=e2-micro \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --tags="$EDGE_TAG" \
    --address="$(gcloud compute addresses describe "$EDGE_IP" \
        --region="$REGION" --format='value(address)' --project="$PROJECT_ID")" \
    --shielded-secure-boot \
    --shielded-vtpm \
    --shielded-integrity-monitoring \
    --no-service-account \
    --no-scopes

# 443 for the mirror, 80 for the certificate challenge and its redirect.
gcloud compute firewall-rules create sccp-edge-web \
    --project="$PROJECT_ID" \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$EDGE_TAG"

# No 22 from the internet: IAP tunnels SSH, so there is no SSH port to find.
gcloud compute firewall-rules create sccp-edge-iap-ssh \
    --project="$PROJECT_ID" \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags="$EDGE_TAG"

gcloud compute ssh "$EDGE" --zone="$ZONE" --project="$PROJECT_ID" --tunnel-through-iap
```

### Let the deploy workflow in (optional)

`deploy-edge.yml` runs on GitHub-hosted runners. It can reach this VM over an
IAP tunnel only with a GCP service account, so the repository takes the cheaper
route: it jumps through the primary deployment host, which already has SSH from
CI. That needs one rule and one key, and no public port on the mirror:

```bash
# The primary's tag -> the mirror's tag, tcp:22, inside the VPC only.
gcloud compute firewall-rules create sccp-edge-ssh-from-primary \
    --project="$PROJECT_ID" \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-tags="$PRIMARY_TAG" \
    --target-tags="$EDGE_TAG"
```

`$PRIMARY_TAG` is the primary VM's network tag. The deploy key that
`TESTNET_SSH_KEY` already holds also has to be authorized on this VM, and the
user in `EDGE_DEPLOY_HOST` has to be the one that key names. GCP rewrites
`authorized_keys` from instance metadata, so add it there rather than by hand:

```bash
# deploy.pub holds one `user:ssh-ed25519 AAAA... comment` line, the same key
# pair TESTNET_SSH_KEY holds. The user must be one that exists on a stock
# Debian image (for example the operator account created by OS Login).
gcloud compute instances add-metadata "$EDGE" \
    --zone="$ZONE" --project="$PROJECT_ID" \
    --metadata-from-file ssh-keys=deploy.pub
```

Then set `vars.EDGE_SSH_PROXY_JUMP=1` and `vars.EDGE_DEPLOY_HOST=<user>@<internal IP>`;
without the jump flag, `EDGE_DEPLOY_HOST` must be directly reachable from the
runner (a self-hosted runner, or a deliberately widened rule — the same
trade-off `docs/setup/GCP_RESOURCE_SETUP.md` describes for the primary). The
workflow checks the pair: an internal address with no jump flag fails in the
`Configure SSH` step with an `::error::` naming `EDGE_SSH_PROXY_JUMP`, because the
mirror has no public SSH to read a host key from.

```bash
# the mirror's internal address: what EDGE_DEPLOY_HOST points at when jumping
gcloud compute instances describe "$EDGE" --zone="$ZONE" --project="$PROJECT_ID" \
    --format='value(networkInterfaces[0].networkIP)'
```

Point an `A` record for the mirror's name at `EDGE_IP` before starting the stack:
Caddy cannot finish the HTTP-01 challenge until the name resolves to this VM.

## Deploy

The VM needs Docker Engine and the Compose plugin (the primary's
`docs/setup/GCP_RESOURCE_SETUP.md` section 5 has the Debian commands). The
stack itself is two files — `docker-compose.yml` and `Caddyfile` — plus a `.env`
that names the image, the mirror's domain and the primary URL.

### Automatically, after every primary deploy

`.github/workflows/deploy-edge.yml` waits for `deploy-testnet-miners` to finish,
then ships this directory, writes `.env`, pulls the tag that deployment built and
proves the replica advanced. Configure it as repository variables and secrets next
to the primary's, or on the `testnet` environment — the workflow reads them inside
a step, so either scope is seen (a job-level `if` would not be, and the job would
skip with nothing in the log):

| Kind | Name | Value |
| --- | --- | --- |
| var | `EDGE_DEPLOY_HOST` | `user@host` of the mirror; unset disables the deploy |
| var | `EDGE_DOMAIN` | the mirror's public name, e.g. `mirror.proofofchess.com` |
| var | `EDGE_PRIMARY_URL` | optional; defaults to `https://app.proofofchess.com` |
| var | `EDGE_DEPLOY_DIR` | optional; defaults to `/srv/sccp-edge` |
| var | `EDGE_SSH_PROXY_JUMP` | optional; `1` to jump through `TESTNET_DEPLOY_HOST` |
| secret | `EDGE_SSH_KEY` | optional; falls back to `TESTNET_SSH_KEY` |
| secret | `EDGE_KNOWN_HOSTS` | optional; falls back to `TESTNET_KNOWN_HOSTS` |

A pinned `EDGE_KNOWN_HOSTS` covers the *primary* — the fallback is the primary's
own pin — so the mirror's host key is scanned through the jump on every run. If
you would rather pin it, put both keys in `EDGE_KNOWN_HOSTS`: that entry is found
with `ssh-keygen -F` and the probe is skipped, which is the only way the step
works when the runner has no route to the mirror at all.

The job is idempotent, and it only destroys the replica on purpose. Both resets
are scoped to `node-data`; `caddy_data` is never touched, because losing it
re-issues the certificate:

* **the node never turns healthy.** The mirror replays its SQLite chain at
  startup, under the *new* build's consensus parameters. A release that moves one
  — the 30 s cadence doubled the maturity, registration, dispute and beacon
  windows — cannot replay blocks written by the old build, so the node exits and
  `tls` never starts behind its `service_healthy` dependency. The restart step
  waits for health, prints the node's last log lines, throws the store away and
  starts once more. A second failure is the image, and the run goes red.
* **block 1 differs from the primary's.** The primary resets its chain on every
  stack deploy, so a mirror that kept the old one is serving blocks nobody
  follows. The workflow compares block 1 with the primary's and deletes the
  node's data when they disagree.

To move the mirror to a different tag by hand, run the workflow with
`image_tag=<sha>`.

### Manually, on the VM

```bash
# on the edge VM, as the deploy user, in /srv/sccp-edge
cp .env.example .env && $EDITOR .env   # image, domain, primary URL
set -a && . ./.env && set +a           # the same values, for the checks below
# If the registry package is private, log in first; the workflow uses its job
# token for exactly this:
#   printf '%s' "$TOKEN" | docker login ghcr.io -u <user> --password-stdin
docker compose pull node
docker compose up -d --no-build
docker compose ps
curl -fsS "https://$SCCP_EDGE_DOMAIN/v1/status"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
    "https://$SCCP_EDGE_DOMAIN/v1/tx" -d '{}'   # 405, by design
curl -sS -o /dev/null -D - -X POST \
    "https://$SCCP_EDGE_DOMAIN/v1/tx" -d '{}' | grep -i '^allow: GET'
```

The mirror does not need the source tree: `docker compose pull` is the only step
that talks to the registry, and `--no-build` keeps Compose from trying to build a
500 MB image on a 1 GB VM if the tag is missing. `docker compose up -d` after a
`SCCP_IMAGE` change recreates the node container and keeps the volume.

Keeping the volume is only right while the new build can read it. The node
replays its stored chain at startup, so an image that changes a consensus
parameter cannot apply blocks written by the previous one and exits instead of
serving. That is the deploy's job, not yours: it waits for health, wipes
`node-data` and starts once more. If you are doing it by hand, see case 1 of
[`docs/operations/MIRROR_OUTAGE.md`](../../docs/operations/MIRROR_OUTAGE.md).

### What to watch

```bash
docker compose logs -f node      # "replicated <peer>: +N blocks to height H"
docker compose logs -f tls       # certificate and request log
docker compose exec node sccp --data-dir /data chain info   # if it stops advancing
```

`--data-dir` is a global option, so it goes before `chain`; `chain status` is not
a command.

A mirror whose height trails the primary by more than a couple of blocks is
either failing to reach `SCCP_PRIMARY_URL` or being refused by it (the log line
names which). It is safe to delete `node-data` and start over: the chain is
rebuildable from the primary in one sync.

### Roll back

The image tag is the only moving part. Pin the previous one in `.env` and
recreate the node; the volume and the certificate are untouched:

```bash
# Edit SCCP_IMAGE in .env to the previous tag, then:
docker compose pull node && docker compose up -d --no-build
```

Docker keeps the previous image locally, so a rollback on the VM does not need
the registry at all. `caddy_data` is what stops a redeploy from re-issuing a
certificate and burning Let's Encrypt's weekly limit.

## Why the API is read-only here

`POST /v1/tx` is how a wallet submits a transaction, and an open write endpoint
on a small VM is an invitation to fill its disk with mempool entries, however
well the node validates them. The primary already terminates TLS for the app and
takes writes; the mirror's job is reads. If that changes, it is one matcher in
`Caddyfile` plus a rate limit and a bigger disk — not a config flag to flip
casually.
