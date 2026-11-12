# Set up the GCP resources

This is a high-level map of the Google Cloud resources the testnet needs. It
is intentionally not a Terraform module or a complete production landing zone:
one operator, one VM, one Docker Compose stack.

The goal is the smallest footprint that can run
[`docker-compose.yml`](../docker-compose.yml), serve the node API on port
`8645`, and receive deploys from the
[`deploy-testnet-miners`](../.github/workflows/deploy-testnet-miners.yml)
workflow.

For exact application settings, environment variables and smoke tests, use
[`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md). This page only covers the
cloud-side setup. It deliberately does not prescribe a budget amount or budget
commands; see [Cost guardrails](#cost-guardrails) for the shape of that work.

---

## What you end up with

| Resource | Purpose | Suggested shape |
| --- | --- | --- |
| Google Cloud project | Holder for billing, API enablement and IAM. | One dedicated project per testnet. |
| Billing account link | Required before Compute Engine can create anything. | Existing account; budgets/alerts added separately. |
| Compute Engine API | The only GCP service the stack needs. | `compute.googleapis.com`. |
| VPC network and subnet | Isolates the VM from the default network. | Custom-mode VPC, one regional subnet. |
| Firewall rules | Allow node API, operator SSH and optionally the relay. | Ingress `8645`, `22`, optional `8646`. |
| VM instance | Runs Docker Compose: one node, one relay, two miners. | Debian 12, `e2-standard-2`, 20–30 GB disk. |
| Static external IP | Optional stable address for DNS and GitHub variables. | Regional address attached to the VM. |
| Deploy user and SSH key | Lets the workflow reach the host without GCP credentials. | `sccp` user, key-only SSH. |
| Docker Engine + Compose plugin | Runs the node, relay and miners. | Installed from Docker's apt repository. |
| GitHub repository variables/secrets | Tell the workflow where and how to deploy. | `TESTNET_DEPLOY_*` variables + `TESTNET_SSH_KEY`. |

You do **not** need GKE, Cloud SQL, a load balancer, Cloud Storage, Artifact
Registry, or a GCP service account for the workflow. Images live in GHCR and
the workflow authenticates to the VM over SSH.

## Before you start

- a Google Cloud account with an active billing account;
- the `gcloud` CLI authenticated locally (`gcloud auth login`);
- permission to create a project and link it to billing;
- a GitHub repository where you can set variables and secrets;
- `curl` and `jq` for the smoke checks;
- optionally the `gh` CLI, for the variable/secret commands below.

The commands assume a bash-like shell and that these variables stay exported for
the whole setup. If you open a new shell, re-export them before continuing.

---

## 1. Project and billing

```bash
gcloud auth login
```

Create a dedicated project. Project IDs are globally unique, so add a suffix:

```bash
export PROJECT_ID="sccp-testnet-${USER}-01"
export REGION="us-central1"
export ZONE="us-central1-a"

gcloud projects create "$PROJECT_ID" --name="SCCP testnet"
gcloud config set project "$PROJECT_ID"
```

Project IDs must be lowercase letters, digits and hyphens; adjust the suffix if
`$USER` contains anything else.

Find the billing account and link it:

```bash
gcloud billing accounts list
export BILLING_ACCOUNT_ID="0X0X0X-0X0X0X-0X0X0X"   # your account ID

gcloud billing projects link "$PROJECT_ID" \
    --billing-account="$BILLING_ACCOUNT_ID"
```

A project without billing can still exist, but Compute Engine refuses to create
instances. Verify:

```bash
gcloud billing projects describe "$PROJECT_ID" \
    --format='value(billingEnabled)'
```

The result must be `True`.

You need `roles/billing.projectManager` or equivalent on the billing account,
and permission to create projects (for example
`roles/resourcemanager.projectCreator`) at the organization or folder level. A
personal GCP account with no organization can do this directly.

---

## 2. Enable the API

```bash
gcloud services enable compute.googleapis.com --project="$PROJECT_ID"
```

Check it:

```bash
gcloud services list --enabled --project="$PROJECT_ID"
```

`compute.googleapis.com` is the only API the testnet uses. The VM pulls public
container images, so no Artifact Registry or Cloud Storage setup is required.

---

## 3. Network and firewall

Use a dedicated VPC rather than the auto-created `default` network, so the
testnet has its own subnet and firewall policy:

```bash
export NETWORK="sccp-testnet"
export SUBNET="sccp-testnet-${REGION}"
export NODE_TAG="sccp-node"

gcloud compute networks create "$NETWORK" \
    --subnet-mode=custom \
    --project="$PROJECT_ID"

gcloud compute networks subnets create "$SUBNET" \
    --network="$NETWORK" \
    --region="$REGION" \
    --range=10.20.0.0/24 \
    --project="$PROJECT_ID"
```

### Node API

The node API is the only endpoint a wallet or faucet client needs:

```bash
gcloud compute firewall-rules create sccp-testnet-node-api \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:8645 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$NODE_TAG" \
    --project="$PROJECT_ID"
```

### Relay, only if external miners connect

In the two-miner deployment both miners run on the same VM and reach the relay
over the Compose network. Leave `8646` closed unless someone outside the VM
needs to submit proofs through the relay:

```bash
gcloud compute firewall-rules create sccp-testnet-relay \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:8646 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$NODE_TAG" \
    --project="$PROJECT_ID"
```

### SSH

The deploy workflow uses plain SSH, not IAP, and GitHub-hosted runners do not
have fixed source addresses. Pick one of these deliberately:

- **Operator only, manual deploys:** allow `tcp:22` from your current IP and run
  the deploy workflow from a self-hosted runner inside that network.
- **GitHub-hosted runner:** allow `tcp:22` from the runner ranges you actually
  use, or front SSH with a bastion/IAP tunnel and adapt the workflow. GitHub's
  published ranges change, so this is operationally heavier than it looks.
- **Simplest demonstration setup:** allow `tcp:22` from `0.0.0.0/0`, keep
  password authentication disabled, use a dedicated key, and accept that SSH is
  internet-exposed.

Example restricted to the operator's current address:

```bash
export ADMIN_CIDR="$(curl -4 -sS https://ifconfig.me)/32"

gcloud compute firewall-rules create sccp-testnet-ssh \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges="$ADMIN_CIDR" \
    --target-tags="$NODE_TAG" \
    --project="$PROJECT_ID"
```

If you use the operator-only rule, remember that the GitHub-hosted workflow
cannot reach the VM until you widen or route that rule. Do not enable SSH
password authentication as a shortcut.

There is no need to open outbound ports: the default egress rules allow the VM
to pull images and software packages.


---

## 4. Static IP and VM

A static external IP is optional, but it keeps the address stable across VM
restarts and makes the GitHub variables and any DNS record stop changing.
Reserve it **before** creating the instance:

```bash
export VM="sccp-testnet-1"
export IP_NAME="sccp-testnet-ip"

gcloud compute addresses create "$IP_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID"

export STATIC_IP="$(gcloud compute addresses describe "$IP_NAME" \
    --region="$REGION" --project="$PROJECT_ID" --format='get(address)')"
echo "$STATIC_IP"
```

Create the VM. `e2-standard-2` (2 vCPU / 8 GB) is a reasonable starting point for
the node, relay and two miners; Stockfish games are CPU-bound, so use
`e2-standard-4` if both miners stay saturated or logs show slow progress. The
20 GB balanced disk holds the Debian image, Docker layers, the chain database and
logs.

```bash
gcloud compute instances create "$VM" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type=e2-standard-2 \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-balanced \
    --network="$NETWORK" \
    --subnet="$SUBNET" \
    --address="$STATIC_IP" \
    --tags="$NODE_TAG" \
    --scopes=default
```

Drop `--address` to use an ephemeral IP instead. Keep `--tags="$NODE_TAG"` or
the firewall rules will not apply.

Check the instance and the effective firewall rules:

```bash
gcloud compute instances describe "$VM" \
    --zone="$ZONE" \
    --format='table(name, status, networkInterfaces[0].accessConfigs[0].natIP)'

gcloud compute firewall-rules list \
    --filter="network:$NETWORK" \
    --format='table(name, direction, allowed[].map().firewall_rule(), sourceRanges)'
```

Record the external IP. If you skipped the static address, copy the `natIP` from
the first command and export it before continuing:

```bash
export STATIC_IP="203.0.113.10"   # the natIP from the describe output
```

The rest of this guide (SSH test, GitHub variables, smoke checks) uses
`$STATIC_IP`.

### Optional DNS

Create an `A` record such as `testnet.example.org` pointing at the static IP,
then use that name in `TESTNET_DEPLOY_HOST`. There is no GCP load balancer or
managed certificate in this setup, and the node still speaks plain HTTP on
`8645`.


---

## 5. Docker Engine and Compose

The VM must have Docker Engine plus the Compose v2 plugin. The workflow runs
`docker compose`, not the old `docker-compose` binary.

SSH to the new instance:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT_ID"
```

Install Docker from Docker's apt repository (the commands below are for the
Debian 12 image):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
docker --version
docker compose version
```

If `docker compose version` fails, the plugin did not install; fix that before
continuing. The workflow will fail with `docker: 'compose' is not a docker
command` otherwise.

Leave the SSH session open for the next section.

---

## 6. Deploy user and SSH key

The workflow logs in as a dedicated unprivileged user. That user only needs
Docker access and a writable deploy directory; it does not need `sudo`.

On the VM:

```bash
sudo useradd --create-home --shell /bin/bash sccp 2>/dev/null || true
sudo usermod -aG docker sccp
sudo install -d -m 755 -o sccp -g sccp /opt/sccp-testnet
id sccp
```

Membership in the `docker` group is effectively root-equivalent on the host.
That is acceptable for a single-tenant testnet VM, but do not reuse this user or
key anywhere else.

On your workstation, create a dedicated key with no passphrase (the workflow
cannot answer a prompt):

```bash
ssh-keygen -t ed25519 -C "sccp-testnet-deploy" \
    -f ~/.ssh/sccp_testnet_deploy -N ""
