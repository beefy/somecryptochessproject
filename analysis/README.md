# Testnet analysis

`testnet_stats.ipynb` charts the live testnet from its public JSON API. It is read-only:
every cell is a `GET`, nothing is signed, no key is involved, and no node state changes.

## Setup

```bash
python -m venv .venv-analysis
pip install -e .                    # sccp + the bot's wallets
pip install -r analysis/requirements.txt
jupyter lab analysis/testnet_stats.ipynb
```

Run the notebook from this directory (`cd analysis`) so `cache/` lands here and the
repository root is found for the synthetic-wallet import.

```
Jupyter command `jupyter-lab` not found.
```

Make sure you are using python3 with your venv.

## What it shows

| Cell | Chart |
| --- | --- |
| Chain overview | height, tip, peers, pool sizes, sample window, throughput |
| Throughput | cumulative transactions, transactions per block, arrival rate, block interval |
| Confirmation speed | interval distribution, blocks-settled-within-x, retargets |
| Transactions | the type mix and the busiest senders |
| Transfer graph | directed wallet-to-wallet graph, synthetic wallets highlighted |
| Token distribution | tokens per wallet, largest wallets |
| Synthetic balances | stacked area of the ten bot wallets over time |
| Miner graph | directed win graph plus a win matrix |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCCP_NODE_URL` | `http://testnet.somecryptochessproject.com:8645` | node to read |
| `SCCP_ANALYSIS_REFRESH` | unset | ignore `cache/` and refetch |
| `SCCP_ANALYSIS_MAX_BLOCKS` | `400` | blocks in the sample |
| `SCCP_ANALYSIS_MAX_BODIES` | `80` | blocks fetched in full for their transactions |
| `SCCP_ANALYSIS_MAX_ACCOUNTS` | `80` | `GET /v1/accounts` calls |
| `SCCP_ANALYSIS_MAX_GAMES` | `400` | games in the sample |

Full block bodies embed proof PGNs, so `MAX_BODIES` is the expensive knob: 80 blocks is a
few megabytes on a busy chain.

## Limits worth knowing

* **No address enumeration.** "Wallets seen" is every address in the sampled blocks plus
  every rated miner. A wallet that never transacted and never mined is invisible.
* **No per-transaction timestamps.** Submit-to-mined latency is not recoverable from the
  chain; the notebook measures the block interval, which bounds it.
* **The bottom of the chain is dated by the genesis document, not by the deploy.**
  `genesis/<network>.json` fixes the premine's timestamp (`1700000000`, 2023-11-14, on
  testnet) and that block is part of the pinned genesis hash, so it cannot be re-dated
  without a new network. The bootstrap blocks above it (heights 1 to
  `bootstrap_end_height`, 19 on testnet) were mined one target interval apart from that
  same stamp, so a chain deployed today carries about twenty blocks dated 2023 and then
  jumps to the deploy date. The notebook prints that window in the overview and keeps it
  out of the time axes and interval statistics; the explorer's "1095d ago" on those rows
  is the same fact. `deploy/bootstrap_*_stack.py` now stamps new bootstraps with the wall
  clock, so this only describes chains bootstrapped before that fix.
* **Balances are reconstructed, then anchored.** The synthetic-wallet history is rebuilt
  from their transfers and shifted so its last point equals `GET /v1/accounts`' `total`.
  The cell prints the drift, which is zero when every movement was captured.
* **Sampled, not exhaustive.** Charts cover the newest `MAX_BLOCKS` blocks; a chain longer
  than that shows its recent history, not its whole life.

## Cache

Every fetch is cached under `cache/` (git-ignored) so a re-run costs nothing. Delete the
directory — or set `SCCP_ANALYSIS_REFRESH=1` — after a deploy that adds blocks.

The notebook is not executed by CI; it is a manual tool.
