# Moving the deployment to `proofofchess.com`

The public testnet and the web application now live on
`app.proofofchess.com`, in the existing `chessproofofwork` Google Cloud project.
The apex `proofofchess.com` is the project's landing page. The five subdomains
(`explorer.`, `wallet.`, `play.`, `dex.`, `bridge.`) are gone: they were never
five deployments, only five names for one bundle, and the bundle now selects its
view from the hash route instead of the hostname.

This page is the cutover record and the rebuild recipe: what already exists, what
has to be created, the order to do it in, and how to get back.

**Status (2026-11-17): the `proofofchess-com` zone is delegated, its apex `A`
record points at the VM, and the GitHub variables name the app host. The
app-plus-landing code is in this commit; merging it publishes both releases and
issues the apex and `app.` certificates. The old subdomain records stay in place
for rollback until step 8.** The GCP project, VM, network, static IP, deploy user
and GitHub environment already exist. The only new cloud resource is the
`proofofchess-com` Cloud DNS zone; the changed settings are its two `A` records
and the GitHub variables that name the host. The repository changes (landing
page, web routing, Caddyfile, docs) are in the same commit as this page.

## Before and after

| | Before | After |
| --- | --- | --- |
| Application origin | `https://explorer.chessproofofwork.com`, `https://wallet.chessproofofwork.com`, `https://play.chessproofofwork.com`, `https://dex.…`, `https://bridge.…` | `https://app.proofofchess.com` |
| Project overview | none | `https://proofofchess.com` |
| View selection | hostname (`siteForHostname`) | hash route: `#/`, `#/wallet`, `#/play`, `#/dex`, `#/bridge` on the app origin |
| Certificates | one per name, five ACME orders | two: the apex landing page and the app |
| DNS | five `A` records in `chessproofofwork-com` | two `A` records in `proofofchess-com` |
| Node API | `http://testnet.chessproofofwork.com:8645` | `http://app.proofofchess.com:8645`, plus `https://app.proofofchess.com/v1` through nginx |
| GitHub host | `sccp@testnet.chessproofofwork.com` | `sccp@proofofchess.com` |
| Cloud project | `chessproofofwork` (206061816244) | unchanged |
| VM | `sccp-testnet-1`, `us-central1-a`, `e2-standard-2`, 20 GB, Debian 12 | unchanged |
| Static IP | `sccp-testnet-ip` = `34.66.79.187` | unchanged |
| DNS zone | `chessproofofwork-com` | `proofofchess-com` |

Nothing about the chain moves: no genesis file, no protocol constant, no storage
format. A testnet redeploy starts from an empty chain anyway, so there is no state
to migrate.

## What the repository change does

| Where | What changed |
| --- | --- |
| `src/landing/` | new static overview served at the apex; no JavaScript, no API call, links every action to the app |
| `src/web/src/lib/site.ts` | dropped `SITE_ORIGINS`, `siteForHostname`, `currentSite`, `crossSiteHref` and `hashUrl`; one `siteHash()` map is now the whole routing contract |
| `src/web/src/App.tsx` | the dashboard no longer depends on the hostname; the tab title follows the active view |
| `src/web/src/main.tsx` | removed the hostname-based title selection |
| `deploy/explorer/Caddyfile` | two site blocks, `proofofchess.com` and `app.proofofchess.com`, one certificate each |
| `deploy/explorer/nginx.conf`, `docker-compose.yml` | one nginx container with two listeners and two independent release trees |
| `.github/workflows/deploy-explorer.yml` | builds and publishes the app and landing artifacts, switches both symlinks, smoke-tests both |
| `.github/workflows/deploy-testnet-miners.yml` | `TESTNET_PUBLIC_URL` default is `http://app.proofofchess.com:8645` |
| `README.md`, `deploy/README.md`, `docs/*.md`, `analysis/*` | every `curl`, `--peer`, `--node`, `dig` and browser URL |
| `src/web/src/**/*.test.ts` | route and relay fixtures use the app origin |

The web bundle is built with `VITE_SCCP_API=""` and `VITE_SCCP_RELAY=/relay`,
so it reads `/v1` and `/relay` from whatever origin served it. Moving the app to
`app.` does not change either path, and the node still needs no CORS exception.

## 1. Confirm the project and the zone

