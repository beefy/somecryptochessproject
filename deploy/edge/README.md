# The second node: a public read-only mirror on its own VM

The testnet's primary VM runs the only authoritative node. Everything the chain
does — mining, matches, settlement, the faucet — happens there. That is one
failure domain for reads as well as writes, and a single node proves nothing
about replication.

This deployment is the second failure domain: an `e2-micro` running one node in
a separate Compose project, pull-syncing the primary, serving the same JSON API
at its own HTTPS name, and refusing every write. It is the smallest host that can
hold the chain, and it is deliberately boring: no miner, no relay, no faucet, no
Docker socket, no inbound port except 443 (and 80 for the ACME challenge).

## What it is for

- **Replication is tested, not assumed.** `--peer` with `--peer-poll-seconds 30`
  keeps pulling the primary's blocks; `sync_peers` checks the genesis hash, the
  parent chain and the proof rules before adopting anything.
- **Reads survive the primary.** Blocks, matches, proofs and status stay
  answerable while the primary is down or being redeployed.
- **Public exposure is one narrow surface.** The proxy speaks TLS, allows GET and
  HEAD, caps request bodies and answers everything else with 405.

It is *not* a second authority: it never mines, never signs, and never accepts a
transaction. Writes go to the primary node (the app already reaches it through
`https://app.proofofchess.com/v1`).

## Create the VM

The firewall for this VM is separate from the primary's: only 80/443 are open and
SSH is over IAP, so there is no inbound `22`, `8645` or `8646`.

```bash
export PROJECT_ID="sccp-testnet"
export REGION="us-central1"
export ZONE="us-central1-a"
export EDGE="sccp-edge"
export EDGE_TAG="sccp-edge"
export EDGE_IP="sccp-edge-ip"

gcloud compute addresses create "$EDGE_IP" --region="$REGION" --project="$PROJECT_ID"

gcloud compute instances create "$EDGE" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
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
    --network=sccp-testnet \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$EDGE_TAG"

# No 22: IAP tunnels SSH, so there is no SSH port for the internet to find.
gcloud compute firewall-rules create sccp-edge-iap-ssh \
    --project="$PROJECT_ID" \
    --network=sccp-testnet \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags="$EDGE_TAG"

gcloud compute ssh "$EDGE" --zone="$ZONE" --project="$PROJECT_ID" --tunnel-through-iap
```

Point an `A` record for the mirror's name at `EDGE_IP` before starting the stack:
Caddy cannot finish the HTTP-01 challenge until the name resolves to this VM.

## Deploy

The VM needs Docker Engine and the Compose plugin (the primary's
`docs/operations/GCP_RESOURCE_SETUP.md` section 5 has the Debian commands), plus the
three files in this directory.

```bash
# on the edge VM, as the deploy user, in /srv/sccp-edge
export SCCP_IMAGE="<the image the primary deploy published>"
export SCCP_EDGE_DOMAIN="mirror.example.com"
export SCCP_PRIMARY_URL="https://app.proofofchess.com"

docker compose pull
docker compose up -d
docker compose ps
curl -fsS "https://$SCCP_EDGE_DOMAIN/v1/status"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
    "https://$SCCP_EDGE_DOMAIN/v1/tx" -d '{}'   # 405, by design
```

The image is public in the same registry the primary deploy uses, so the mirror
does not need the source tree. The `build:` key in the Compose file is for local
runs; `docker compose pull` wins when `SCCP_IMAGE` names a registry image and
BuildKit is not asked to build.

### What to watch

```bash
docker compose logs -f node      # "replicated <peer>: +N blocks to height H"
docker compose logs -f tls       # certificate and request log
docker compose exec node sccp chain status --data-dir /data   # if it stops advancing
```

A mirror whose height trails the primary by more than a couple of blocks is
either failing to reach `SCCP_PRIMARY_URL` or being refused by it (the log line
names which). It is safe to delete `node-data` and start over: the chain is
rebuildable from the primary in one sync.

## Why the API is read-only here

`POST /v1/tx` is how a wallet submits a transaction, and an open write endpoint
on a small VM is an invitation to fill its disk with mempool entries, however
well the node validates them. The primary already terminates TLS for the app and
takes writes; the mirror's job is reads. If that changes, it is one matcher in
`Caddyfile` plus a rate limit and a bigger disk — not a config flag to flip
casually.
