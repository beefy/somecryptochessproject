# Deploying the explorer

Every merge that touches `src/web` or `deploy/explorer` runs
[`deploy-explorer.yml`](../.github/workflows/deploy-explorer.yml), which publishes
the built bundle to:

```text
http://explorer.somecryptochessproject.com
```

That host serves three things on one origin: the bundle at `/`, the node API
under `/v1` and the matchmaking relay under `/relay`. It is an nginx container,
not a bucket, for the reason in the next section.

The deploy never touches the chain. It uploads a new release directory, moves one
symlink and leaves the node, the relay and the SQLite file alone — unlike
[`deploy-testnet-miners.yml`](../.github/workflows/deploy-testnet-miners.yml),
which resets the chain.

## Why a proxy instead of just files

The node sends **no CORS headers** (only the matchmaker has a
`CORSMiddleware`, and only when it is started with `--origin`). A bundle served
from one origin therefore cannot read `/v1` on another: `GET /v1/status` fails in
the browser with an opaque network error even though `curl` works. The page has
to be same-origin with the API, which is why the workflow builds with
`VITE_SCCP_API` empty and lets nginx proxy the path.

The relay is proxied for the same reason, one step further: `#/play` talks to it
directly (`VITE_SCCP_RELAY`), so a hosted build would need a second origin, an
`--origin` allowlist on `sccp matchmaker run` and a port open to browsers. Built
with `VITE_SCCP_RELAY=/relay`, the page calls `/relay/v1/...` on its own origin,
nginx strips the prefix, and `8646` can stay closed. Same-origin requests are not
preflighted, so the relay needs no CORS configuration at all.

| Browser asks for | nginx does | Reaches |
| --- | --- | --- |
| `GET /` and `/assets/*` | serves `/srv/current` | the uploaded release |
| `GET /v1/status` | `proxy_pass http://127.0.0.1:8645` | the node |
| `POST /relay/v1/matches/{id}/move` | `proxy_pass http://127.0.0.1:8646/` | the matchmaker |

Both proxy targets are loopback ports on the VM. The explorer container runs with
`network_mode: host`, so it shares the host's network namespace and reaches the
ports that [`docker-compose.yml`](../docker-compose.yml) already publishes. It
needs no Docker network in common with the chain's stack and no credentials.

## What the workflow does

1. **build** — `npm ci`, `npm test`, then `npm run build` with
   `VITE_SCCP_API=""` and `VITE_SCCP_RELAY=/relay`, uploaded as an artifact. The
   vectors that `ci.yml` runs are run again here: a bundle that derives an address
   or hashes a move wrongly must not reach a browser that signs with it.
2. **upload** — copies `deploy/explorer/docker-compose.yml` and
   `deploy/explorer/nginx.conf` to the deploy directory, then extracts the bundle
   into a new `www/releases/<UTC timestamp>-<sha>` directory.
3. **start** — `docker compose up -d` in the deploy directory. Idempotent: it
   recreates the container only when the configuration changed.
4. **switch** — `ln -sfn` a `current.next` link and `mv -T` it over `current`.
   That rename is the deploy: nginx resolves the symlink per request, so no
   container is restarted and no request can see a half-written bundle. The step
   then asserts that `current` really resolves to the new release.
5. **prune** — keeps the last five releases, for rollback.
6. **verify** — over the public URL: the document, one hashed asset referenced by
   it, and `/v1/status` returning `"network": "testnet"`. `/relay/v1/status` is
   checked too but only warns, because the relay belongs to the chain's deploy.

## Public endpoint

```bash
curl -sS http://explorer.somecryptochessproject.com/v1/status \
  | jq '{network, height, tip_hash}'
curl -sS http://explorer.somecryptochessproject.com/relay/v1/status \
  | jq '{network, epoch, matches}'
curl -sSI http://explorer.somecryptochessproject.com/ | head -3
```

The first two answer because of the proxy; the third is the bundle. Open the root
in a browser for the explorer, or `#/play` for the table.

## Layout on the host

```text
/opt/sccp-explorer/
├── docker-compose.yml        # from deploy/explorer/docker-compose.yml
├── nginx.conf                # from deploy/explorer/nginx.conf
└── www/                      # bind-mounted at /srv, read-only
    ├── current -> releases/20261112T183045-1a2b3c4
    └── releases/
        ├── 20261112T183045-1a2b3c4/
        └── 20261111T090512-9f8e7d6/
```

`current` points at the live release. Everything else in `releases/` is a
rollback target and is deleted when a sixth deploy lands.

---

## One-time setup

Nothing here is a new server. The explorer runs on the testnet VM, next to the
node and relay it proxies, so the GCP work is a firewall rule and a DNS record on
an environment that already exists. If the VM does not exist yet, build it first
with [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md); the commands below assume
its variables (`$PROJECT_ID`, `$NETWORK`, `$NODE_TAG`, `$STATIC_IP`,
`$DNS_ZONE`).

### GCP: firewall and DNS

