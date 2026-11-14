# Create a testnet wallet and fund it

This is the short path from nothing to a funded `testnet` wallet:

1. create an encrypted keyfile locally;
2. back up its 24-word phrase;
3. ask a testnet node's faucet for coins;
4. wait for the transfer to be included in a block;
5. read the balance.

Creating and backing up the wallet is offline. Only steps 3–5 talk to a node.
Everything here uses **testnet coins**, which have no value.

For the full keyfile and backup reference, see
[`OPEN_A_WALLET.md`](OPEN_A_WALLET.md). For sending coins afterwards, see
[`SEND_COINS.md`](SEND_COINS.md).

---

## 1. Install the CLI

From the repository root:

```bash
cd somecryptochessproject
python -m venv .venv
source .venv/bin/activate
pip install -e .
sccp --version
```

```text
sccp 0.17.0 (protocol v1)
```

Python 3.11 or newer is required. The HTTP examples pipe JSON through `jq` for
readability; install it separately or drop the `| jq` suffix.

---

## 2. Create the wallet

```bash
sccp wallet new --network testnet --out ~/.sccp/testnet-alice.key
```

The command prompts for a passphrase twice, then prints the address:

```text
Passphrase:
Repeat for confirmation:
sccpt1qrvk6qhg6jc72c2evv8kgyj8cf3y29hj85d2xj0x
```

The last line is the address. Copy it; the faucet needs it.

What to know before continuing:

- the keyfile is written with mode `0600` and is encrypted with your passphrase;
- `wallet new` does **not** print the recovery phrase;
- the passphrase is not stored and cannot be reset;
- a `testnet` address starts with `sccpt1`.

Store the keyfile outside the repository. Do not commit it.

### Back up the recovery phrase now

```bash
sccp wallet export-mnemonic --network testnet \
    --key ~/.sccp/testnet-alice.key
```

Write the 24 words down, in order, offline. Anyone with the phrase can recreate
the wallet, so treat it as the wallet itself. The keyfile copy is also encrypted
with your passphrase; if you lose both the phrase and the passphrase, the funds
are gone.

Check the address at any time without the passphrase:

```bash
sccp wallet address --network testnet --key ~/.sccp/testnet-alice.key
```

Use `--json` on `wallet new`, `wallet address` or `wallet export-mnemonic` when
a script needs structured output:

```json
{"address": "sccpt1...", "keyfile": "...", "network": "testnet", "public_key": "..."}
```

---

## 3. Point at a testnet node

The faucet and balance endpoints are served by the node on port `8645`:

```bash
export SCCP_NODE="http://127.0.0.1:8645"
```

For the hosted two-miner deployment, use the public testnet host:

```bash
export SCCP_NODE="http://testnet.somecryptochessproject.com:8645"
```

The hosted chain is reset on every deployment, so addresses and balances from
an earlier deploy are gone. Use `http://127.0.0.1:8645` when running the stack
locally from source.

The testnet deployment does **not** terminate TLS. If the node is behind a reverse
proxy, use its HTTPS URL instead. Check that the node is alive and on testnet:

```bash
curl -sS "$SCCP_NODE/v1/status" | jq '{network, height, tip_hash, mempool}'
```

```json
{"network": "testnet", "height": 19, "tip_hash": "...", "mempool": {"transactions": 0, "proofs": 0}}
```

`height: 19` is the normal post-bootstrap height of the two-miner stack.

---

## 4. Ask the faucet for coins

Read the live policy first:

```bash
curl -sS "$SCCP_NODE/v1/faucet" | jq
```

```json
{
  "network": "testnet",
  "address": "sccpt1q...",
  "amount": 1000,
  "max_amount": 1000,
  "cooldown_seconds": 3600,
  "client_window_seconds": 3600,
  "client_grants": 5
}
```

The defaults are one grant of **1,000 test coins** per address per hour, and at
most **five grants per client IP per hour**. Behind a reverse proxy, make sure
the proxy forwards the real client address, or every user will share one
five-grant window. The limiter is in memory, so restarting the node clears every
cooldown.

Capture your address and make the request:

```bash
ADDRESS="$(sccp wallet address --network testnet \
    --key ~/.sccp/testnet-alice.key)"

curl -sS -X POST "$SCCP_NODE/v1/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$ADDRESS\"}" | jq
```

```json
{
  "network": "testnet",
  "address": "sccpt1qrvk...",
  "amount": 1000,
  "txid": "9a2b..."
}
```

`amount` is optional and must be between 1 and 1,000 whole coins:

```bash
curl -sS -X POST "$SCCP_NODE/v1/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$ADDRESS\",\"amount\":250}" | jq
```

A successful request means the transfer was **signed by the faucet key and
admitted to the node's mempool**. It does not mean the coins are spendable yet.

---

## 5. Wait for inclusion, then check the balance

Track the transaction by the returned `txid`:

```bash
export TXID="9a2b..."   # value from the faucet response
curl -sS "$SCCP_NODE/v1/transactions/$TXID" | jq
```

Pending:

```json
{
  "txid": "9a2b...",
  "network": "testnet",
  "type": "transfer",
  "height": null,
  "in_mempool": true,
  "block_hash": null
}
```

Confirmed:

```json
{
  "txid": "9a2b...",
  "network": "testnet",
  "type": "transfer",
  "height": 20,
  "in_mempool": false,
  "block_hash": "..."
}
```

