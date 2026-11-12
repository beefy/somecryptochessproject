# The two-miner testnet stack

`docker-compose.yml` runs the testnet deployment on one machine: a full node, the
matchmaking relay, one deterministic bootstrap job, a traffic drip and two
Stockfish miners. It starts from the pinned `testnet` genesis, funds and
registers both miners, commits/reveals availability, opens a matchable epoch,
plays games and settles the winning proofs on chain. `sccp devnet up` remains the
fast in-process demo; this stack exercises the real multi-process, HTTP and
SQLite path. The merge-to-deploy workflow and its monthly cost are in
[`docs/TESTNET_DEPLOYMENT.md`](../docs/TESTNET_DEPLOYMENT.md).

The current hosted deployment is at
`http://testnet.somecryptochessproject.com:8645`; the commands below use
`127.0.0.1` because they are for a local Compose stack.

```bash
docker compose up --build
curl http://127.0.0.1:8645/v1/status    # node: height, mempool, peers
curl http://127.0.0.1:8646/v1/status     # relay: beacon, matches, reputation
curl http://127.0.0.1:8646/v1/matches    # what the miners are playing
docker compose logs -f bootstrap drip miner-1
docker compose down -v                   # resets the chain and relay state
```

The first run takes a minute or two: the bootstrap writes 19 blocks, then the
relay opens round 0 and the miners play. When a game is decisive, a settled
proof appears as `mined height 20: 1 proof(s) ...` in the node log; a draw has no
proof to settle.

## What each container does

| Service | Command | Purpose |
| --- | --- | --- |
| `init` | `python deploy/init_testnet_stack.py --out /stack` | writes the two keyfiles, their availability secrets, the passphrase file and `stack.json` |
| `node` | `sccp --data-dir /data node run --network testnet --mine --miner-address ... --mine-interval-seconds 60 --no-mine-bootstrap` | node API plus the proof settler, paced at the target block spacing; chain in SQLite under `node-data` |
| `bootstrap` | `python deploy/bootstrap_testnet_stack.py --stack /stack --node http://node:8645` | funds/registers both miners, commits and reveals availability, writes `relay.json` from epoch 3 |
| `drip` | `python deploy/drip_testnet_traffic.py --node http://node:8645` | keeps one ordinary transfer in the pool so every epoch has the ledger work its blocks need |
| `relay` | `sccp matchmaker run --config /stack/relay.json --store /stack/relay-state.json --node http://node:8645` | opens round 0, runs the 30+0 clock, opens a new round every 60 s and follows the chain's epoch |
| `miner-1`, `miner-2` | `sccp miner run --engine-profile stockfish-19 --nodes-per-move 5000 --nonce-secret-file /stack/keys/miner-1.nonce` | join, play, co-sign, announce win proofs and commit/reveal their own availability |

The bootstrap is deliberately separate from mining. It builds each block with
the same `BlockBuilder` a miner uses and submits it through the node's public
`POST /v1/blocks`; the node re-validates everything. The only privileged input is
the published testnet faucet key, which pays each stack miner 100 test coins.

## The keys and engine are reproducible on purpose

`deploy/init_testnet_stack.py` derives both miners from fixed seeds and the stack
passphrase is published (`sccp-testnet-stack`). The image installs Debian's
Stockfish as `/usr/games/stockfish`; the bootstrap registers that binary's
fingerprint and both miners run it. Testnet coins have no value, and a
reproducible stack is what makes `docker compose up` work without a coordination
step. **Never reuse these seeds, that passphrase or that bond on mainnet.**

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
pieces of ledger work. A two-miner roster supplies four per epoch (availability
plus reveal each), so one decisive game per epoch is what carries the stack; a
roster of three or more is self-sustaining. A game that ends on the clock also
files a bonded timeout claim, and that claim is an ordinary transaction, so it
moves the ledger even before its proof settles.

Two further settings make that arithmetic hold even when the games do not:

- `drip` submits one ordinary faucet transfer whenever the node reports an empty
  transaction pool, so the fifth piece of work is there when no game produced a
  proof. It signs what any wallet signs and its transfer goes through
  `POST /v1/tx` like any other, so the node admits or refuses it on the same
  terms. `docker compose logs -f drip` shows `dripped 1 units to sccpt1...`.
- The node's settler runs with `--mine-interval-seconds 60`, the network's
  `target_block_seconds`. A block's timestamp is the wall clock and the
  difficulty retarget reads those timestamps, so a settler that built a block on
  every two-second poll made the chain look thirty times too fast; the schedule
  answered by raising `wins_required` to `2`, which asks one block to carry two
  wins by the same miner -- more than a two-miner roster wins in an epoch -- and
  the chain stopped at the first block that demanded it. At one block per target
  interval the retarget is neutral and `wins_required` stays `1`.

Two equal Stockfish builds mostly draw, and a draw is not a proof, so both stack
miners run with `--resign-after-plies 60`: on its own turn a miner resigns once
the game reaches 60 plies and both seats co-sign a `resignation` result, which
the winner proves like any other win. Without that policy `GET /v1/matches`
shows `"termination": "draw"` on every session and the tip can sit mid-epoch.

## Reset behaviour and limits

- The deploy workflow runs `docker compose down -v` before every `up`, so each
  merge starts a fresh deterministic chain. Do the same locally when testing a
  new image.
- The bootstrap registers three games per miner (the testnet epoch quota) and
  the relay opens one round every minute, so a miner's quota can run out for the
  epoch; the next epoch's roster restores it.
- The faucet's limits are in memory and reset with the node. See
  [`docs/RUNBOOK.md`](../docs/RUNBOOK.md) for the exact policy.

## Clocks and witnesses

The relay runs timed sessions whether or not the stack asks for them: both
clocks start on the second join, moves are charged against the mover and every
accepted move is answered with a receipt. Without `--witness-key` those receipts
carry no signatures, so a flagged game cannot become a timeout claim. Add a
keyfile to the relay (`--witness-key /stack/relay.key`) and pass the matching
public key hex to the miners (`sccp miner run --relay-witness-key <hex>`) to run
the full signed path. Both are one flag each because a witness identity is an
operator decision, not a network parameter.
