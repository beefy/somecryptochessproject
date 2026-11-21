# The testnet stack

`docker-compose.yml` runs the testnet deployment on one machine: a full node, the
matchmaking relay, one deterministic bootstrap job, a demo seeder, a traffic drip,
four Stockfish miners -- two standard-chess seats and two Chess960 seats -- plus
three clock witnesses and two extra role miners (a deliberately slow seat and a
GNU Chess seat). It starts from the pinned `testnet` genesis, funds and registers
all four fixed miners for their variants, registers the witnesses, commits/reveals
availability, opens a matchable epoch, plays one game per variant per round and
settles the winning proofs on chain. `sccp devnet up` remains the fast in-process
demo; this stack exercises the real multi-process, HTTP and SQLite path. The
merge-to-deploy workflow and its monthly cost are in
[`docs/setup/TESTNET_DEPLOYMENT.md`](../docs/setup/TESTNET_DEPLOYMENT.md).

A second, `e2-micro` VM can host a public read-only mirror of the chain; that
deployment lives in [`edge/`](edge/README.md) and is a separate Compose project on
purpose. `.github/workflows/deploy-edge.yml` redeploys it after the primary's
deploy, from the same image tag, when `EDGE_DEPLOY_HOST` is configured; without
that variable there is no mirror and nothing waits for one.

This page is about building the stack. When it is already running and something is
wrong with it, [`docs/operations/`](../docs/operations/README.md) is the triage
index: severity, the first five minutes, and a runbook per failure.


The current hosted deployment is at
`http://app.proofofchess.com:8645`; the commands below use
`127.0.0.1` because they are for a local Compose stack. The web application is a
separate Compose deploy, in `explorer/`, published at
`https://app.proofofchess.com`, while the project overview from `src/landing` is
at `https://proofofchess.com/`; see
[`docs/setup/EXPLORER_DEPLOYMENT.md`](../docs/setup/EXPLORER_DEPLOYMENT.md).

```bash
docker compose up --build
curl http://127.0.0.1:8645/v1/status    # node: height, mempool, peers
curl http://127.0.0.1:8646/v1/status     # relay: beacon, matches, reputation
curl http://127.0.0.1:8646/v1/matches    # what the miners are playing
docker compose logs -f bootstrap seed drip miner-1
docker compose down -v                   # resets the chain and relay state
```

## The ETH bridge environment

The SCCP node never holds an Ethereum key. It only publishes which Ethereum
environment belongs to its network, and the browser wallet signs the ETH leg
through an EIP-1193 provider. The mapping is fixed in `sccp.bridge.config`:

| SCCP network | Ethereum | Chain id | Default RPC |
| --- | --- | --- | --- |
| `devnet` | Anvil | `31337` | `http://127.0.0.1:8545` |
| `testnet` | Sepolia | `11155111` | set `SCCP_ETH_RPC_URL` |
| `mainnet` | Ethereum | `1` | set `SCCP_ETH_RPC_URL` |

Deploy the contract once for the environment, then give the node the address:

```console
# contracts/ has the Foundry project and its tests
$ export SCCP_ETH_RPC_URL=https://sepolia.example/rpc
$ cd contracts && forge install foundry-rs/forge-std
$ SCCP_NETWORK=testnet forge script script/Deploy.s.sol:Deploy \
    --rpc-url "$SCCP_ETH_RPC_URL" --private-key "$DEPLOYER_PRIVATE_KEY" --broadcast

$ export SCCP_ETH_HTLC_CONTRACT=0x...
$ docker compose up -d node
$ curl http://127.0.0.1:8645/v1/bridge
```

The deploy script rejects a chain-id mismatch before broadcasting, so a stale
RPC URL cannot put a testnet contract on mainnet. `SCCP_ETH_CONFIRMATIONS` and
`SCCP_ETH_SCCP_MARGIN` are optional overrides of the per-network defaults; setting
either one to zero is an error, and a margin at or below the SCCP `reorg_depth` is
refused because the maker needs the finality window plus time to claim ETH.
Leaving `SCCP_ETH_HTLC_CONTRACT` unset is supported: the bridge page still shows
the ZQWX locks and clearly disables every ETH action.

For the hosted testnet workflow, set repository variables
`SCCP_ETH_RPC_URL` (Sepolia) and `SCCP_ETH_HTLC_CONTRACT`; the deploy job passes
them into the node container. No private key is stored in GitHub.


The first run takes a minute or two: the bootstrap writes 19 blocks, then the
relay opens round 0 and the miners play. When a game is decisive, a settled
proof appears as `mined height 20: 1 proof(s) ...` in the node log; a draw has no
proof to settle.

## What each container does

