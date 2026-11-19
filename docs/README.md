# Documentation

The pages are grouped by what you are trying to do. Pick a row, or read the
folder table below it.

| I want to... | Read |
| --- | --- |
| get coins into a testnet wallet | [`guides/CREATE_A_TESTNET_WALLET.md`](guides/CREATE_A_TESTNET_WALLET.md) |
| know the exact wire format or consensus rule | [`design/PROTOCOL.md`](design/PROTOCOL.md) |
| watch a game become a block, then export it | [`guides/PLAY_AND_EXPORT.md`](guides/PLAY_AND_EXPORT.md) |
| run a whole network on this machine | [`setup/RUN_A_DEVNET.md`](setup/RUN_A_DEVNET.md) |
| deploy to a cloud VM | [`setup/GCP_RESOURCE_SETUP.md`](setup/GCP_RESOURCE_SETUP.md) |
| know what healthy looks like, and how to restart it | [`operations/RUNBOOK.md`](operations/RUNBOOK.md) |
| handle an outage: triage, fix, prove it, write it up | [`operations/README.md`](operations/README.md) |
| exercise the bridge, the DEX and Chess960 | [`guides/TEST_DEVNET_BRIDGE_DEX.md`](guides/TEST_DEVNET_BRIDGE_DEX.md) |
| see what is still unfinished | [`design/IMPLEMENTATION_PLAN.md`](design/IMPLEMENTATION_PLAN.md) |

## The four folders

| Folder | For | Authority |
| --- | --- | --- |
| [`guides/`](guides) | using the CLI, the wallet and the web app: a task with a beginning and an end | describes the shipped commands |
| [`setup/`](setup) | standing things up: a devnet, a miner, a VM, a deploy workflow, a domain move | describes how this repository is built and deployed |
| [`operations/`](operations) | running what is already deployed: health, failure scenarios, incidents | describes how this deployment is repaired |
| [`design/`](design) | the protocol, the threat model, the design and the roadmap | `PROTOCOL.md` is **normative**; the rest is explanatory |

`guides/INSTALL.md` is the shared first step for every guide: the editable
install, the node URL and the `--network` rule. Start there if `sccp` is not on
your `PATH` yet.

## Guides

| Page | What it covers |
| --- | --- |
| [`INSTALL.md`](guides/INSTALL.md) | install the CLI, choose a node, keep the keyfile safe |
| [`CREATE_A_TESTNET_WALLET.md`](guides/CREATE_A_TESTNET_WALLET.md) | the short path: keyfile, faucet, balance |
| [`OPEN_A_WALLET.md`](guides/OPEN_A_WALLET.md) | the full keyfile reference: create, back up, restore, inspect |
| [`SEND_COINS.md`](guides/SEND_COINS.md) | fund, transfer, fees, nonces and settlement |
| [`PLAY_AND_EXPORT.md`](guides/PLAY_AND_EXPORT.md) | play on-chain, serve the chain, export the block's PGN |
| [`EXPLORE_THE_CHAIN.md`](guides/EXPLORE_THE_CHAIN.md) | the web app: build it, what the pages show, the playable table |
| [`TEST_DEVNET_BRIDGE_DEX.md`](guides/TEST_DEVNET_BRIDGE_DEX.md) | ETH↔SCCP, pools, swaps and Chess960 end to end, CLI and web |

## Setup

The pages that make something exist. Every one of them ends with something
running; none of them assumes it already is.

