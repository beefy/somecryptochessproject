# Deploying the two-miner testnet

Every merge to `main` runs
[`.github/workflows/deploy-testnet-miners.yml`](../.github/workflows/deploy-testnet-miners.yml):

1. build the stack image and push it to GHCR as
   `ghcr.io/<owner>/<repo>:testnet`;
2. over SSH, copy `docker-compose.yml` to the deployment host;
3. run `docker compose down -v` (fresh chain), pull the new image and start the
   node, bootstrap, relay and two miners;
4. run `deploy/check_testnet_stack.py` inside the node container and fail the
   workflow if the node has not reached the bootstrap height or the relay has no
   match.

The deploy job is skipped until a host is configured, so the image build is
still useful on a repository without a testnet VM.

## One-time setup

For the GCP-side checklist (project, API, firewall, VM, Docker and SSH), see
[`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md).

Create a GitHub Environment named `testnet` and set:

| Kind | Name | Meaning |
| --- | --- | --- |
| variable | `TESTNET_DEPLOY_HOST` | `user@host`, for example `sccp@testnet.example.org` |
| variable | `TESTNET_DEPLOY_DIR` | optional checkout directory; default `/opt/sccp-testnet` |
| variable | `TESTNET_DEPLOY_PORT` | optional SSH port; default `22` |
| secret | `TESTNET_SSH_KEY` | private key for that user (ed25519, no passphrase) |
| secret | `TESTNET_KNOWN_HOSTS` | optional pinned host key; without it the workflow runs `ssh-keyscan` |

The host needs Docker Engine with the Compose plugin and ports 8645/8646
reachable from wherever clients will connect. The workflow authenticates to
GHCR with its job-scoped `GITHUB_TOKEN`; either make the package visible to the
VM or keep the package private and let that token pull it (the job requests
`packages: read`).

Neither service terminates TLS. If the node and relay are public, put a reverse
proxy or load balancer in front and firewall 8645/8646 so only that proxy can
reach them. The node API is read-heavy plus transaction/proof submission, and
the faucet is unauthenticated by design; an open port is an open faucet.

The deployment is deliberately destructive: `down -v` deletes the chain and
relay state before every deploy. That is what lets the deterministic bootstrap
start at height 0. Do not point this workflow at a chain whose history matters.

## Monthly cost estimate

The stack is two Stockfish miners plus one node and one relay, all small
single-threaded processes. A 4 vCPU / 8 GB VM is comfortable; 2 vCPU / 4 GB
works with a little less headroom.

| Line item | Assumption | Monthly estimate |
| --- | --- | ---: |
| Compute | 4 vCPU / 8 GB, always on, about $0.066/hour | $48 |
| Block storage | 20 GB SSD | $2 |
| Egress | 100 GB (status polling, faucet use, proof traffic) | $1 |
| Snapshots/backups | 20 GB | $2 |
| GHCR image storage | under 1 GB, one retained testnet tag | $0 (free tier) |
| GitHub Actions | build + deploy, roughly 10 minutes per merge | $0 (free tier) |
| **Total** | excluding domain/TLS | **about $53/month** |

That is the mainstream-cloud estimate. A budget VPS with equivalent resources
(Hetzner, OVH, a small provider) is commonly **$10–$20/month**, and a
reserved/committed instance can cut the compute line by 30–40%. Add a domain and
managed TLS if the node and relay are exposed under names rather than IPs.

What is not included: a public RPC/load balancer, DDoS protection, a managed
database, or long-term log storage. The node uses one SQLite file and the relay a
JSON store, so there is nothing else to run at this scale.

The estimate assumes the chain is reset on each deploy and therefore only
produces the three games the bootstrap epoch allows. A continuously running
public testnet needs a miner availability/reveal loop and a larger game quota;
budget another 2 vCPU per additional pair of miners if you keep the one-core per
miner assumption.