```

Install the public half for `sccp`:

```bash
gcloud compute scp ~/.ssh/sccp_testnet_deploy.pub \
    "$VM":/tmp/sccp-deploy.pub \
    --zone="$ZONE" --project="$PROJECT_ID"

gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT_ID" --command \
    "sudo install -d -m 700 -o sccp -g sccp /home/sccp/.ssh && \
     sudo install -m 600 -o sccp -g sccp /tmp/sccp-deploy.pub \
       /home/sccp/.ssh/authorized_keys && \
     rm -f /tmp/sccp-deploy.pub"
```

Test the exact login the workflow will use, from your workstation:

```bash
ssh -i ~/.ssh/sccp_testnet_deploy sccp@"$STATIC_IP" \
    'id && docker version --format "{{.Server.Version}}" && \
     docker compose version && ls -ld /opt/sccp-testnet'
```

If this prompts for a password, the key or permissions are wrong. Fix that
before touching GitHub. The workflow will not fall back to a password.


---

## 7. Wire the deployment workflow

The workflow needs no GCP credentials. It builds the image in GitHub Actions,
then SSHes to the VM and runs Compose there. Configure the repository (or the
`testnet` environment, if you scope them there) with:

| Kind | Name | Value |
| --- | --- | --- |
| variable | `TESTNET_DEPLOY_HOST` | `sccp@<static-ip-or-dns>` |
| variable | `TESTNET_DEPLOY_DIR` | `/opt/sccp-testnet` |
| variable | `TESTNET_DEPLOY_PORT` | `22` (optional, defaults to 22) |
| secret | `TESTNET_SSH_KEY` | contents of `~/.ssh/sccp_testnet_deploy` |
| secret | `TESTNET_KNOWN_HOSTS` | `ssh-keyscan -H <host>` output (optional) |

Create an environment named `testnet` in **Settings → Environments**. No
protection rules are needed for a demonstration; add them if a human should
approve every deploy. The remote deploy job stays skipped until
`TESTNET_DEPLOY_HOST` is set, so the image build remains useful without a VM.

Using the GitHub CLI from the repository root:

```bash
gh variable set TESTNET_DEPLOY_HOST --body "sccp@$STATIC_IP"
gh variable set TESTNET_DEPLOY_DIR  --body "/opt/sccp-testnet"
gh secret set TESTNET_SSH_KEY < ~/.ssh/sccp_testnet_deploy