| Page | What it covers |
| --- | --- |
| [`RUN_A_DEVNET.md`](setup/RUN_A_DEVNET.md) | a complete devnet from source: install, chain, node, web, wallet, miner |
| [`RUN_A_TESTNET_MINER.md`](setup/RUN_A_TESTNET_MINER.md) | relay and engine miner: register, availability, proofs |
| [`TESTNET_DEPLOYMENT.md`](setup/TESTNET_DEPLOYMENT.md) | what the merge-to-deploy workflow does, and the monthly cost |
| [`GCP_RESOURCE_SETUP.md`](setup/GCP_RESOURCE_SETUP.md) | project, VM, firewall, deploy user and workflow wiring |
| [`EXPLORER_DEPLOYMENT.md`](setup/EXPLORER_DEPLOYMENT.md) | publish the app and landing page: proxies, TLS, rollback |
| [`DOMAIN_MIGRATION.md`](setup/DOMAIN_MIGRATION.md) | the cutover to `proofofchess.com`, in the order it has to run |


## Operations

The pages you open when something is already deployed and something is wrong.
[`README.md`](operations/README.md) is the index and the severity model;
[`RUNBOOK.md`](operations/RUNBOOK.md) is the day-2 reference for the stack itself.
Every scenario page has the same shape: how to identify it, which sub-case it is,
what to do now, what to do afterwards, and how to prove the fix.

| Page | Symptom |
| --- | --- |
| [`README.md`](operations/README.md) | the index: what is running, severity, the first five minutes |
| [`INCIDENT_RESPONSE.md`](operations/INCIDENT_RESPONSE.md) | any incident: roles, order of operations, comms, postmortem |
| [`RUNBOOK.md`](operations/RUNBOOK.md) | health checks, traffic, restarts, faucet, stuck chain, reorgs |
| [`PRIMARY_DOWN.md`](operations/PRIMARY_DOWN.md) | nothing answers |
| [`CHAIN_STALLED.md`](operations/CHAIN_STALLED.md) | the API answers and the height does not move |
| [`DATA_LOSS.md`](operations/DATA_LOSS.md) | a volume, a database, a key, or the disk |
| [`TLS_AND_DNS.md`](operations/TLS_AND_DNS.md) | certificates, ACME, records, port 80 |
| [`DEPLOY_FAILURES.md`](operations/DEPLOY_FAILURES.md) | a red deploy, or a green one that changed nothing |
| [`MIRROR_OUTAGE.md`](operations/MIRROR_OUTAGE.md) | the read-only mirror is stale or down |
| [`FAUCET_AND_KEYS.md`](operations/FAUCET_AND_KEYS.md) | the faucet is dry, a key is missing or leaked |
| [`SECURITY_INCIDENT.md`](operations/SECURITY_INCIDENT.md) | an exposed port, abuse, a compromised host or credential |


## Design

| Page | What it covers |
| --- | --- |
| [`PROTOCOL.md`](design/PROTOCOL.md) | normative: canonical encoding, transactions, ledger, consensus, API |
| [`WHITEPAPER.md`](design/WHITEPAPER.md) | the whole implementation in prose, from identity to operation |
| [`BIG_PICTURE.md`](design/BIG_PICTURE.md) | wallet, variant assets, DEX and the ETH bridge, and what remains |
| [`THREAT_MODEL.md`](design/THREAT_MODEL.md) | assets, adversaries, each mitigation and its residual risk |
| [`IMPLEMENTATION_PLAN.md`](design/IMPLEMENTATION_PLAN.md) | prioritized next steps, acceptance criteria, working agreements |

## Writing a page

- Say what the page needs at the top and where to go next at the bottom; do not
  repeat the install block — link [`guides/INSTALL.md`](guides/INSTALL.md).
- A rule that consensus enforces belongs in [`design/PROTOCOL.md`](design/PROTOCOL.md);
  link it instead of restating it.
- Links are relative (`../design/PROTOCOL.md` from a guide) so they work on
  GitHub, in a checkout and in the wheel's metadata alike.
- The folder is the tense. A page that says "create", "install" or "wire the
  workflow" is `setup/`; a page that starts from a symptom and a broken system is
  `operations/`. When a topic needs both, the build goes in `setup/` and the repair
  goes in `operations/`, with a link across.
- Every page here is checked by `tests/unit/test_docs_addresses.py` (the sample
  addresses must parse); keep pasted output and addresses from real runs.