| Service | Command | Purpose |
| --- | --- | --- |
| `init` | `python deploy/init_testnet_stack.py --out /stack` | writes the four miner keyfiles, the three witness keyfiles (and their public-key files), the availability secrets, the passphrase file and `stack.json` |
| `node` | `sccp --data-dir /data node run --network testnet --mine --miner-address ... --mine-interval-seconds 30 --no-mine-bootstrap` | node API plus the proof settler, paced at the target block spacing; chain in SQLite under `node-data` |
| `bootstrap` | `python deploy/bootstrap_testnet_stack.py --stack /stack --node http://node:8645` | funds/registers all four miners for their variants, funds and registers the three clock witnesses, commits and reveals availability, writes `relay.json` from epoch 3 (and keeps it on a re-run) |
| `seed` | `python -m deploy.seed_testnet_demo --stack /stack --node http://node:8645` | verifies the genesis-seeded ZQWX/C960 pool and completed SCCP HTLC; falls back to creating them on a custom chain whose genesis does not |
| `drip` | `python deploy/drip_testnet_traffic.py --node http://node:8645` | keeps one ordinary transfer in the pool so every epoch has the ledger work its blocks need |
| `traffic` | `python deploy/synthetic_testnet_traffic.py --node http://node:8645 --relay http://relay:8646 --extra-miners 2 --no-extra-miner-churn` | creates ten reproducible wallets on startup, funds them in the first cycle, then per cycle tops up and moves coins between 2-10 of the wallets, exercises bridge HTLCs and DEX pools/swaps when funded, waits for both phases to be mined, and idles 1-5 minutes; every twelve cycles it also fires an 80-transfer burst over five waves, and it keeps the two extra role miners seated (`synthetic-miner-1` slow, `synthetic-miner-2` GNU Chess) |
| `relay` | `sccp matchmaker run --config /stack/relay.json --store /stack/relay-state.json --node http://node:8645 --witness-key /stack/keys/witness-1.key` | opens round 0, pairs within each variant, runs the 10+0 clock, opens a new round every 60 s and follows the chain's epoch |
| `miner-1`, `miner-2` | `sccp miner run --variant standard --engine-profile stockfish-19 --nodes-per-move 5000 --nonce-secret-file /stack/keys/miner-N.nonce --submit-timeout-claims` | standard-chess pair: join, play, co-sign, announce win proofs, submit bonded timeout claims and their finalising proofs, and commit/reveal their own availability |
| `miner-3`, `miner-4` | `sccp miner run --variant chess960 --engine-profile stockfish-19 --nodes-per-move 5000 --nonce-secret-file /stack/keys/miner-N.nonce --submit-timeout-claims` | Chess960 pair: replay the beacon-selected start position, play with `UCI_Chess960`, settle C960 proofs, and submit timeout claims on a flag |
| `witness-2`, `witness-3` | `sccp witness run --relay http://relay:8646 --key /stack/keys/witness-N.key` | independent clock witnesses: poll the relay, verify its receipts, co-sign each receipt and the flag checkpoint, and serve their own bundle at `/v1/matches/{id}/receipts` |

The bootstrap is deliberately separate from mining. It builds each block with
the same `BlockBuilder` a miner uses and submits it through the node's public
`POST /v1/blocks`; the node re-validates everything. The only privileged input is
the published testnet faucet key, which pays each stack miner 100 test coins.