Once `height` is set, read the account:

```bash
curl -sS "$SCCP_NODE/v1/accounts/$ADDRESS" | jq
```

```json
{
  "network": "testnet",
  "address": "sccpt1qrvk...",
  "balance": 100000000000,
  "immature": 0,
  "bonded": 0,
  "total": 100000000000,
  "nonce": 1
}
```

Or monitor for updates
```bash
curl -sS "$SCCP_NODE/v1/transactions/$TXID" | jq
curl -sS "$SCCP_NODE/v1/mempool" | jq
curl -sS "$SCCP_NODE/v1/status" | jq '{height, mempool}'
```

Amounts from the HTTP API are integer **base units**, and
`1 coin = 100,000,000` units. The example balance is 1,000 coins.

The CLI formats the same account, and asks a node for it, so this works from any
machine against a hosted node:

```bash
sccp wallet balance --network testnet \
    --key ~/.sccp/testnet-alice.key --node "$SCCP_NODE"
```

```text
sccpt1qrvk... 1000.00000000 (spendable 1000.00000000, bonded 0.00000000, immature 0.00000000)
```

`wallet balance` reads `GET /v1/accounts/{address}` from `--node` (default
`http://127.0.0.1:8645`) and falls back to `<data-dir>/chain.sqlite` only when no
node answers. `--json` reports which one it used in the `source` field.


### Why a faucet transfer can stay pending

The testnet never mines an empty block. A block is built when there is a proof to
settle, or -- after bootstrap -- when the mempool holds at least one transaction:
that *heartbeat* block carries no proofs, mints no subsidy and takes exactly one
pending transaction, so it cannot be used to farm coins. Pending faucet transfers
ride along in the next block that is built for either reason, together with the
miners' own availability and reveal transactions, which the miners in this guide
submit every epoch. Because a heartbeat block absorbs one transaction and not the
whole pool, the queue moves one block at a time, which is what carries the chain
across an epoch boundary when no proof is available.

So after a deploy:

- a transfer usually confirms within an epoch: the miners' availability and
  reveal transactions are enough to produce heartbeat blocks on their own;
- if nothing at all is pending -- no proof and no transaction -- no block can be
  built, and the transfer waits for the next transaction;
- the faucet endpoint cannot force a block. Only the mempool can.

This is a demonstration-chain property, not a wallet problem. Check `/v1/status`
and the node log before assuming the faucet failed.

---

## 6. Alternative: operator CLI faucet

`sccp faucet send` is the operator path. It builds the same transfer from the
published faucet key, reads the faucet's balance and next nonce from `--node`
(falling back to `--data-dir` when that node does not answer), and it **bypasses
the HTTP rate limits**:

```bash
sccp faucet send --network testnet --to "$ADDRESS" --amount 1000 \
    --node "$SCCP_NODE"
```

```text
faucet sent 1000.00000000 to sccpt1qrvk...
txid: 9a2b...
```

Use this only when you operate the node: it spends from the faucet key with no
rate limit. It still only admits the transaction; inclusion follows the same
proof-driven rule.

---

## 7. Common errors

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Connection refused` | Node is not running or the port is wrong. | Check `curl "$SCCP_NODE/v1/status"` and the firewall. |
| HTTP `404` with `the faucet is disabled` | The node was started with `--no-faucet`, or is not on testnet/devnet. | Use a faucet-enabled testnet node. |
| HTTP `400` with `not a valid testnet address` | Address prefix or checksum is wrong. | Create the keyfile with `--network testnet` and copy the exact address. |
| HTTP `400` with `faucet amount must be between 1 and 1000 coins` | Requested amount is outside the policy. | Use 1–1,000, or omit `amount` for 1,000. |
| HTTP `429` with `Retry-After` | The address already received a grant, or this client sent five. | Wait the advertised seconds, or use a different address/client. |
| `txid` known but `height` stays `null` | The transfer is still in the mempool. | Wait for a proof-settled block; see the caveat above. |
| `no chain database at ...` from CLI commands | A local-only command ran without the node's data directory. | Pass the node's `--data-dir`, or use the HTTP endpoints. |
| `keyfile already exists` | The `--out` path is taken. | Choose another path, or use `--force` only for a disposable key. |
| `wrong passphrase or corrupted ciphertext` | Passphrase is wrong or the keyfile was edited. | Retry; restore from the 24-word phrase if needed. |

Exit codes for CLI commands: `0` success, `1` refused/failed, `2` invalid usage.

---

## 8. Safety checklist

- [ ] The keyfile is mode `0600` and outside the repository.
- [ ] The 24-word phrase is offline and stored separately from the keyfile.
- [ ] The passphrase is in a password manager, never in the keyfile.
- [ ] The address starts with `sccpt1` and the keyfile says `"network": "testnet"`.
- [ ] The node's `/v1/status` says `"network": "testnet"`.
- [ ] You did not send anything of value: testnet coins are valueless and the
      faucet key is public.

---

## See also

- [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — full keyfile, mnemonic and network
  reference.
- [`SEND_COINS.md`](SEND_COINS.md) — spend the funded balance.
- [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — use the wallet as a miner.
- [`RUNBOOK.md`](RUNBOOK.md) — faucet policy and testnet operations.