ssh-keyscan -H "$STATIC_IP" > /tmp/sccp-known-hosts 2>/dev/null
gh secret set TESTNET_KNOWN_HOSTS < /tmp/sccp-known-hosts
```

`TESTNET_KNOWN_HOSTS` pins the host key. Without it the workflow falls back to
`ssh-keyscan`, which is vulnerable to a first-use man-in-the-middle on a
network you do not trust. Set it.

No permanent registry credential is needed. The deploy job pipes its job-scoped
`GITHUB_TOKEN` to `docker login` on the VM; that token has `packages: read`. Keep
the GHCR package linked to this repository, or otherwise grant the repository
access, so the token can pull it. Do not store a long-lived PAT on the VM.

The exact variable meanings and a manual Compose invocation are also documented
in [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md). Do not duplicate the
application environment there; this section only makes the cloud host reachable.

---

## 8. First deploy and smoke check

Trigger the workflow:

```bash
gh workflow run deploy-testnet-miners.yml
gh run watch
```

Or push a commit to `main`; the workflow is configured for both.

When it finishes, check the host:

```bash
ssh -i ~/.ssh/sccp_testnet_deploy sccp@"$STATIC_IP" \
    'cd /opt/sccp-testnet && docker compose ps'
```

Check the API from your workstation:

```bash
curl -sS "http://$STATIC_IP:8645/v1/status" \
    | jq '{network, height, tip_hash, mempool}'