```bash
export PROJECT_ID="chessproofofwork"
export REGION="us-central1"
export ZONE="us-central1-a"
export DNS_ZONE="proofofchess-com"
export APEX_HOST="proofofchess.com"
export APP_HOST="app.proofofchess.com"

gcloud config set project "$PROJECT_ID"
gcloud dns managed-zones describe "$DNS_ZONE" \
    --format='value(dnsName,nameServers)'
dig +short NS "$APEX_HOST"
```

The zone must be public, must serve `proofofchess.com.`, and its name servers
must be the ones the registrar delegates to. If `dig NS` returns nothing, fix the
delegation at the registrar **before** creating records; a record in a zone
nobody asks cannot complete the ACME challenge. A new Cloud DNS zone contains
only `NS` and `SOA`: copy any `MX`, `TXT`, `CAA` or verification records from the
old provider before changing the delegation.

The project also contains the older `chessproofofwork-com` zone. Leave its records
in place during the cutover; step 8 retires them.

## 2. Reserve the name

Two `A` records, both pointing at the VM's static address:

```bash
for host in "$APEX_HOST" "$APP_HOST"; do
  gcloud dns record-sets create "$host." \
      --zone="$DNS_ZONE" --type=A --ttl=60 \
      --rrdatas="34.66.79.187" --project="$PROJECT_ID"
done

dig +short "$APEX_HOST" A      # 34.66.79.187
dig +short "$APP_HOST" A       # 34.66.79.187
```

Use `gcloud dns record-sets update` if a record already exists. TTL 60 is
deliberate for the cutover window; raise it to 300 after a week. Do not put a
`CNAME` at the apex: `proofofchess.com` needs an address record. The app
subdomain could be a `CNAME`, but this deployment uses plain `A` records so both
names land on the host running the proxy.

## 3. Harden SSH before the first new-name deploy

The deploy user is `sccp`, key-only. A rebuilt VM does not inherit the hardening,
and an older VM may not have it either, so apply the drop-in from
[`EXPLORER_DEPLOYMENT.md`](EXPLORER_DEPLOYMENT.md#ssh-access-to-the-vm) (it is
idempotent) and verify the effective settings:

```bash
gcloud compute ssh sccp-testnet-1 --zone="$ZONE" --project="$PROJECT_ID" --command '
  sudo install -d -m 755 /etc/ssh/sshd_config.d
  sudo tee /etc/ssh/sshd_config.d/00-sccp-hardening.conf >/dev/null <<"EOF"
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
X11Forwarding no
EOF
  sudo sshd -t && sudo systemctl reload ssh
'

gcloud compute ssh sccp-testnet-1 --zone="$ZONE" --project="$PROJECT_ID" --command \
  "sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|permitrootlogin) '"
```

Expected: `passwordauthentication no`, `kbdinteractiveauthentication no`,
`pubkeyauthentication yes`, `permitrootlogin no`. Then confirm the deploy key
still works and a password is refused:

```bash
ssh -o BatchMode=yes sccp@proofofchess.com true
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    sccp@proofofchess.com true    # Permission denied (publickey)
```

Port 22 stays open to GitHub-hosted runners, which have no fixed egress address;
key-only authentication is what makes that acceptable. A self-hosted runner in
the VPC can restrict the rule to its subnet, or use the IAP range
`35.235.240.0/20` with `gcloud compute ssh --tunnel-through-iap`.

## 4. Update GitHub variables and the pinned host key

```bash
# Repository-level values. If the deploy is scoped to the `testnet` environment
# (as EXPLORER_DEPLOYMENT.md recommends), use `--env testnet` on each command
# instead: environment values override repository values.
gh variable set TESTNET_DEPLOY_HOST --body "sccp@proofofchess.com"
gh variable set TESTNET_PUBLIC_URL  --body "http://app.proofofchess.com:8645"

# The host did not change, but its name did; pin the key under the new name.
ssh-keyscan -H proofofchess.com | gh secret set TESTNET_KNOWN_HOSTS

gh variable delete EXPLORER_DEPLOY_HOST 2>/dev/null || true
gh variable delete EXPLORER_DEPLOY_HOST --env testnet 2>/dev/null || true
```

If the variables or secret are scoped to the `testnet` environment, repeat the
three `set` commands with `--env testnet`; leaving an old environment value in
place keeps the deploy on the old name even after the repository value changes.
The same applies to `EXPLORER_DEPLOY_HOST`: delete it at both levels, as above.

`EXPLORER_DEPLOY_DIR` and `EXPLORER_SSH_KEY` do not need values: the explorer
workflow falls back to `TESTNET_DEPLOY_HOST` and `TESTNET_SSH_KEY`.

