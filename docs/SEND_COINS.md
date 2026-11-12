# Send coins (transfer)

A transfer is signed on your machine with the keyfile, then submitted to a node.
The node validates it and holds it in its mempool; the balance changes only when
a block includes it. Nothing about your private key leaves the host.

`wallet send` is one command:

```bash
sccp --data-dir ./data wallet send \
    --network testnet \
    --key alice.key \
    --to sccpt1qrrx3yp3ufmfsqk6l9u0wg7xpjd7670v5v85vgp9 \
    --amount 12 \
    --node http://127.0.0.1:8645
```

```text
sent 12.00000000 to sccpt1qrrx3yp3ufmfsqk6l9u0wg7xpjd7670v5v85vgp9 (fee 0.00001000)
txid: 4f0c1a...
```

If you created the wallet already, this is the whole flow. The rest of this
guide explains the prerequisites, what the command checks, and how to prove the
coins moved.

---

## 1. What you need

| Item | Why |
| --- | --- |
| A keyfile | `wallet send` signs with it and refuses a keyfile from another network. |
| A chain database under `--data-dir` | The command reads your nonce and spendable balance from the local chain. |
| A running node | The signed transaction is POSTed to `--node` (default `http://127.0.0.1:8645`). Use `http://testnet.somecryptochessproject.com:8645` for the hosted testnet. |
| A recipient address | A `sccpt1...` address on testnet/devnet, `sccp1...` on mainnet. |
| Spendable coins | `amount + fee` must be **spendable**; `bonded` and `immature` coins cannot be spent. |

The data directory must be the one the node is serving. The CLI reads
`<data-dir>/chain.sqlite` directly instead of trusting a remote balance, so run
it on the node's host (or point it at a copy of that database). Always pass
`--network`: without it the CLI falls back to its default (`testnet`), not to the
keyfile's network. If the chain is not there, you get:

```text
error: no chain database at data/chain.sqlite; run `sccp --data-dir data node run --network testnet` first
```

### Start a node

```bash
sccp --data-dir ./data node run --network testnet --port 8645
```

Leave it running in its own terminal. It prints the height and tip, then serves
the API on `127.0.0.1:8645`.

---

## 2. Fund a test wallet

On `testnet` and `devnet`, genesis funds a public faucet. With the node running:

```bash
sccp --data-dir ./data faucet send --network testnet --to sccpt1qrvk... --amount 1000
```

```text
faucet sent 1000.00000000 to sccpt1qrvk...
txid: 9a2b...
```

The faucet is unauthenticated by design and exists only on test networks. Use it
only for coins with no value.

Check the balance:

```bash
sccp --data-dir ./data wallet balance --network testnet --key alice.key
```

```text
sccpt1qrvk... 1000.00000000 (spendable 1000.00000000, bonded 0.00000000, immature 0.00000000)
```

The three buckets mean:

- **spendable** — available to a transfer right now.
- **bonded** — locked behind a miner registration; `wallet send` cannot touch it.
- **immature** — a block reward still inside `coinbase_maturity` (20 blocks on
  testnet, 100 on mainnet).

The faucet transaction must be included in a block before the coins are
spendable; see section 5.

---

## 3. Amounts, fees and addresses

Amounts are **whole coins as integers**. There are no decimal amounts on the
command line:

```bash
--amount 12        # 12.00000000 coins
--amount 0         # error: a transfer amount must be positive
```

One coin is `100_000_000` base units. `--fee` is in base units:

```bash
--fee 1000         # 0.00001000 coins; the default and the network minimum
--fee 1            # error: fee 1 is below the network minimum of 1000
```

The fee is paid to the miner whose block includes the transfer.

Addresses are bech32 and network-specific:

| Network | Example prefix | Valid on |
| --- | --- | --- |
| `mainnet` | `sccp1...` | mainnet only |
| `testnet` | `sccpt1...` | testnet |
| `devnet` | `sccpt1...` | devnet |

A malformed or wrong-network address is refused before anything is signed:

```text
error: 'sccp1...' is not a valid testnet address
```

Copy the recipient address with care: a transfer cannot be cancelled or
reversed. There is no memo field and no refund path.

---

## 4. What the command does

1. Reads the keyfile header and refuses it if `--network` names another network.
2. Loads the chain from `--data-dir` and derives the sender address from the
   keypair.
3. Checks the recipient address, `amount > 0`, `recipient != sender`, the fee
   floor, and `spendable >= amount + fee`.
4. Uses the account's current **nonce**.
5. Signs the transaction locally with Ed25519.
6. POSTs it to `<node>/v1/tx` and prints the txid and whether the node accepted
   it.

The private seed is decrypted only in this process and is never sent to the
node. The node receives the signed transaction and the public key, exactly as it
would from any peer.

### One transfer at a time per address

The nonce comes from the adopted chain, not from the mempool. If you submit two
transfers before the first is mined, both use the same nonce and the node refuses
the second:

```text
transaction rejected (...): sccpt1... already has a pending transaction with nonce 3
```

