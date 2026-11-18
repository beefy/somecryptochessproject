# Documentation

The pages are grouped by what you are trying to do. Pick a row, or read the
folder table below it.

| I want to... | Read |
| --- | --- |
| get coins into a testnet wallet | [`guides/CREATE_A_TESTNET_WALLET.md`](guides/CREATE_A_TESTNET_WALLET.md) |
| know the exact wire format or consensus rule | [`design/PROTOCOL.md`](design/PROTOCOL.md) |
| watch a game become a block, then export it | [`guides/PLAY_AND_EXPORT.md`](guides/PLAY_AND_EXPORT.md) |
| run a whole network on this machine | [`operations/RUN_A_DEVNET.md`](operations/RUN_A_DEVNET.md) |
| keep the public testnet healthy | [`operations/RUNBOOK.md`](operations/RUNBOOK.md) |
| deploy to a cloud VM | [`operations/GCP_RESOURCE_SETUP.md`](operations/GCP_RESOURCE_SETUP.md) |
| exercise the bridge, the DEX and Chess960 | [`guides/TEST_DEVNET_BRIDGE_DEX.md`](guides/TEST_DEVNET_BRIDGE_DEX.md) |
| see what is still unfinished | [`design/IMPLEMENTATION_PLAN.md`](design/IMPLEMENTATION_PLAN.md) |

## The three folders

| Folder | For | Authority |
| --- | --- | --- |
| [`guides/`](guides) | using the CLI, the wallet and the web app: a task with a beginning and an end | describes the shipped commands |
| [`operations/`](operations) | running, deploying and repairing nodes, miners and the web origins | describes this repository's deployment |
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

## Operations

| Page | What it covers |
| --- | --- |
| [`RUN_A_DEVNET.md`](operations/RUN_A_DEVNET.md) | a complete devnet from source: install, chain, node, web, wallet, miner |
| [`RUN_A_TESTNET_MINER.md`](operations/RUN_A_TESTNET_MINER.md) | relay and engine miner: register, availability, proofs |
| [`RUNBOOK.md`](operations/RUNBOOK.md) | health checks, traffic, restarts, faucet, stuck chain, reorgs |
| [`TESTNET_DEPLOYMENT.md`](operations/TESTNET_DEPLOYMENT.md) | what the merge-to-deploy workflow does, and the monthly cost |
| [`GCP_RESOURCE_SETUP.md`](operations/GCP_RESOURCE_SETUP.md) | project, VM, firewall, deploy user and workflow wiring |
| [`EXPLORER_DEPLOYMENT.md`](operations/EXPLORER_DEPLOYMENT.md) | publish the app and landing page: proxies, TLS, rollback |
| [`DOMAIN_MIGRATION.md`](operations/DOMAIN_MIGRATION.md) | the cutover to `proofofchess.com`, in the order it has to run |

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
- Every page here is checked by `tests/unit/test_docs_addresses.py` (the sample
  addresses must parse); keep pasted output and addresses from real runs.
