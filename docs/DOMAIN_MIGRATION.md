# Moving the deployment to `proofofchess.com`

The public testnet and the web application now live on one name,
`proofofchess.com`, in the existing `chessproofofwork` Google Cloud project. The
five subdomains (`explorer.`, `wallet.`, `play.`, `dex.`, `bridge.`) are gone:
they were never five deployments, only five names for one bundle, and the bundle
now selects its view from the hash route instead of the hostname.

This page is the cutover record and the rebuild recipe: what already exists, what
has to be created, the order to do it in, and how to get back.

**Status (2026-11-17): the `proofofchess-com` zone is delegated, its apex `A`
record points at the VM, and the GitHub variables name the new host. The
one-origin code is in this commit; merging it deploys the bundle and issues the
certificate. The old subdomain records stay in place for rollback until step 8.**
The GCP project, VM, network, static IP, deploy user and GitHub environment
already exist. The only new cloud resource is the `proofofchess-com` Cloud DNS
zone; the only changed settings are its `A` record and the GitHub variables that
name the host. The repository changes (web routing, Caddyfile, docs) are in the
same commit as this page.

## Before and after

| | Before | After |
| --- | --- | --- |
| Public origin | `https://explorer.chessproofofwork.com`, `https://wallet.chessproofofwork.com`, `https://play.chessproofofwork.com`, `https://dex.…`, `https://bridge.…` | `https://proofofchess.com` |
| View selection | hostname (`siteForHostname`) | hash route: `#/`, `#/wallet`, `#/play`, `#/dex`, `#/bridge` |
| Certificates | one per name, five ACME orders | one certificate for the apex |
| DNS | five `A` records in `chessproofofwork-com` | one `A` record in `proofofchess-com` |
| Node API | `http://testnet.chessproofofwork.com:8645` | `http://proofofchess.com:8645`, plus `https://proofofchess.com/v1` through nginx |
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
| `src/web/src/lib/site.ts` | dropped `SITE_ORIGINS`, `siteForHostname`, `currentSite`, `crossSiteHref` and `hashUrl`; one `siteHash()` map is now the whole routing contract |
| `src/web/src/App.tsx` | the dashboard no longer depends on the hostname; the tab title follows the active view |
| `src/web/src/main.tsx` | removed the hostname-based title selection |
| `deploy/explorer/Caddyfile` | one `proofofchess.com` site block, one certificate |
| `deploy/explorer/nginx.conf`, `docker-compose.yml` | comments describe one origin, not five names |
| `.github/workflows/deploy-explorer.yml` | comments and the one-name publish description |
| `.github/workflows/deploy-testnet-miners.yml` | `TESTNET_PUBLIC_URL` default is `http://proofofchess.com:8645` |
| `README.md`, `deploy/README.md`, `docs/*.md`, `analysis/*` | every `curl`, `--peer`, `--node`, `dig` and browser URL |
| `src/web/src/**/*.test.ts` | route and relay fixtures use the new origin |

The web bundle is built with `VITE_SCCP_API=""` and `VITE_SCCP_RELAY=/relay`,
so it reads `/v1` and `/relay` from whatever origin served it. Moving the name
does not change either path, and the node still needs no CORS exception.

## 1. Confirm the project and the zone

```bash
export PROJECT_ID="chessproofofwork"
export REGION="us-central1"
export ZONE="us-central1-a"
export DNS_ZONE="proofofchess-com"
export WEB_HOST="proofofchess.com"

gcloud config set project "$PROJECT_ID"
gcloud dns managed-zones describe "$DNS_ZONE" \
    --format='value(dnsName,nameServers)'
dig +short NS "$WEB_HOST"
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

One `A` record at the apex, pointing at the VM's static address:

```bash
gcloud dns record-sets create "$WEB_HOST." \
    --zone="$DNS_ZONE" --type=A --ttl=60 \
    --rrdatas="34.66.79.187" --project="$PROJECT_ID"

dig +short "$WEB_HOST" A      # 34.66.79.187
```

Use `gcloud dns record-sets update` if the record already exists. TTL 60 is
deliberate for the cutover window; raise it to 300 after a week. Do not put a
`CNAME` at the apex: `proofofchess.com` needs an address record, and the node API
and relay are paths on that same host.

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
gh variable set TESTNET_PUBLIC_URL  --body "http://proofofchess.com:8645"

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
height 19, an empty mempool and a fresh faucet. The explorer deploy uploads the
bundle and obtains the one certificate. Order matters: nginx proxies `/v1` and
`/relay` to `127.0.0.1:8645` and `127.0.0.1:8646`, so the chain must be up first.

## 6. Verify from outside the VM

```bash
curl -fsS http://proofofchess.com:8645/v1/status \
  | jq '{network, height, tip_hash, mempool}'
curl -fsS http://proofofchess.com:8646/v1/status | jq '.epoch'

curl -sS  https://proofofchess.com/v1/status | jq '.height'
curl -sS  https://proofofchess.com/relay/v1/status | jq '.epoch'
curl -sSI https://proofofchess.com/ | head -3    # 200, not a TLS error
curl -sSI http://proofofchess.com/  | head -1    # 308 -> https
```

The hash routes cannot be checked with `curl` because the server always returns
the same document. Open each one in a browser and confirm the view:

| URL | Expected |
| --- | --- |
| `https://proofofchess.com/#/` | explorer dashboard |
| `https://proofofchess.com/#/wallet` | wallet overview (or the unlock form) |
| `https://proofofchess.com/#/play` | Play, with the wallet gate |
| `https://proofofchess.com/#/dex` | DEX swap view |
| `https://proofofchess.com/#/bridge` | bridge buy view |

On the VM, `docker compose ps` should show every service healthy in both
`/opt/sccp-testnet` and `/opt/sccp-explorer`, and the `tls` log should say the
certificate for `proofofchess.com` was obtained.

## 7. Rollback

The host did not change, so rollback is a repository rollback, not a DNS move:

```bash
git revert <the single-domain merge>
git push
```

That redeploys the previous Caddyfile and bundle under the old names. The old
`chessproofofwork-com` records are still in place until step 8, so the old URLs
work again as soon as Caddy reloads. If only GitHub is wrong, revert step 4 first:
put `TESTNET_DEPLOY_HOST` and `TESTNET_PUBLIC_URL` back to the old values and
re-run the deploy.

## 8. Retire the old names

After a week of the new origin serving traffic, remove the subdomain records so
nothing silently depends on them. They do not redirect: the single-name Caddyfile
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


