# The two-miner testnet stack

`docker-compose.yml` runs the testnet deployment on one machine: a full node, the
matchmaking relay, one deterministic bootstrap job and two Stockfish miners. It
starts from the pinned `testnet` genesis, funds and registers both miners,
commits/reveals availability, opens a matchable epoch, plays games and settles
the winning proofs on chain. `sccp devnet up` remains the fast in-process demo;
this stack exercises the real multi-process, HTTP and SQLite path. The merge-to-
deploy workflow and its monthly cost are in
[`docs/TESTNET_DEPLOYMENT.md`](../docs/TESTNET_DEPLOYMENT.md).

The current hosted deployment is at
`http://testnet.somecryptochessproject.com:8645`; the commands below use
`127.0.0.1` because they are for a local Compose stack.

```bash
docker compose up --build
curl http://127.0.0.1:8645/v1/status    # node: height, mempool, peers
curl http://127.0.0.1:8646/v1/status     # relay: beacon, matches, reputation
curl http://127.0.0.1:8646/v1/matches    # what the miners are playing
docker compose logs -f bootstrap miner-1
docker compose down -v                   # resets the chain and relay state
```

The first run takes a minute or two: the bootstrap writes 19 blocks, then the
relay opens round 0 and the miners play. When a game is decisive, a settled
proof appears as `mined height 20: 1 proof(s) ...` in the node log; a draw has no
proof to settle.

## What each container does

| Service | Command | Purpose |
| --- | --- | --- |
| `init` | `python deploy/init_testnet_stack.py --out /stack` | writes the two keyfiles, the passphrase file and `stack.json` |
| `node` | `sccp --data-dir /data node run --network testnet --mine --miner-address ... --no-mine-bootstrap` | node API plus the proof settler; chain in SQLite under `node-data` |
| `bootstrap` | `python deploy/bootstrap_testnet_stack.py --stack /stack --node http://node:8645` | funds/registers both miners, commits and reveals availability, writes `relay.json` from epoch 3 |
| `relay` | `sccp matchmaker run --config /stack/relay.json --store /stack/relay-state.json` | opens round 0, runs the 30+0 clock, opens a new round every 60 s |
| `miner-1`, `miner-2` | `sccp miner run --engine-profile stockfish-19 --nodes-per-move 5000` | join, play, co-sign and announce win proofs to the node |

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
secret. The bootstrap rewrites `/stack/relay.json` from the chain's epoch 3
beacon and roster every time it runs.

## Reset behaviour and limits

- The deploy workflow runs `docker compose down -v` before every `up`, so each
  merge starts a fresh deterministic chain. Do the same locally when testing a
  new image.
- The bootstrap registers three games per miner (the testnet epoch quota) and
  the relay opens one round every minute. Once those games are played the chain
  stops advancing until another deployment/reset: this is a demonstration
  network, not a long-lived public testnet yet.
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