`drip` and `traffic` are the two generators, and they answer different questions.
`drip` closes the epoch's ledger-work gap; `traffic` is ordinary usage -- wallets
funded from the faucet, transfers between them, then 1-5 minutes of quiet -- so a
testnet nobody is watching still exercises the paths a client takes. Neither holds
a roster slot or builds a block. `traffic` keeps its coins inside its own wallet
pool, so it circulates them instead of draining the faucet; the
[runbook](../docs/operations/RUNBOOK.md#synthetic-traffic) has the arithmetic.

Every twelfth cycle `traffic` also runs a burst: it tops the pool up, queues 80
transfers in five waves of about one second and only then waits for them to be
mined, so several blocks see a deep mempool instead of the usual trickle. The
burst uses the published operator key rather than the faucet endpoint, because
those addresses already took their one public grant; ordinary cycles still spend
the faucet first. `--burst-every 0` (or `--no-burst`) disables it;
`--burst-transfers`, `--burst-rounds`, `--burst-interval-seconds` and the two
balance flags size it. The stats notebook marks each burst and compares its
per-minute volume with the ordinary cycles.

`seed` is now an idempotent check rather than the source of the demo state.
Testnet genesis already creates the ZQWX/C960 pool (5 coins per side) and a
completed 1-ZQWX HTLC, so `GET /v1/pools`, `GET /v1/htlcs` and the Bridge
**Activity** tab are populated at height 0. The service detects those records and
reports them; it can still create the bridge leg and pool for a custom network
whose genesis does not seed them. It signs with the published stack keys and two
deterministic demo wallets, and every transaction it does send goes through the
node's normal admission path. The Ethereum leg remains an operator deployment:
the node holds no Ethereum key by design, so deploying `EthHtlc` for Sepolia
still needs a funded deployer key and `SCCP_ETH_HTLC_CONTRACT`.

The `traffic` container also seats the two extra role miners
(`deploy/synthetic_miner_creation.py`), and that is not optional in the Compose
deployment: the fast four all resign on polite drawn games, so nothing else
produces a *timeout*. `synthetic-miner-1` is deliberately slow (36 s a move, no
resignation), so a 10+0 game really runs out of clock and the signed flag becomes
a bonded `timeout_claim`; `synthetic-miner-2` plays GNU Chess (`--depth 5`, its
own fingerprint), so the roster is not six copies of one binary. Both are fixed
roles rather than a churn (`--no-extra-miner-churn`): their identities are
derived from published seeds, and a seat that exits is repaired on the next
cycle.

The seats are children of the `traffic` process, not Compose services, so
`docker compose ps` cannot show them. The fleet writes what it holds to
`/stack/synthetic-fleet.json` at the start of every cycle, and the deploy
workflow gates on it with `--fleet-only --expect-roles slow,gnuchess` (run inside
the `traffic` container, which has both `/app` and `/stack`). A host that cannot
fit them says so in that file, with the measured reason.

## The keys and engine are reproducible on purpose

`deploy/init_testnet_stack.py` derives all four miners from fixed seeds and the
stack passphrase is published (`sccp-testnet-stack`). The image installs Debian's
Stockfish as `/usr/games/stockfish`; the bootstrap registers that binary's
fingerprint and every miner runs it, with `UCI_Chess960` enabled for the Chess960
pair (python-chess turns it on from each board, so a miner must never configure
the option itself). Testnet coins have no value, and a reproducible stack is what
makes `docker compose up` work without a coordination step. **Never reuse these
seeds, that passphrase or that bond on mainnet.**

`/stack/stack.json` lists each miner's address, keyfile and availability nonce
secret, and `/stack/keys/miner-N.nonce` holds that secret for
`sccp miner run --nonce-secret-file`. The bootstrap rewrites `/stack/relay.json`
from the chain's epoch 3 beacon and roster every time it runs.

## Keeping the chain moving

The bootstrap commits availability for the epochs it can reach, but it is a
one-shot job: without help the roster and the chain both stop once those reveals
are used up. Two things keep the stack alive past that point:

- Each miner runs with `--nonce-secret-file`, so it commits availability two
  epochs ahead and reveals it every epoch. The roster therefore keeps growing
  instead of emptying.
- The relay runs with `--node http://node:8645`, so it moves onto each epoch the
  chain opens instead of matching forever for epoch 3. `docker compose logs -f
  relay` shows `relay moved to epoch N with 2 matchable miner(s)`.

Between decisive games the chain still has to advance, and a block needs either a
proof or at least one transaction: a draw produces no proof, so the availability
and reveal transactions double as the heartbeat that carries the chain into the
next epoch. The builder puts exactly one transaction in a proofless block, so
each pending transaction buys one block and an epoch of five blocks needs five
pieces of ledger work. The four-miner roster supplies eight per epoch
(availability plus reveal each), so the heartbeat is self-sustaining even when
both variants draw. A game that ends on the clock also files a bonded timeout
claim, and that claim is an ordinary transaction, so it moves the ledger even
before its proof settles.

Two further settings make that arithmetic hold even when the games do not:

- `drip` submits one ordinary faucet transfer whenever the node reports an empty
  transaction pool, so the fifth piece of work is there when no game produced a
  proof. It signs what any wallet signs and its transfer goes through
  `POST /v1/tx` like any other, so the node admits or refuses it on the same
  terms. `docker compose logs -f drip` shows `dripped 1 units to sccpt1...`.
- The node's settler runs with `--mine-interval-seconds 30`, the network's
  `target_block_seconds`. A block's timestamp is the wall clock and the
  difficulty retarget reads those timestamps, so a settler that built a block on
  every one-second poll made the chain look thirty times too fast; the schedule
  answered by raising `wins_required` to `2`, which asks one block to carry two
  wins by the same miner -- more than the roster supplies in an epoch -- and the
  chain stopped at the first block that demanded it. At one block per target
  interval the retarget is neutral and `wins_required` stays `1`.

Two equal Stockfish builds mostly draw, and a draw is not a proof, so all four
stack miners run with `--resign-after-plies 60`: on its own turn a miner resigns once
the game reaches 60 plies and both seats co-sign a `resignation` result, which
the winner proves like any other win. Without that policy `GET /v1/matches`
shows `"termination": "draw"` on every session and the tip can sit mid-epoch.

The fixed limit also explains why a fresh deploy's games are all `0-1`. White
moves on even ply counts, so 60 plies is reached on white's turn: white resigns
first and black is the winner every time. The result is a consequence of the
resignation policy, not of scoring or of the relay. An odd limit flips the
winner to white; only a limit that varies per match produces a mix of results.

## Testing the image itself

`make container-e2e` builds the current tree and runs
`tests/e2e/test_container_smoke.py`. The test creates a private Docker network,
seeds twelve deterministic traffic wallets, starts the node, the drip and a
traffic container, and asserts through the node API that the mined chain
contains closed HTLCs: one claimed before its timelock and one refunded after
it. A transfer case sends an ordinary ZQWX payment and checks both balances and
the mined transaction record, and a DEX case drives a C960 pool through create,
swap, add-liquidity and remove-liquidity. A miner-payout case then mines one
maturity window of fee-paying transfers and checks that the miner account gains
the bootstrap subsidy and the transaction fees, releases them only after
`coinbase_maturity` blocks, and grows the node's `minted` total by exactly the
new coinbase value. The containers only talk to the node;
the assertions run in the host process, so the test covers the artefact the
deploy workflow ships rather than the Python packages in place.

C960 is minted when a Chess960 proof settles, plus the fixed genesis allocation
on testnet and mainnet. The devnet genesis used by this smoke test has no C960
allocation, so the DEX case uses a dedicated devnet node whose fixture credits
the published traffic wallets with C960 before serving; every transaction after
that seed goes through normal admission, block building and validation. This
smoke test does not play a Chess960 match, so C960 issuance itself remains
covered by `test_chess960.py`, and the DEX transitions by `test_dex.py`.

It is opt-in and deselects itself otherwise:

```bash
make container-e2e
# or
SCCP_RUN_CONTAINER_E2E=1 pytest -m container tests/e2e/test_container_smoke.py -s
```

Set `SCCP_CONTAINER_E2E_IMAGE` to an existing tag to skip the build while
iterating on the test.

## Reset behaviour and limits

- The deploy workflow runs `docker compose down -v` before every `up`, so each
  merge starts a fresh deterministic chain. Do the same locally when testing a
  new image.
- The bootstrap registers three games per miner (the testnet epoch quota) and
  the relay opens one round every minute, so a miner's quota can run out for the
  epoch; the next epoch's roster restores it.
- The faucet's limits are in memory and reset with the node. See
  [`docs/operations/RUNBOOK.md`](../docs/operations/RUNBOOK.md) for the exact policy.

## Clocks and witnesses

The relay runs timed sessions whether or not the stack asks for them: both
clocks start on the second join, moves are charged against the mover and every
accepted move is answered with a receipt. Without `--witness-key` those receipts
carry no signatures, so a flagged game cannot become a timeout claim. The stack's
relay signs with witness-1, and every miner is given all three witnesses' public
keys and the two independent witness URLs. A flag a miner wins is signed as a
`timeout_claim`, submitted to the node by default, and followed by the matching
`time_forfeit` proof once the claim is in a block. The deployments spell
`--submit-timeout-claims` explicitly; `--no-submit-timeout-claims` is the
observation-only alternative.

The relay also counts them: started with `--node`, it reads `witnesses` from the
node's `/v1/status` on every poll and refuses to open a round while the epoch's
active witness count is below the network's `clock_witness_quorum` (3 on
testnet). A flag that fewer witnesses signed can never settle a `timeout_claim`,
so the round would only produce games whose losses cannot reach the chain. This
stack registers all three witnesses in the bootstrap, so the gate is satisfied;
the devnet stack writes `require_witnesses: false` instead, because it runs no
witness service.

## The devnet stack, without Compose

`setup_devnet_stack.py` is the devnet counterpart of the two testnet scripts
above. It needs no running node and no container: one command writes the keys,
builds `chain.sqlite` itself, then writes the relay config derived from the epoch
the chain opened.

```bash
python deploy/setup_devnet_stack.py --out .devnet-stack
# wrote 2 devnet keyfiles and .devnet-stack/stack.json
# built 3 bootstrap blocks: height 3, epoch 3 matchable
# relay config for epoch 3 written to .devnet-stack/relay.json
```

The command prints the node, relay and miner commands for those files.
`init_devnet_stack.py` and `bootstrap_devnet_stack.py` still hold the two halves
for tests and scripts that need only one stage. Both stacks publish their seeds
and passphrase on purpose; devnet has no value to protect and a reproducible
stack is what makes the files work without a coordination step. `--fresh` on
`setup_devnet_stack.py` rebuilds the devnet chain and never touches the keys.
[`docs/setup/RUN_A_DEVNET.md`](../docs/setup/RUN_A_DEVNET.md) section 9.3 walks
through it.
