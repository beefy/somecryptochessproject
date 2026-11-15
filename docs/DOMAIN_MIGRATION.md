# Moving the deployment to `chessproofofwork.com`

The public testnet and the explorer now live under `chessproofofwork.com`, in a
new Google Cloud project. This page is the cutover: what already exists, what
still has to be created, the order to do it in, and how to get back.

**Status (2026-11-15): infrastructure complete, deployment not yet run.** The
project, network, firewall, static IP, VM, Docker host, deploy user, DNS records,
GitHub variables and secrets are all in place. The chain and the explorer are
deployed by `deploy-testnet-miners.yml` and `deploy-explorer.yml`. The steps below
are the record of what was done and the rebuild recipe for the next project; the
concrete values are in [As built](#as-built).

The repository side is done -- every name in the code, the workflows and the
docs is already the new one:

| Where | What changed |
| --- | --- |
| `deploy/explorer/Caddyfile` | the site label, which is also the Let's Encrypt name |
| `.github/workflows/deploy-testnet-miners.yml` | the `TESTNET_PUBLIC_URL` default |
| `.github/workflows/deploy-explorer.yml`, `README.md`, `deploy/README.md` | comments and prose |
| `src/web/src/lib/relay.test.ts` | the page origin in the relay fixture |
| `docs/*.md`, `analysis/*` | every `curl`, `--peer`, `--node` and `dig` example |

Nothing about the chain moves: no genesis file, no protocol constant, no storage
format. A testnet redeploy starts from an empty chain anyway, so there is no
state to migrate.

## The two deployments

| | Old | New |
| --- | --- | --- |
| Project | `project-title-here` (797291709791) | `chessproofofwork` (206061816244) |
| Node name | `testnet.somecryptochessproject.com` | `testnet.chessproofofwork.com` |
| Explorer name | `explorer.somecryptochessproject.com` | `explorer.chessproofofwork.com` |
| Cloud DNS zone | `somecryptochessproject-com` | `chessproofofwork-com` |
| VM | `sccp-testnet-1`, `us-central1-a`, `e2-standard-2`, 20 GB `pd-balanced`, Debian 12 | the same name and shape, no service account (step 3) |
| Network | custom VPC `sccp-testnet`, subnet `10.20.0.0/24`, tag `sccp-node` | one-to-one, same names, no peering |
| Static IP | `sccp-testnet-ip` = `34.61.101.81` | `sccp-testnet-ip` = `34.66.79.187` |
| Firewall | `tcp:80,443`, `tcp:8645`, `tcp:8646`, `tcp:22`, all tagged `sccp-node` | one-to-one, same names |

## What was already there (checked 2026-11-14)

- The project `chessproofofwork` existed, and so did its Cloud DNS zone
  `chessproofofwork-com`: public, serving `chessproofofwork.com.`, with Google
  Domains already delegating to `ns-cloud-d1..d4.googledomains.com`. Records
  created with `gcloud dns` go live within the TTL.
- No `A` records: neither name resolved.
- Only `dns.googleapis.com` was enabled; `compute.googleapis.com` was not.
- The old project still runs `sccp-testnet-1` at `34.61.101.81` with both old
  names pointing at it. Leave it alone until step 9.

## As built

Everything below was done on 2026-11-15 and verified from a shell, not merely
created:

| Resource | Value |
| --- | --- |
| Project | `chessproofofwork` (206061816244), billing account `01CF82-258F97-56B4CD` |
| API | `compute.googleapis.com` enabled; billing was already linked (`billingEnabled: true`) |
| VPC | `sccp-testnet`, custom mode, no peering |
| Subnet | `sccp-testnet-us-central1`, `us-central1`, `10.20.0.0/24` |
| Firewall | `sccp-testnet-ssh` (22), `-node-api` (8645), `-relay` (8646), `-explorer-web` (80,443); all `0.0.0.0/0`, all on tag `sccp-node` |
| Static IP | `sccp-testnet-ip` = `34.66.79.187`, attached (so it does not bill as idle) |
| VM | `sccp-testnet-1`, `us-central1-a`, `e2-standard-2`, 20 GB `pd-balanced`, Debian 12, tag `sccp-node`, internal `10.20.0.2`, **no service account** |
| Docker | Engine 29.9.3, Compose v5.7.1 |
| Deploy user | `sccp`, uid 1002, member of `docker`, **not** of `sudo`; owns `/opt/sccp-testnet` and `/opt/sccp-explorer`; `~/.ssh/sccp_testnet_deploy.pub` installed |
| DNS | `testnet.` and `explorer.chessproofofwork.com.`, type `A`, `34.66.79.187`, TTL 60, zone `chessproofofwork-com` |
| GitHub | vars `TESTNET_DEPLOY_HOST`, `TESTNET_PUBLIC_URL`; secret `TESTNET_KNOWN_HOSTS` refreshed (step 6) |
| Budget | `chessproofofwork testnet`, $75/month, project-scoped, alerts at 50/90/100% |

Two details that are easy to lose when the VM is rebuilt:

- The instance is created with the project's default compute service account,
  which holds `roles/editor`. Before the first deploy it was stopped, set with
  `--no-service-account --no-scopes` and started again, so a container escape on
  the VM cannot mint a Google token:

  ```bash
  gcloud compute instances stop sccp-testnet-1 --zone="$ZONE"
  gcloud compute instances set-service-account sccp-testnet-1 --zone="$ZONE" \
      --no-service-account --no-scopes
  gcloud compute instances start sccp-testnet-1 --zone="$ZONE"
  ```

  Verify from the VM:
  `curl -s -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token`
  answers `404`.
- `oslogin.googleapis.com` is enabled in the project, but OS Login is **not**
  active: nothing sets `enable-oslogin`, so metadata `ssh-keys` still decide who
  logs in. Setting `enable-oslogin=TRUE` is the clean way to move human access to
  IAM and drop the project-wide key.

## Two ways to move, and why this guide takes the second

The disk can be carried over: snapshot the old boot disk, create an image in the
new project with `--source-snapshot`, and start an instance from it. That keeps
`/opt`, the Docker install and `~/.ssh`.

It is not worth it here. Every `deploy-testnet-miners` run does
`docker compose down -v`, so the old VM holds no chain state, no wallets and no
database -- only a Docker install that section 5 of
[`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) rebuilds in ten minutes. A
fresh VM also keeps the old one running untouched, which is what makes the
rollback in step 8 a DNS edit.

## 1. Finish the new project

```bash
export PROJECT_ID="chessproofofwork"
export REGION="us-central1"
export ZONE="us-central1-a"

gcloud config set project "$PROJECT_ID"

# Link billing if it is not linked yet (see GCP_RESOURCE_SETUP.md section 1).
gcloud billing accounts list
gcloud billing projects link "$PROJECT_ID" --billing-account=0X0X0X-0X0X0X-0X0X0X
gcloud billing projects describe "$PROJECT_ID" | grep billingEnabled

gcloud services enable compute.googleapis.com
```

`billingEnabled: false` makes every step below fail with "Compute Engine API has
not been used" or a quota error that names neither the project nor the fix.

## 2. Network and firewall

Same shape as the old project, so the deploy docs and the `sccp-node` tag keep
working unchanged:

```bash
gcloud compute networks create sccp-testnet --subnet-mode=custom

gcloud compute networks subnets create sccp-testnet-us-central1 \
    --network=sccp-testnet --region="$REGION" --range=10.20.0.0/24

gcloud compute firewall-rules create sccp-testnet-ssh \
    --network=sccp-testnet --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 --source-ranges=0.0.0.0/0 --target-tags=sccp-node

gcloud compute firewall-rules create sccp-testnet-node-api \
    --network=sccp-testnet --direction=INGRESS --action=ALLOW \
    --rules=tcp:8645 --source-ranges=0.0.0.0/0 --target-tags=sccp-node

gcloud compute firewall-rules create sccp-testnet-relay \
    --network=sccp-testnet --direction=INGRESS --action=ALLOW \
    --rules=tcp:8646 --source-ranges=0.0.0.0/0 --target-tags=sccp-node

gcloud compute firewall-rules create sccp-testnet-explorer-web \
    --network=sccp-testnet --direction=INGRESS --action=ALLOW \
    --rules=tcp:80,tcp:443 --source-ranges=0.0.0.0/0 --target-tags=sccp-node
```

`tcp:80` is not optional even though Caddy serves 443: the Let's Encrypt HTTP-01
challenge arrives on 80, and without the rule the certificate never issues and
`https://explorer.chessproofofwork.com` serves a TLS error instead.

## 3. Static IP and the VM

```bash
gcloud compute addresses create sccp-testnet-ip --region="$REGION"
export STATIC_IP="$(gcloud compute addresses describe sccp-testnet-ip \
    --region="$REGION" --format='value(address)')"
echo "$STATIC_IP"

gcloud compute instances create sccp-testnet-1 \
    --zone="$ZONE" \
    --machine-type=e2-standard-2 \
    --network=sccp-testnet \
    --subnet=sccp-testnet-us-central1 \
    --address=sccp-testnet-ip \
    --tags=sccp-node \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=20GB --boot-disk-type=pd-balanced
```

The address is reserved before the VM, so DNS never has to change again if the
instance is rebuilt. `e2-standard-2` is what the old VM runs: two Stockfish
miners, a node, a relay and the traffic generators fit, and little else does.

## 4. Docker, the deploy user and the key

Follow [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) sections 5 and 6 against
the new instance. In short: Docker Engine plus the Compose v2 plugin, a `sccp`
user in the `docker` group, and the two deploy directories:

```bash
sudo install -d -m 755 -o sccp -g sccp /opt/sccp-testnet
sudo install -d -m 755 -o sccp -g sccp /opt/sccp-explorer
```

The existing deploy key can be reused: install
`~/.ssh/sccp_testnet_deploy.pub` for `sccp` on the new VM exactly as section 6
shows. The *host* key is new, which is why the `TESTNET_KNOWN_HOSTS` secret has
to be refreshed in step 6.

## 5. DNS: the moment of the switch

Do this after the VM exists and before the first deploy, so the workflow's
`curl` check and Caddy's challenge both find a live name:

```bash
export DNS_ZONE="chessproofofwork-com"

gcloud dns record-sets create "testnet.chessproofofwork.com." \
    --zone="$DNS_ZONE" --type=A --ttl=60 \
    --rrdatas="$STATIC_IP" --project="$PROJECT_ID"

gcloud dns record-sets create "explorer.chessproofofwork.com." \
    --zone="$DNS_ZONE" --type=A --ttl=60 \
    --rrdatas="$STATIC_IP" --project="$PROJECT_ID"

dig +short testnet.chessproofofwork.com
dig +short explorer.chessproofofwork.com
```

TTL 60 is deliberate: the rollback in step 8 is the same two commands pointed at
the old address, and nothing should sit in a resolver cache for an hour. The old
`A` records in the old zone stay in place until then.

## 6. GitHub variables and secrets

The deploy job reads these repository-level values. They named the old host until
2026-11-15; these are the commands that set them, and the ones to re-run if a
value is ever lost:

```bash
gh variable set TESTNET_DEPLOY_HOST \
    --body "sccp@testnet.chessproofofwork.com"
gh variable set TESTNET_PUBLIC_URL \
    --body "http://testnet.chessproofofwork.com:8645"

# The new VM has a new host key; the old secret pins the old one, and a
# mismatch fails the deploy before it runs a single command.
ssh-keyscan -H testnet.chessproofofwork.com | gh secret set TESTNET_KNOWN_HOSTS
```

`TESTNET_SSH_KEY` and `TESTNET_DEPLOY_DIR` do not change. The explorer workflow
falls back to `TESTNET_DEPLOY_HOST` when `EXPLORER_DEPLOY_HOST` is unset, which
is how it is configured today; set `EXPLORER_DEPLOY_HOST` only if the two ever
live on different machines.

Set these **after** `dig` answers in step 5. The deploy SSHes by name, so the
workflow fails with a DNS error if the record is not live yet -- and every merge
to `main` runs it.

## 7. Deploy and verify

```bash
gh workflow run deploy-testnet-miners.yml
gh run watch

gh workflow run deploy-explorer.yml
gh run watch
```

The testnet deploy rebuilds the chain from the deterministic bootstrap, so expect
height 19 with an empty mempool and a fresh faucet. Then check from a machine that
is not the VM:

```bash
curl -fsS http://testnet.chessproofofwork.com:8645/v1/status \
  | jq '{network, height, tip_hash, mempool}'
curl -fsS http://testnet.chessproofofwork.com:8646/v1/status | jq '.epoch'

curl -sS  https://explorer.chessproofofwork.com/v1/status | jq '.height'
curl -sS  https://explorer.chessproofofwork.com/relay/v1/status | jq '.epoch'
curl -sSI https://explorer.chessproofofwork.com/ | head -3    # 200, not a TLS error
curl -sSI http://explorer.chessproofofwork.com/  | head -1    # 308 -> https
```

On the VM, `docker compose ps` in `/opt/sccp-testnet` and `/opt/sccp-explorer`
should show every service healthy, and the explorer's `caddy` log should say the
certificate for `explorer.chessproofofwork.com` was obtained. The rest of the
expected log lines are in [`RUNBOOK.md`](RUNBOOK.md).

The two stacks start in that order on purpose: the explorer's nginx proxies
`/v1` and `/relay` to `127.0.0.1:8645` and `127.0.0.1:8646`, so the chain has to
be up before the page has anything to read.

## 8. Rollback

Until step 9 the old VM still runs the old stack on `34.61.101.81`, with its
certificate in the `caddy_data` volume and its chain intact:

```bash
gcloud dns record-sets update "testnet.chessproofofwork.com." \
    --zone="$DNS_ZONE" --type=A --ttl=60 \
    --rrdatas="34.61.101.81" --project="$PROJECT_ID"
gcloud dns record-sets update "explorer.chessproofofwork.com." \
    --zone="$DNS_ZONE" --type=A --ttl=60 \
    --rrdatas="34.61.101.81" --project="$PROJECT_ID"

gh variable set TESTNET_DEPLOY_HOST \
    --body "sccp@testnet.somecryptochessproject.com"
gh variable set TESTNET_PUBLIC_URL \
    --body "http://testnet.somecryptochessproject.com:8645"
```

`--rrdatas` replaces the record set wholesale, which is what makes this a
one-command move. Reverting the two variables matters as much as the DNS: with
the old host restored, deploys and the smoke check go back to the old VM too.

## 9. Decommission the old deployment -- carefully

**Do not delete the old project.** `project-title-here` also holds the
`bobtheraspberrypi.com` and `textmmo.com` DNS zones; deleting it takes those
with it. Delete only the sccp resources, and only after a week of the new names
being up:

```bash
gcloud dns record-sets delete "testnet.somecryptochessproject.com." \
    --zone="somecryptochessproject-com" --type=A --project="project-title-here"
gcloud dns record-sets delete "explorer.somecryptochessproject.com." \
    --zone="somecryptochessproject-com" --type=A --project="project-title-here"

gcloud compute instances delete sccp-testnet-1 \
    --zone=us-central1-a --project="project-title-here"
gcloud compute addresses delete sccp-testnet-ip \
    --region=us-central1 --project="project-title-here"
```

The VPC, subnet and firewall rules can go too once nothing else uses them. Keep
the DNS zone itself: it costs almost nothing and the domain registration may
outlive the deployment.

Then retire the credentials that were shared while both hosts were live:

- `TESTNET_SSH_KEY` is authorized on the old VM as well, so until that instance is
  gone one private key opens both hosts. Generate a new deploy key, install it for
  `sccp` on the surviving VM, update the secret, and only then delete the old key.
- The project-level `ssh-keys` metadata entry (`nate:ssh-rsa ...`) creates a
  `google-sudoers` member on **every** instance in either project. Removing it is
  the difference between one credential per host and one credential for
  everything.
- Raise the two `A` record TTLs from 60 to 300 once the new host has been up for
  a week; the short TTL exists only for the rollback window.

While doing any of this, remember that a local `gcloud` config still defaults to
`project-title-here`, and both projects contain a VM called `sccp-testnet-1`, a
VPC called `sccp-testnet` and identically named firewall rules. Pass `--project`
on every destructive command, or pin the config first:

```bash
gcloud config set project chessproofofwork
```

## What does not move

- Chain state, wallets and faucet grants: the chain is reset on every deploy, so
  the new testnet is a new chain. A testnet keyfile is still valid; its balance
  starts over.
- The GitHub repository, the package name and the
  `ghcr.io/chessproofofwork/somecryptochessproject` image: the domain does not
  touch them.
- The pinned Stockfish downloads in `data/engines.json`: URL-based, not
  host-based.

## See also

- [`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md) -- the same resources, built
  from nothing, section by section.
- [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md) -- what the deploy workflow
  does on every merge, and the monthly cost.
- [`EXPLORER_DEPLOYMENT.md`](EXPLORER_DEPLOYMENT.md) -- the nginx/Caddy pair,
  rolling back a bundle, and the certificate.
- [`RUNBOOK.md`](RUNBOOK.md) -- health checks and incident steps after the move.