```bash
export EXPLORER_HOST="explorer.somecryptochessproject.com"

gcloud compute firewall-rules create sccp-testnet-explorer-web \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$NODE_TAG" \
    --project="$PROJECT_ID"
```

`--target-tags` must be the tag the instance already carries. This adds a rule to
the existing policy; it does not touch the node (`8645`) or relay (`8646`) rules,
and those keep working unchanged.

Then point the name at the same address the node uses:

```bash
gcloud dns record-sets create "$EXPLORER_HOST." \
    --zone="$DNS_ZONE" \
    --type=A \
    --ttl=60 \
    --rrdatas="$STATIC_IP" \
    --project="$PROJECT_ID"
```

If the record already exists, use `gcloud dns record-sets update` with the same
arguments. Verify from your workstation before wiring GitHub:

```bash
dig +short "$EXPLORER_HOST" A
```

A CNAME pointing at another *name* that resolves to the same VM also works, but
the deployment expects an `A` record on the instance's address: `/v1` and
`/relay` are paths on the origin that served the page, so the name has to land on
the host running the proxy rather than on a redirect. TLS is a separate decision;
see [TLS](#tls).

### GCP: the deploy directory

The workflow creates its own tree under `$DEPLOY_DIR` (default
`/opt/sccp-explorer`), but `/opt` belongs to root. It tries
`sudo -n install -d` first, which works when the deploy user has passwordless
sudo — GCP's default user does. A restricted `sccp` user usually does not, so if
the **Prepare the deploy directory** step stops, run its printed command once,
exactly as for `/opt/sccp-testnet`:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT_ID" --command \
  "sudo install -d -m 755 -o sccp -g sccp /opt/sccp-explorer"
```

Use the user from `EXPLORER_DEPLOY_HOST` (`sccp` in the example) as both owner and
group. The step also re-checks writability after creating the directory, so a
`/opt/sccp-explorer` that exists but belongs to someone else fails there with the
`chown` to run, instead of a `mkdir: Permission denied` in the middle of the
upload.

The VM needs nothing else. Docker Engine and the Compose v2 plugin are already
installed for the chain's deploy, the `nginx:1.27-alpine` image is pulled on first
use, and port `80` is free unless something else claimed it:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT_ID" --command \
  "sudo ss -ltnp 'sport = :80' || echo 'port 80 free'"
```

### GitHub

The deploy job reuses the `testnet` environment, because the explorer lives on the
same host. Only the variables below are new; each one falls back to the
`TESTNET_*` value that is already set, so a deployment that keeps both on one VM
needs **no new configuration at all**.

| Kind | Name | Falls back to | Meaning |
| --- | --- | --- | --- |
| variable | `EXPLORER_DEPLOY_HOST` | `TESTNET_DEPLOY_HOST` | `user@host` of the VM |
| variable | `EXPLORER_DEPLOY_DIR` | — (default `/opt/sccp-explorer`) | where the releases live |
| variable | `EXPLORER_DEPLOY_PORT` | — (default `22`) | SSH port |
| variable | `EXPLORER_PUBLIC_URL` | — (default `http://explorer.somecryptochessproject.com`) | what the smoke check requests |
| secret | `EXPLORER_SSH_KEY` | `TESTNET_SSH_KEY` | private key for that user |
| secret | `EXPLORER_KNOWN_HOSTS` | `TESTNET_KNOWN_HOSTS` | pinned host key |

Set them explicitly only when the explorer is somewhere else:

```bash
gh variable set EXPLORER_DEPLOY_HOST --body "sccp@$EXPLORER_HOST"
gh variable set EXPLORER_PUBLIC_URL  --body "http://$EXPLORER_HOST"
gh secret   set EXPLORER_SSH_KEY < ~/.ssh/sccp_explorer_deploy
```

Then trigger it:

```bash
gh workflow run deploy-explorer.yml
gh run watch
```

The deploy job is skipped while neither host variable is set: the bundle still
builds, so a repository without a VM keeps a useful check.

---

## TLS

This deployment speaks plain HTTP, like the rest of the testnet. That is a
deliberate limit, not an oversight — and it matters more here than for a JSON
endpoint, because `#/play` generates a seat keypair **in the page** and signs
moves with it. Anyone who can read the traffic can read that key. Treat the page
as a testnet toy, or terminate TLS before pointing anyone at it.

Two ways, both outside this workflow:

- **In front.** Put Cloudflare, a load balancer, or a Caddy/nginx TLS terminator
  ahead of the VM and proxy to `:80`. Set
  `gh variable set EXPLORER_PUBLIC_URL --body "https://$EXPLORER_HOST"` so the
  smoke check follows the new scheme. Nothing in the bundle has to change:
  `VITE_SCCP_RELAY=/relay` is a path, so it picks up whatever origin served the
  page.
- **Here.** Add a `listen 443 ssl` server block with the same locations to
  [`deploy/explorer/nginx.conf`](../deploy/explorer/nginx.conf), mount the
  certificate in [`docker-compose.yml`](../deploy/explorer/docker-compose.yml),
  and add `tcp:443` to the firewall rule. Keep `/v1` and `/relay` on the same
  origin — splitting them across hosts brings back the CORS problem this design
  removes.

## Rollback

The last five releases stay on disk. List them and move the symlink:

```bash
ssh sccp@explorer.somecryptochessproject.com \
  "ls -1t /opt/sccp-explorer/www/releases | head -5"
ssh sccp@explorer.somecryptochessproject.com \
  "cd /opt/sccp-explorer/www \
   && ln -sfn releases/<release> current.next && mv -T current.next current \
   && readlink -f current"
```

`mv -T` is not decoration: without `-T`, `mv` treats a destination that is a
symlink to a directory as a directory and moves the new link *inside* it, which
leaves the site on the old bundle and raises nothing. The workflow asserts the
result after every deploy for the same reason.

Rolling back the bundle does not roll back the chain. If an API shape changed,
an old bundle may show errors instead of data; the fix is a forward deploy, or a
`git revert` of the merge that changed it.


---

## Two deploys, one VM

`deploy-explorer.yml` and `deploy-testnet-miners.yml` are independent on purpose:

- the explorer deploy never runs `docker compose down`, never restarts the node
  and never writes to the node's data directory;
- the chain deploy works in `/opt/sccp-testnet` and does not look at
  `/opt/sccp-explorer`, so `down -v` cannot take the page offline;
- a merge that changes both runs both, in either order.

The one visible overlap is timing: while the chain deploy restarts the node, the
page shows its "cannot reach the node at this origin" state for a few seconds,
and `#/play` may lose an in-flight move. Reload when the deploy goes green. The
chain is also reset on every chain deploy, so the explorer will show a freshly
bootstrapped chain afterwards — that is the reset, not a broken page.

The explorer is also unaffected by 8646 being firewalled off. Browsers only ever
reach the relay through `/relay` on port 80, so the relay rule in
[`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) can stay closed to the internet
even though `#/play` plays real matches.

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| `502` on `/v1/status` | No node on the VM's `8645`, or the container is not up | `curl 127.0.0.1:8645/v1/status` on the VM; `docker compose ps` in `/opt/sccp-testnet` |
| `502` on `/relay/v1/status` | No relay on `8646` | `curl 127.0.0.1:8646/v1/status` on the VM |
| Page loads, panel says "cannot reach the node at this origin" | Same as the 502, seen from the browser | The API panel names the base URL; compare with the curl above |
| `404` on `/assets/*.js` | Release upload incomplete, or the symlink points at an empty directory | `ls -l /opt/sccp-explorer/www/current/`; re-run the workflow |
| Page serves an old bundle | `current` was not switched | `readlink -f /opt/sccp-explorer/www/current` — it must be the newest release |
| nginx crash-loops with `cannot open /etc/nginx/conf.d/default.conf` | `nginx.conf` was never uploaded (a directory exists at that path) | Re-run the workflow; it copies the file before starting the container |
| `bind source path does not exist` | `www` missing because no release has been uploaded | Run the workflow once |
| `mkdir: cannot create directory '/opt/sccp-explorer': Permission denied` | The deploy directory was never created and the user has no passwordless sudo | Run the `sudo install -d` command in [the deploy directory](#gcp-the-deploy-directory); the job's **Prepare the deploy directory** step now stops first and prints it |
| `mv: invalid option -- 'T'` | Not GNU coreutils | The VM should run Debian 12, as in [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) |
| `EADDRINUSE` on port 80 | Something else on the host owns it | `sudo ss -ltnp 'sport = :80'`; move that service or run the explorer elsewhere |
| `dig` returns nothing | DNS record missing or not propagated | `gcloud dns record-sets list --zone="$DNS_ZONE"`; wait for the 60 s TTL |
| `curl` times out on port 80 | Firewall rule missing, or the instance lost its tag | `gcloud compute firewall-rules list --filter="network:$NETWORK"` |
| Workflow deploy job skipped | No host configured | `gh variable list \| grep -i deploy_host` |
| `#/play` says "the relay at … is on devnet" | The relay follows a different chain than the node | The **Two networks** panel names both; restart the relay with the node's `--network`, see [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md) |

## Cost

The explorer adds no billable resource: it is one container on the VM that is
already running the chain, a ~10 MB image and a few hundred KB of files per
release. Page loads are a few hundred KB of JS, so egress stays inside the noise
of the node's own traffic. The only cost that changes is storage, and five kept
releases are a couple of megabytes.

## See also

- [`src/web/README.md`](../src/web/README.md) — building the bundle, the dev
  server, `VITE_SCCP_API`/`VITE_SCCP_RELAY` and the failure modes in the page
  itself.
- [`EXPLORE_THE_CHAIN.md`](EXPLORE_THE_CHAIN.md) — what the pages show and how to
  read them.
- [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md) — the chain deploy this one
  sits beside.
- [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) — the VM, network and deploy
  user both workflows use.