Do this **after** `dig` answers in step 2. The deploy SSHes by name, and every
merge to `main` runs it.

## 5. Deploy the chain, then the page

```bash
gh workflow run deploy-testnet-miners.yml
gh run watch

gh workflow run deploy-explorer.yml
gh run watch
```

The testnet deploy rebuilds the chain from the deterministic bootstrap, so expect
height 19, an empty mempool and a fresh faucet. The explorer deploy uploads both
the app and landing releases and obtains the two certificates. Order matters:
nginx proxies `/v1` and `/relay` to `127.0.0.1:8645` and `127.0.0.1:8646`, so the
chain must be up first.

## 6. Verify from outside the VM

```bash
curl -fsS http://app.proofofchess.com:8645/v1/status \
  | jq '{network, height, tip_hash, mempool}'
curl -fsS http://app.proofofchess.com:8646/v1/status | jq '.epoch'

curl -sS  https://app.proofofchess.com/v1/status | jq '.height'
curl -sS  https://app.proofofchess.com/relay/v1/status | jq '.epoch'
curl -sSI https://app.proofofchess.com/ | head -3    # 200, not a TLS error
curl -sSI https://proofofchess.com/    | head -3    # the landing page
curl -sSI http://proofofchess.com/     | head -1    # 308 -> https
```

The hash routes cannot be checked with `curl` because the server always returns
the same document. Open each one in a browser and confirm the view:

| URL | Expected |
| --- | --- |
| `https://proofofchess.com/` | project overview, with links into the app |
| `https://app.proofofchess.com/#/` | explorer dashboard |
| `https://app.proofofchess.com/#/wallet` | wallet overview (or the unlock form) |
| `https://app.proofofchess.com/#/play` | Play, with the wallet gate |
| `https://app.proofofchess.com/#/dex` | DEX swap view |
| `https://app.proofofchess.com/#/bridge` | bridge buy view |

On the VM, `docker compose ps` should show every service healthy in both
`/opt/sccp-testnet` and `/opt/sccp-explorer`, and the `tls` log should say the
certificates for `proofofchess.com` and `app.proofofchess.com` were obtained.

## 7. Rollback

The host did not change, so rollback is a repository rollback, not a DNS move:

```bash
git revert <the single-domain merge>
git push
```

That redeploys the previous Caddyfile and both bundles under the old names. The
old `chessproofofwork-com` records are still in place until step 8, so the old
URLs work again as soon as Caddy reloads. If only GitHub is wrong, revert step 4
first: put `TESTNET_DEPLOY_HOST` and `TESTNET_PUBLIC_URL` back to the old values
and re-run the deploy.

## 8. Retire the old names

After a week of the new app origin serving traffic, remove the subdomain records
so nothing silently depends on them. They do not redirect: the current Caddyfile
stops answering them at cutover, and the records stay only so a revert can bring
the old names back. After this step, a rollback also needs the records recreated.
Remove them with:

```bash
for host in testnet explorer wallet play dex bridge; do
  gcloud dns record-sets delete "$host.chessproofofwork.com." \
      --zone="chessproofofwork-com" --type=A --project="$PROJECT_ID" || true
done
```

The zone itself can stay; it costs almost nothing and the registration may outlive
the deployment. Do not delete the project: it holds the VM, the static address and
the `proofofchess-com` zone. Also remove `VITE_*_ORIGIN` overrides from any local
`.env` file or CI secret: the bundle no longer reads them, and leaving them behind
suggests a second origin still exists.

## What does not move

- Chain state, wallets and faucet grants: the chain is reset on every deploy, so
  the testnet is a new chain. A testnet keyfile is still valid; its balance starts
  over.
- The GitHub repository, package name and
  `ghcr.io/chessproofofwork/somecryptochessproject` image.
- The pinned Stockfish downloads in `data/engines.json`: URL-based, not
  host-based.
- The VM, its static IP, the VPC and the firewall rules.

## See also

- [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) -- the same resources, built
  from nothing, section by section.
- [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md) -- what the deploy workflow
  does on every merge, and the monthly cost.
- [`EXPLORER_DEPLOYMENT.md`](EXPLORER_DEPLOYMENT.md) -- the nginx/Caddy pair,
  rolling back a bundle, the certificate and SSH hardening.
- [`RUNBOOK.md`](RUNBOOK.md) -- health checks and incident steps after the move.


