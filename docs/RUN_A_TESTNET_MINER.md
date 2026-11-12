# Run a testnet miner

A miner is a wallet plus a UCI chess engine plus a process that polls the
matchmaking relay, plays the games the relay assigns, co-signs the result and
announces the win proof to a node. The engine is the work: a miner that cannot
win games cannot produce blocks.

There are two ways to see a miner run:

| Path | Command | What it proves |
| --- | --- | --- |
| In-process demo | `sccp devnet up --miners 4 --blocks 10 --engines mock` | The whole loop (pairing, play, proofs, blocks) in one process. |
| Relay + miner processes | `sccp matchmaker run` and `sccp miner run` | The real multi-process wiring, over HTTP, with real clocks. |

> **Status.** Relay play, engine mining, win-proof construction and the wallet
> and miner commands work today. On-chain settlement on a live testnet
> (funding, registration inclusion, proof inclusion) is still milestone M6 work;
> [`deploy/README.md`](../deploy/README.md) is explicit about the boundary. Use
> `sccp devnet up` for a settled chain, and the relay flow below for the miner
> itself.

---

## 1. What the miner does

1. **Registers** on chain: locks a bond and publishes an endpoint and an engine
   label (`miner register`).
2. **Commits availability** for a future epoch with a secret nonce
   (`miner availability`), then **reveals** the nonce one epoch later
   (`miner reveal`) so the epoch beacon can be derived.
3. **Polls the relay** for its assigned matches, joins its seat, plays with the
   engine under a real **30+0** clock, and signs its moves.
4. **Co-signs the result** and turns the relay transcript into a `WinProof`.
5. **Announces the proof** to a node (`--node`), plus a timeout claim when it won
   on the clock and the relay's receipts are signed by a witness it trusts.

The relay does not trust the miner's clock reports: its own monotonic clock is
authoritative and every accepted move gets a signed receipt.

---

## 2. Prepare

```bash
cd somecryptochessproject
source .venv/bin/activate
pip install -e ".[dev]"
sccp --version
```

You need Python 3.11 or newer. A real engine also needs a binary (section 4.4);
the `mock` profile needs none. Always pass `--network` to `miner run`: without
it the CLI falls back to `testnet`, not to the keyfile's network.

---

## 3. Sanity check: one command, no setup

```bash
sccp devnet up --miners 4 --blocks 10 --engines mock
```

A smaller run (`--miners 2 --blocks 4`) prints:

```text
devnet up: height 4 after 0.970s
tip: c3b92857...
genesis: 20a94874...
epochs: 3; 3 matches; 1 proofs mined
  sccpt1qz4metcfj6spestnykg7dxh5q95nz70jducmanvg 99.99990000
  sccpt1qzg85eqh9as0t9z86sq02n7k5ug54cd0uu6mkp0s 99.99990000
```

This runs a complete network in process: it funds and registers the miners,
commits and reveals availability, pairs them by beacon, plays mock games, mines
the proofs and reports balances. It writes nothing to `--data-dir`; it is a
deterministic demonstration, not a chain you can query afterwards. If this
command fails, fix the install before starting real miner processes.

---

## 4. Run a relay and miners (works today)

This is the same wiring as `docker compose up`, without Docker.

### 4.1 Create a local stack

```bash
python deploy/init_testnet_stack.py --out ./stack
```

```text
stack for testnet in stack
beacon: defe1940...
  miner-1: sccpt1qrvwy92...
  miner-2: sccpt1qrrx3yp3...
  miner-3: sccpt1qqre4fx...
```

It writes:

| Path | Contents |
| --- | --- |
| `stack/relay.json` | Network, epoch beacon, roster (address, public key, engine, quota), timeout. |
| `stack/keys/miner-1..3.key` | Three encrypted keyfiles, derived from fixed, published seeds. |
| `stack/passphrase` | The published stack passphrase, mode `0600`. |
| `stack/stack.json` | Addresses, keyfile paths and availability nonce secrets. |

These keys and that passphrase are **public on purpose**. Testnet coins have no
value. Never reuse them on another network.

Two roster entries are the minimum for a match; three is convenient because the
odd miner waits while the other two play. The relay in `relay.json` opens round
`0` at startup.

### 4.2 Start the relay

```bash
sccp matchmaker run \
    --network testnet \
    --config ./stack/relay.json \
    --store ./stack/relay-state.json \
    --host 127.0.0.1 \
    --port 8646
```

```text
matchmaker on 127.0.0.1:8646 for testnet epoch 1 (1 matches, round 0, witness none)
```