Wait for the first transfer to be included in a block (or for the mempool to be
cleared) before sending again from the same address.

---

## 5. Watch it settle

`accepted: true` (or the human line `sent ...`) means **the node admitted the
transaction to its mempool**. It does not mean a block includes it yet.

While it is pending:

```bash
curl -s http://127.0.0.1:8645/v1/transactions/<txid>
```

```json
{"txid": "...", "network": "testnet", "type": "transfer", "height": null, "in_mempool": true}
```

The mempool endpoint lists what is waiting:

```bash
curl -s http://127.0.0.1:8645/v1/mempool
```

After a miner includes it in a block, the same transaction reports a `height`,
and the balances move:

```bash
sccp --data-dir ./data wallet balance --network testnet --key alice.key
sccp --data-dir ./data wallet history --network testnet --key alice.key
sccp --data-dir ./data chain blocks --limit 5
```

`wallet history` reads the adopted chain, not the mempool, so a pending transfer
does not appear there yet. Once mined, each row shows the height, type, signed
delta, fee, counterparty and txid:

```text
     7 transfer           -12.00001000 sccpt1qrrx... 4f0c1a...
```

`wallet balance` and `chain account` show the same numbers:

```bash
sccp --data-dir ./data chain account sccpt1qrvk... --network testnet
```

```text
sccpt1qrvk... (testnet)
total 987.99999000: spendable 987.99999000, bonded 0.00000000, immature 0.00000000
nonce 4
```

### Status in this snapshot

A node started with `sccp node run --mine --miner-address ADDRESS` runs a proof
settler: it takes an eligible proof from the mempool and builds the next block
around it, including any pending transfers. The docker-compose testnet starts
that settler automatically. After the bootstrap window the node does not mine
empty blocks, so a transfer confirms with the next decisive game; a draw
produces no proof and no block. See [`RUNBOOK.md`](RUNBOOK.md) for the exact
bootstrap and settlement behaviour.

---

## 6. Machine-readable output

```bash
sccp --json --data-dir ./data wallet send \
    --network testnet --key alice.key --to sccpt1... --amount 12
```

```json
{"accepted": true, "amount": 1200000000, "fee": 1000, "from": "sccpt1...", "network": "testnet", "nonce": 3, "to": "sccpt1...", "txid": "...", "type": "transfer"}
```

`wallet history --json` returns `{"address", "network", "history": [...]}`, with
one object per transaction (`txid`, `height`, `type`, `delta`, `fee`,
`counterparty`). `wallet balance --json` returns `address`, `network`,
`spendable`, `immature`, `bonded` and `total`, all in base units.

---

## 7. Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `no chain database at ...` | `--data-dir` is wrong or the node never ran there. | Start the node with that `--data-dir`, or pass the right one. |
| `... has N spendable, the transfer needs M` | `amount + fee` exceeds spendable coins (bonded/immature do not count). | Lower the amount, wait for maturity, or use another address. |
| `fee N is below the network minimum of 1000` | `--fee` is too small. | Omit `--fee` or use at least `1000`. |
| `a transfer amount must be positive` | `--amount` is zero or negative. | Use a positive integer. |
| `a transfer must name a different recipient` | `--to` is the sender's own address. | Send to another address. |
| `'...' is not a valid testnet address` | Typo, checksum error, or another network's address. | Re-copy the address; check the prefix. |
| `keyfile belongs to network '...', but --network '...' was requested` | The keyfile is for another network. | Use the matching network or keyfile. |
| `connection refused` / `cannot reach http://127.0.0.1:8645` | No node is listening at `--node`. | Start `sccp node run`, or pass the correct URL. |
| `... already has a pending transaction with nonce N` | A second send was built from the same chain height. | Wait for the first transfer to be mined. |
| A node-side rejection (mempool error, nonce clash, malformed signature) | The node's chain or mempool disagrees with the locally built transaction. | Read the node's reason; re-sync the data directory before retrying. |

Exit codes: `0` signed and submitted, `1` refused by the CLI or the node, `2`
invalid usage.

---

## 8. Safety checklist

- [ ] Re-read the recipient address before pressing Enter; transfers are final.
- [ ] Confirm the network: `sccpt1...` on testnet/devnet, `sccp1...` on mainnet.
- [ ] Keep the passphrase out of the command line; use a mode-0600
      `--passphrase-file` or `SCCP_WALLET_PASSPHRASE`.
- [ ] Send a small amount first when using a new address.
- [ ] Do not hand-edit nonces; the CLI already reads the next one from the chain.
- [ ] Remember that `bonded` coins are a miner bond, not spendable balance.

---

## See also

- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — create a keyfile
  and fund it from the HTTP faucet before sending.
- [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md) — create a keyfile, back it up, read its
  address.
- [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — register a miner with a
  bond and earn block rewards.
- [`PLAY_AND_EXPORT.md`](PLAY_AND_EXPORT.md) — play a game on-chain.
- [`PROTOCOL.md`](PROTOCOL.md) — normative transaction and address formats.