curl -sS "http://$STATIC_IP:8645/v1/faucet" | jq
```

Expected after a successful deploy: network `testnet`, height `19` after
bootstrap, and a faucet policy response. The workflow also runs
`deploy/check_testnet_stack.py` against the node and relay, so a green deploy job
already proves the internal Compose network works.

The deployment resets the chain on every run (`docker compose down -v`). All
wallets, balances and faucet grants from a previous deploy are discarded. This
is intentional for the deterministic demonstrator.

To fund a wallet against this host, follow
[`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) with
`SCCP_NODE="http://$STATIC_IP:8645"`.


---

## 9. Cost guardrails

At this size, cost is dominated by the VM, its disk and the optional unattached
static IP. Keep the setup boring:

- one VM, one 20–30 GB disk, no managed databases or load balancers;
- delete the static IP if you delete the VM, because an unattached address is
  still billed;
- a Cloud Billing **budget** is an alert, not a hard cap. Set one in
  **Billing → Budgets & alerts** if you want email or Pub/Sub notification, but
  do not rely on it to stop spend;
- the closer thing to a hard ceiling is a Compute Engine quota on CPUs or
  instances, or simply deleting the VM when the demonstration is over.

This page intentionally stops at that overview. Budget amounts, thresholds and
programmatic notification wiring are a separate billing decision, not a
prerequisite for creating the testnet VM.

---

## 10. Cleanup

Delete the billable resources when the demonstration is over. The short version
is to delete the project, which removes the VM, disk, static IP, firewall rules
and network together:

```bash
gcloud projects delete "$PROJECT_ID" --quiet
```

To keep the project but stop the compute charges:

```bash
gcloud compute instances delete "$VM" \
    --zone="$ZONE" --delete-disks=all --quiet

gcloud compute addresses delete "$IP_NAME" \
    --region="$REGION" --quiet
```

Delete the static address even if you keep the VM's disk, otherwise the
unattached address continues to bill. Firewall rules and an empty VPC have no
separate charge.

Deleting the project does not clean up the GHCR package or the GitHub
environment; remove those separately if you are decommissioning the repository.

---

## 11. Troubleshooting the cloud setup

| Symptom | Likely cause | Check |
| --- | --- | --- |
| `billingEnabled` is `False` | Project is not linked to a billing account. | `gcloud billing projects describe "$PROJECT_ID"`. |
| `Compute Engine API has not been used` | API enablement failed or wrong project. | `gcloud services list --enabled --project="$PROJECT_ID"`. |
| `Quota 'CPUS' exceeded` | Regional CPU quota too low. | IAM & Admin → Quotas, or request a small increase. |
| SSH times out | Firewall rule missing, source CIDR wrong, or GitHub runner IP not allowed. | `gcloud compute firewall-rules list --filter="network:$NETWORK"`. |
| `Permission denied (publickey)` | Wrong key, wrong user, or bad `authorized_keys` ownership. | Re-run the `sccp@` login test with `ssh -v`. |
| `docker: 'compose' is not a docker command` | Compose v2 plugin missing. | `docker compose version` on the VM. |
| GHCR `unauthorized` during deploy | Package visibility or workflow token permissions. | Check the build job and package settings. |
| `curl :8645` times out | VM lacks `sccp-node` tag, firewall rule is wrong, or the stack is not running. | `docker compose ps` and the firewall list. |
| Deploy succeeds but faucet `404` | Node started with `--no-faucet`. | Compose environment and `docker compose logs node`. |
| Chain height resets after every deploy | Expected: the workflow runs `docker compose down -v`. | [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md). |

---

## See also

- [`TESTNET_DEPLOYMENT.md`](TESTNET_DEPLOYMENT.md) — compose settings, workflow
  details and smoke tests.
- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — fund a wallet
  against the deployed node.
- [`RUNBOOK.md`](RUNBOOK.md) — operating and recovering the testnet.
- [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — miner-side setup.