- `--store` persists sessions and reputation across a relay restart.
- `--round-seconds 900` makes the relay open the next round every 15 minutes.
  Without it, only the round named in `relay.json` (or `--round N`) is opened.
- `--witness-key ./stack/keys/relay.key` signs the clock receipts. Create that
  keyfile first with `sccp wallet new --network testnet --out stack/keys/relay.key`
  and answer the passphrase prompt (or pass the same `--passphrase-file` when
  starting the relay); without a witness, receipts carry no signatures and cannot
  settle a timeout on chain. A claim only settles against a witness that is
  registered and bonded on chain; creating the keyfile is step one, and
  registering it as a clock witness is a separate `register_witness` transaction
  (PROTOCOL section 10.2) that this CLI does not expose yet.

Leave the relay running.

### 4.3 Start the miners

In one terminal per miner:

```bash
sccp --passphrase-file ./stack/passphrase miner run \
    --network testnet \
    --key ./stack/keys/miner-1.key \
    --engine-profile mock \
    --relay http://127.0.0.1:8646
```

The first line names the identity and the engine fingerprint:

```text
miner sccpt1qrvwy92u2es34fnpwnddkhtpw8wzqjpy6u6yszpz on testnet polling http://127.0.0.1:8646 with mock-v1:8c86f2e3eaa26cba
```

Start `miner-2` and `miner-3` the same way. A match needs both seats, so a lone
miner joins and waits; two miners that the beacon paired play, and the third
waits for another round. Progress looks like this:

```text
stopped after 30 polls (6.22s): 1 joins, 32 moves, 0 results, 0 proofs, 0 timeout claims
```

Stop a miner with `Ctrl-C`; it closes the engine and prints the summary. Useful
options while testing:

| Option | Meaning |
| --- | --- |
| `--iterations N` | Stop after N relay polls instead of running until interrupted. |
| `--poll-seconds 1.0` | Wait between polls (must be positive). |
| `--max-games-per-epoch N` | Availability quota this miner offers (default: the round count). |
| `--json` | Print one JSON summary instead of human lines; refusals appear under `refusals`. |
| `--node URL` | Announce win proofs to a node (section 6). |
| `--relay-witness-key HEX` | Trust a relay clock witness, so a flag can become a timeout claim. Repeatable. |

Without `--iterations`, the process runs until you interrupt it. It never exits
because a game was lost: it keeps polling for the next assignment.

If a miner refuses to act, the reason is printed (deduplicated) at the end:

```text
  refused: relay unavailable: cannot reach http://127.0.0.1:8646/v1/matches: [Errno 61] Connection refused
```

Fix the relay or the `--relay` URL; the miner does not silently retry with
different settings.

### 4.4 Use a real engine

Download a pinned Stockfish build (the script verifies the archive SHA-256):

```bash
python scripts/fetch_engine.py --list
python scripts/fetch_engine.py --engine stockfish-19 --dest ./bin
```

Run the miner with the matching profile and the binary:

```bash
sccp --passphrase-file ./stack/passphrase miner run \
    --network testnet \
    --key ./stack/keys/miner-1.key \
    --engine-profile stockfish-19 \
    --engine-path ./bin/stockfish-macos-universal \
    --relay http://127.0.0.1:8646 \
    --nodes-per-move 50000
```

- `--engine-profile` is one of `stockfish-19`, `stockfish-18` or `mock`. A real
  profile refuses to guess a binary: it needs `--engine-path`.
- `--nodes-per-move` is the search limit per move; the default comes from the
  network parameters (`50_000` on testnet, `250_000` on mainnet).
- `--depth N` uses a fixed depth instead of a node limit.
- The fingerprint in the first line is the SHA-256 of the binary you passed. The
  relay records an engine label from its roster in the match certificate; a
  verifier sees both, so publish an honest label.

`fetch_engine.py` writes a platform-specific file name into `./bin` (for example
`stockfish-macos-universal` on macOS, `stockfish-linux-x86-64-universal` on
Linux, `stockfish-windows-x86-64-universal.exe` on Windows). Check `ls ./bin`
and pass that exact path.

The mock profile is deterministic and fast; it is for wiring, not for earning.
A real engine needs a working UCI binary for your platform.

### 4.5 Add your own key to the roster

The relay only assigns matches to addresses in `relay.json`. To run your own
wallet as a miner:

```bash
sccp wallet new --network testnet --out alice.key
sccp --json wallet address --key alice.key
```

Copy `address` and `public_key` from the JSON, then add a roster entry (keep the
existing entries so there is an opponent). Replace the `sccpt1...` and `...`
placeholders with those two values before running it:

```bash
python - <<'PY'
import json
from pathlib import Path

path = Path("stack/relay.json")
document = json.loads(path.read_text())
document["roster"]["sccpt1..."] = {
    "public_key": "...",
    "engine": "stockfish-19",
    "max_games": 3,
}
path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
```

Restart the relay so it loads the new roster, then run `miner run` with
`--key alice.key`. The keyfile must be for the same network as the relay config.
Restart with a fresh store (delete `stack/relay-state.json` or pass a new
`--store` path): `relay.json` asks the relay to open round 0, and the old store
already has it, so the relay would exit with `round 0 of epoch 1 is already
open`.

The `engine` value is a printable label recorded in the certificate; the real
fingerprint of your binary is what `miner run` prints. Using the printed value
keeps the two consistent.

---

## 5. Put the miner on chain

The relay can pair a roster entry without any chain state, but the network only
treats an address as a matchable miner when the ledger says it is registered
through the epoch, holds at least the minimum bond, and is not banned.
Registration is what turns a wallet into a miner identity on chain.

You need a funded keyfile and the node's chain database:

```bash
sccp --data-dir ./data node run --network testnet --port 8645
```

The keyfile's **spendable** balance must cover the bond plus the fee. On testnet
the minimum bond is 10 coins; on mainnet 500; on devnet 0.

### 5.1 Register

```bash
sccp --data-dir ./data miner register \
    --network testnet \
    --key alice.key \
    --endpoint http://alice.example:8646 \
    --bond 10 \
    --engine stockfish-19
```

```text
registered sccpt1... with a 10.00000000 bond
txid: 1c9d...
```

- `--endpoint` is the public endpoint the relay dials (required).
- `--bond` is in whole coins and must be at least the network minimum.
- `--engine` is a printable label; use the fingerprint `miner run` prints.
- The bond moves from `spendable` to `bonded`; it is not spent, but it is locked.
  The ledger has `unregister` and `withdraw_bond` transaction types (PROTOCOL
  section 10.2), but this CLI has no commands for them yet, so treat the bond as
  locked for the life of the deployment.

Registration is valid for `registration_epochs` (10 on testnet, 30 on mainnet).
The transaction must be included in a block before the ledger changes; the CLI
gets as far as the node's mempool (section 5.4).

### 5.2 Commit availability

Availability tells the beacon which miners intend to play a future epoch:

```bash
sccp --data-dir ./data miner availability \
    --network testnet \
    --key alice.key \
    --max-games 3
```

```text
committed availability for epoch 3 (3 games)
txid: 7b20...
nonce secret (needed for the reveal): 5f3c...
```

- The target is always **two epochs ahead** (`availability_lead = 2` on testnet
  and mainnet). Building for any other epoch is refused.
- `--max-games` cannot exceed the epoch's round quota (3 on testnet).
- Keep the printed `nonce secret`: it is the only value that reveals the
  commitment. Losing it means the commitment cannot be opened.

### 5.3 Reveal

```bash
sccp --data-dir ./data miner reveal \
    --network testnet \
    --key alice.key \
    --nonce-secret 5f3c...
```

```text
revealed for epoch 3
txid: 9d11...
```

A reveal is only valid in the epoch immediately before its target. Once it is in
a block, the epoch's beacon can include your nonce. A registration without a
matching reveal leaves the miner out of the beacon data for that epoch.

### 5.4 What "submitted" means here

`miner register`, `miner availability` and `miner reveal` sign against the local
chain and submit to the node, exactly like `wallet send`. The node validates them
and puts them in its mempool. Registration and availability take effect when a
block includes them.

In this snapshot a live node does not mine automatically; the in-process
`sccp devnet up` is the settled demo, and testnet settlement is M6. Treat the
three commands above as correctly signed and admitted, not as instantly final.

---

## 6. Point the miner at a node

`--node` is optional. Without it the miner still plays and co-signs, but keeps
win proofs local; with it, every verified proof is announced to the node:

```bash
sccp --passphrase-file ./stack/passphrase miner run \
    --network testnet \
    --key ./stack/keys/miner-1.key \
    --engine-profile mock \
    --relay http://127.0.0.1:8646 \
    --node http://127.0.0.1:8645
```

If the node rejects a proof, the miner records a refusal with the node's reason
instead of crashing. Check that the node is the same network and serves the chain
you registered on; proofs carry the network id and are rejected on a mismatch.

---

## 7. Verify what is happening

The relay is HTTP; watch it directly:

```bash
curl -s http://127.0.0.1:8646/v1/status
curl -s http://127.0.0.1:8646/v1/matches
```

