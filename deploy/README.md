# The local testnet stack

`docker-compose.yml` runs the M4 network on one machine: a full node, the
matchmaking relay and three miners, all on the `testnet` parameters. It exists
to exercise the multi-process wiring (startup order, shared state, real ports,
graceful shutdown) that the in-process devnet cannot: `sccp devnet up` remains
the fast, deterministic, one-command demo.

```bash
docker compose up --build
curl http://127.0.0.1:8645/v1/status    # node
curl http://127.0.0.1:8646/v1/status    # relay: beacon, matches, reputation
curl http://127.0.0.1:8646/v1/matches   # what the miners are playing
docker compose logs -f miner-1
docker compose down -v                  # also drops the chain and relay state
```

## What each container does

| Service | Command | Purpose |
| --- | --- | --- |
| `init` | `python deploy/init_testnet_stack.py --out /stack` | writes the three keyfiles, the passphrase file and `relay.json` |
| `node` | `sccp --data-dir /data node run --network testnet` | the section 5.12 API, with the chain in SQLite under the `node-data` volume |
| `relay` | `sccp matchmaker run --config /stack/relay.json --store /stack/relay-state.json` | opens round 0 at startup and serves the match protocol |
| `miner-1..3` | `sccp miner run --engine-profile mock` | joins its assigned matches, plays them and co-signs the results |

## The keys are public on purpose

`deploy/init_testnet_stack.py` derives the three miners from fixed seeds and the
stack passphrase is published (`sccp-testnet-stack`). Testnet coins have no
value, and a reproducible stack is what makes `docker compose up` work without a
coordination step. **Never reuse these seeds or that passphrase on mainnet.**

`/stack/stack.json` lists each miner's address, keyfile and availability nonce
secret for the epoch the relay was configured with.

## What this stack does not do yet

- **No on-chain settlement.** Testnet genesis funds the public faucet, not these
  keys, so the stack's miners are not registered and have no availability on
  chain; the node stays at the genesis block. Funding, registration and proof
  inclusion are M6 work (`docs/RUN_A_TESTNET_MINER.md`), and the full loop is
  demonstrated today by `sccp devnet up`. For the same reason the miners are
  started without `--node`: a proof announcement the chain cannot accept would
  only produce refusals in the log.
- **Mock engines.** The image ships no UCI binary, so the miners use the seeded
  mock engine. Two random movers often shuffle: a game ends by checkmate,
  stalemate or resignation, and mock engines may reach the ply ceiling first. To
  play with a real engine, mount a binary and pass
  `--engine-path /path/to/stockfish`.
- **One round.** The relay opens the round named in `relay.json`
  (`open_round: 0`). Rounds do not advance by themselves; `POST /v1/rounds`
  with `{"round": 1}` opens the next one. Round scheduling belongs to the
  testnet launch (M6).
