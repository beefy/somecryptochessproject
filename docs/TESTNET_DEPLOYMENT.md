# Deploying the four-miner testnet

Every merge to `main` runs
[`.github/workflows/deploy-testnet-miners.yml`](../.github/workflows/deploy-testnet-miners.yml):

1. build the stack image and push it to GHCR as
   `ghcr.io/<owner>/<repo>:testnet`;
2. over SSH, copy `docker-compose.yml` to the deployment host;
3. run `docker compose down -v` (fresh chain), pull the new image and start the
   node, bootstrap, genesis demo check (`seed`), relay, four miners (two
   standard, two Chess960) and the two traffic generators (`drip` and `traffic`,
   with extra-miner churn disabled);
4. run `deploy/check_testnet_stack.py` inside the node container and fail the
   workflow if the node has not reached the bootstrap height or the relay has no
   match;
5. request `/v1/status` from the public URL and fail if the node is not serving
   `testnet` through DNS;
6. print the `traffic` and `seed` container logs so the deploy record shows both
   starting up. It is informational: the pool never drains on a chain that also
   runs `drip`, and the generator paces itself in minutes, so gating the deploy
   on either would fail on timing rather than on a broken stack.

The traffic generators add no new port, host, firewall rule or DNS record: they
talk to the node over the Compose network and run beside the miners on the same
VM. Their lifetime is the chain's, which is why they are services in this project
rather than a second workflow like the explorer. `docs/RUNBOOK.md` describes the
loop and why it cannot drain the faucet.

The deploy job is skipped until a host is configured, so the image build is
still useful on a repository without a testnet VM.

## The demo pool and bridge

Testnet genesis already contains the demo DEX and bridge activity, so a fresh
deploy opens with a useful `GET /v1/pools` and `GET /v1/htlcs` without waiting
for a Chess960 proof:

- genesis allocates 5 SCCP and 5 C960 to a published demo liquidity wallet and
  creates the SCCP/C960 pool from them, with the LP shares held by that wallet;
- genesis also records a completed 1-SCCP HTLC between two published demo
  wallets, using the published demo preimage, so the Bridge **Activity** tab is
  populated at height 0;
- the `seed` service is now an idempotent check: it detects the existing pool and
  HTLC and reports them instead of creating duplicates. It can still create both
  if an operator removes the genesis records from a custom test network.

The genesis allocations and records are ordinary consensus state: the pool is in
`state_root`, the HTLC has a derived `htlc_id`, and both are served by the
production node. `seed` still signs with published stack keys, which is safe for
testnet and never acceptable on mainnet. The Ethereum leg remains an operator
deployment: deploying `EthHtlc` on Sepolia needs a funded Ethereum deployer key,
which the SCCP node deliberately does not hold. Set `SCCP_ETH_RPC_URL` and
`SCCP_ETH_HTLC_CONTRACT` after that one-time contract deploy.

## Public endpoint

The current deployment is published at:

```text
http://testnet.chessproofofwork.com:8645
```

Check it from any host:

```bash
curl -sS http://testnet.chessproofofwork.com:8645/v1/status \
  | jq '{network, height, tip_hash, mempool}'
```

The endpoint is plain HTTP and the chain is reset on every deploy. Do not use it
for anything of value. The deploy workflow connects to the same name over SSH
(`sccp@testnet.chessproofofwork.com`); clients only need the node port.

The browser client is a second, independent deploy:
[`deploy-explorer.yml`](../.github/workflows/deploy-explorer.yml) publishes
`src/web/dist` at `https://explorer.chessproofofwork.com`, on the same VM,
behind Caddy (TLS) and an nginx proxy that puts `/v1` and `/relay` on the page's
own origin.
This document covers the chain; the page is in
[`EXPLORER_DEPLOYMENT.md`](EXPLORER_DEPLOYMENT.md).

## One-time setup

For the GCP-side checklist (project, API, firewall, VM, Docker and SSH), see
[`GCP_RESOURCE_SETUP.md`](GCP_RESOURCE_SETUP.md).

Create a GitHub Environment named `testnet` and set:

| Kind | Name | Meaning |
| --- | --- | --- |
| variable | `TESTNET_DEPLOY_HOST` | `user@host`, for example `sccp@testnet.chessproofofwork.com` |
| variable | `TESTNET_DEPLOY_DIR` | optional checkout directory; default `/opt/sccp-testnet` |
| variable | `TESTNET_DEPLOY_PORT` | optional SSH port; default `22` |
| variable | `TESTNET_PUBLIC_URL` | optional public node URL; default `http://testnet.chessproofofwork.com:8645` |
| variable | `SCCP_ETH_RPC_URL` | optional Sepolia RPC for the node's `GET /v1/bridge` document |
| variable | `SCCP_ETH_HTLC_CONTRACT` | optional deployed `EthHtlc` address on Sepolia |
| variable | `SCCP_ETH_CONFIRMATIONS` | optional testnet confirmation-depth override (default `6`) |
| variable | `SCCP_ETH_SCCP_MARGIN` | optional SCCP timelock margin in blocks (default `24`) |
| secret | `TESTNET_SSH_KEY` | private key for that user (ed25519, no passphrase) |
| secret | `TESTNET_KNOWN_HOSTS` | optional pinned host key; without it the workflow runs `ssh-keyscan` |

The host needs Docker Engine with the Compose plugin and ports 8645/8646
reachable from wherever clients will connect. For the current deployment those
are exposed through `testnet.chessproofofwork.com`; port 8645 is the node
API and 8646 is the relay. The workflow authenticates to GHCR with its
job-scoped `GITHUB_TOKEN`; either make the package visible to the VM or keep the
package private and let that token pull it (the job requests `packages: read`).

Neither service terminates TLS. If the node and relay are public, put a reverse
proxy or load balancer in front and firewall 8645/8646 so only that proxy can
reach them. The node API is read-heavy plus transaction/proof submission, and
the faucet is unauthenticated by design; an open port is an open faucet.

The deployment is deliberately destructive: `down -v` deletes the chain and
relay state before every deploy. That is what lets the deterministic bootstrap
start at height 0. Do not point this workflow at a chain whose history matters.

## Monthly cost estimate

The stack is four Stockfish miners (two standard, two Chess960) plus one node and
one relay, all small single-threaded processes. The existing 2 vCPU / 8 GB VM
runs the four at 5 000 nodes per move; a 4 vCPU / 8 GB VM leaves more headroom.
The synthetic extra-miner churn stays off so it cannot compete with the fixed
seats.

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
reserved/committed instance can cut the compute line by 30–40%. The current
deployment uses a Cloud DNS `A` record for
`testnet.chessproofofwork.com`; managed TLS is not included.

What is not included: a public RPC/load balancer, DDoS protection, a managed
database, or long-term log storage. The node uses one SQLite file and the relay a
JSON store, so there is nothing else to run at this scale.

The estimate assumes the chain is reset on each deploy and therefore only
produces the three games the bootstrap epoch allows. A continuously running
public testnet needs a miner availability/reveal loop and a larger game quota;
budget another 2 vCPU per additional pair of miners if you keep the one-core per
miner assumption.