`/v1/matches` shows the pairing (white/black), status, SAN moves, the certificate
and the game result. `/v1/status` reports the beacon epoch, match count, current
round and reputation.

The chain side:

```bash
sccp --data-dir ./data chain info --network testnet
sccp --data-dir ./data chain blocks --limit 5 --network testnet
sccp --data-dir ./data chain account sccpt1... --network testnet
sccp --data-dir ./data wallet balance --network testnet --key alice.key
sccp --data-dir ./data wallet history --network testnet --key alice.key
```

`chain account` prints `registered until epoch N (engine, endpoint)` once the
registration is in the adopted chain, or `not registered` before that.

Always finish a debugging session with a full replay check:

```bash
sccp --data-dir ./data chain verify --network testnet
```

```text
ok: N blocks replay to height N-1 (tip...)
```

---

## 8. Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `error: engine profile 'stockfish-19' needs an explicit binary; pass an engine path or run scripts/fetch_engine.py` | A real profile was selected without `--engine-path`. | Fetch a pinned build and pass its path, or use `--engine-profile mock`. |
| `error: cannot start engine ...` | The binary is missing, not executable, or not a UCI engine. | Check the path and permissions; run it by hand once. |
| `error: relay unavailable: cannot reach ...` | The relay is not listening at `--relay`. | Start `sccp matchmaker run` and check the URL/port. |
| `error: --relay-witness-key '...' is not valid hex` | The witness key is not a hex public key. | Pass the 64-character hex `public_key` from `wallet address --json`. |
| `error: --poll-seconds must be positive` | `--poll-seconds 0` or negative. | Use a positive number. |
| `error: --iterations cannot be negative` | Negative poll count. | Use `0` (forever) or a positive count. |
| `error: keyfile belongs to network '...', but --network '...' was requested` | Keyfile and relay/network disagree. | Use a keyfile for the relay's network. |
| `error: relay config needs a non-empty roster object` | `relay.json` lost its roster. | Re-run `deploy/init_testnet_stack.py`, or add entries. |
| `error: relay config needs a beacon object` | `relay.json` is missing the epoch beacon. | Regenerate the stack; the beacon must match the epoch. |
| `error: relay roster entry ... needs an engine fingerprint` | A roster entry has no `engine`. | Add a printable engine label to the entry. |
| `error: config is for 'testnet', but --network selected 'devnet'` | `--network` and the config disagree. | Drop `--network` or fix the config. |
| `round 0 of epoch 1 is already open` | The relay store already contains that round and `open_round` tries again. | Remove the store for a fresh start, or open another `--round N`. |
| A miner joins but never moves | The other seat never joined, or the round has no match for it. | Run at least two roster miners; check `/v1/matches`. |
| `refused: ...` lines in the summary | The miner saw an error it could not fix. | Read the reason; it is the node's or relay's own message. |

Exit codes: `0` clean shutdown, `1` refused by the CLI or a service, `2` invalid
usage.

---

## 9. Operational safety

- **The bond is real.** Registration locks coins; slashing and challenge
  settlement can take them. Read [`THREAT_MODEL.md`](THREAT_MODEL.md) before
  running a miner with value at stake.
- **Stack keys are public.** `deploy/init_testnet_stack.py` derives its keys from
  fixed seeds and publishes the passphrase. Never use them on mainnet.
- **One process per keyfile.** Two miners signing with the same key race on
  nonces and confuse reputation.
- **Trust only witnesses you know.** `--relay-witness-key` lets the miner build a
  timeout claim from that witness's receipts. A wrong or malicious key can make
  the claim unverifiable and cost the bond.
- **Protect the availability secret** until the reveal is in a block, and keep
  the keyfile passphrase in a mode-0600 file or the environment, as described in
  [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md).
- **Watch maturity.** Mining rewards are `immature` until `coinbase_maturity`
  blocks pass (20 on testnet, 100 on mainnet); they cannot pay a bond before
  then.

---

## See also

- [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — create and back up the miner's
  keyfile.
- [`SEND_COINS.md`](SEND_COINS.md) — fund the bond and understand spendable vs
  bonded coins.
- [`PLAY_AND_EXPORT.md`](PLAY_AND_EXPORT.md) — play a relay match by hand.
- [`deploy/README.md`](../deploy/README.md) — the docker-compose testnet stack
  and what it deliberately does not do yet.
- [`THREAT_MODEL.md`](THREAT_MODEL.md) — adversaries, slashing and quality
  challenges.
- [`PROTOCOL.md`](PROTOCOL.md) — match, proof and clock formats.
