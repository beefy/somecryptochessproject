# Send coins (transfer)

A transfer is signed on your machine with the keyfile, then submitted to a node.
The node validates it and holds it in its mempool; the balance changes only when
a block includes it. Nothing about your private key leaves the host.

`wallet send` is one command:

```bash
sccp wallet send \
    --network testnet \
    --key alice.key \
    --to sccpt1qrrx3yp3ufmfsqk6l9u0wg7xpjd7670v5v85vgp9 \
    --amount 12 \
    --node http://testnet.somecryptochessproject.com:8645
```

```text
sent 12.00000000 to sccpt1qrrx3yp3ufmfsqk6l9u0wg7xpjd7670v5v85vgp9 (fee 0.00001000)
txid: 4f0c1a...
```

If you created the wallet already, this is the whole flow, on any machine: the
node supplies your balance and next nonce, so there is nothing to sync and no
chain database to find first. The rest of this guide explains the prerequisites,
what the command checks, and how to prove the coins moved.

---

## 1. What you need

| Item | Why |
| --- | --- |
| A keyfile | `wallet send` signs with it and refuses a keyfile from another network. |
| A running node | The command reads your balance and next nonce from it and POSTs the signed transfer back to it. The default is `http://127.0.0.1:8645`; the hosted testnet is `http://testnet.somecryptochessproject.com:8645`. |
| A recipient address | A `sccpt1...` address on testnet/devnet, `sccp1...` on mainnet. |
| Spendable coins | `amount + fee` must be **spendable**; `bonded` and `immature` coins cannot be spent. |

A transfer needs two numbers -- the spendable balance and the next nonce -- and
only the node that will judge the transaction has the ones that count. So the
wallet asks it (`GET /v1/accounts/{address}`) and signs against that answer,
which is why nothing has to be synced and why the command works from any machine.
Add `--json` to see the source it used in the `source` field.

`--data-dir` is the fallback for when no node answers, and it is what makes an
offline build possible: the same two numbers are then read from
`<data-dir>/chain.sqlite`, so that database must be the chain the node will
accept. Always pass `--network`: without it the CLI falls back to its default
(`testnet`), not to the keyfile's network. When neither answers, you get:

```text
error: no chain database at data/chain.sqlite; run `sccp --data-dir data node run --network testnet` first
```

A `chain.sqlite` that exists but holds no blocks is not an error: the command
runs, and every balance it reports is `0.00000000`, because the balances it
reads are the ones that chain knows about. When such an empty result is what you
are looking at — a wallet balance of zero, or a refused transfer — the CLI says
so on stderr, because only a node that stayed silent can get you there:

```text
note: data/chain.sqlite holds only the genesis block, so it knows no other balances,
and no node answered with one. Start the node that holds the coins and pass its URL
as --node: the wallet reads balances and nonces from that node, and --data-dir is
only the fallback for building a transfer offline.
```

A node keeps recent blocks in `chain.sqlite-wal` until it checkpoints, so a copy
of the database taken **while the node runs** must include `chain.sqlite-wal`
too; otherwise the copy opens at height 0. Pointing `--data-dir` straight at the
node's directory is simpler and always current — SQLite's write-ahead log lets
the wallet read the file the node is writing.

### Start a node

```bash
sccp --data-dir ./data node run --network testnet --port 8645
```

Leave it running in its own terminal. It prints the height and tip, then serves
the API on `127.0.0.1:8645`.

---

## 2. Fund a test wallet

On `testnet` and `devnet`, genesis funds a public faucet. With a node running:

```bash
sccp faucet send --network testnet --to sccpt1qrvk... --amount 1000 \
    --node http://127.0.0.1:8645
```

```text
faucet sent 1000.00000000 to sccpt1qrvk...
txid: 9a2b...
```

Like `wallet send`, the faucet reads its own balance and next nonce from
`--node` and falls back to `--data-dir` only if that node does not answer.

The faucet is unauthenticated by design and exists only on test networks. Use it
only for coins with no value.

Check the balance:

```bash
sccp wallet balance --network testnet --key alice.key \
    --node http://127.0.0.1:8645
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
--fee 1            # error: fee 1 base units is below the network minimum of 1000 base units
```

Both units appear in a single send, so the arithmetic in an error message can
look odd: `--amount 12` is `12 * 100_000_000 = 1_200_000_000` base units, and
with the minimum fee the transfer needs `1_200_001_000` base units. The CLI
prints both sides in coins:

```text
error: sccpt1... has 0.00000000 spendable, the transfer needs 12.00001000 (12.00000000 plus a 0.00001000 fee); this balance came from the node at http://127.0.0.1:8645
```

The trailing clause names where the number came from, so an empty balance is easy
to attribute: a node that reports zero is a node serving a chain where that
address has not been funded (see section 7 for the two-fork case).

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
2. Asks `--node` for `GET /v1/accounts/<sender>`; only if that node does not
   answer does it load the chain from `<data-dir>/chain.sqlite`. The sender
   address is derived from the keypair either way.
3. Checks the recipient address, `amount > 0`, `recipient != sender`, the fee
   floor, and `spendable >= amount + fee`.
4. Uses the nonce the node reported (or the local chain's, when building offline).
5. Signs the transaction locally with Ed25519.
6. POSTs it to `<node>/v1/tx` and prints the txid and whether the node accepted
   it.

The private seed is decrypted only in this process and is never sent to the
node. The node receives the signed transaction and the public key, exactly as it
would from any peer.

### One transfer at a time per address

The nonce comes from the account's chain state, not from the mempool. If you
submit two transfers before the first is mined, both use the same nonce and the
node refuses the second:

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
sccp wallet balance --network testnet --key alice.key --node http://127.0.0.1:8645
sccp --data-dir ./data wallet history --network testnet --key alice.key
sccp --data-dir ./data chain blocks --limit 5
```

`wallet balance` asks the node, so it is current the moment the block lands.
`wallet history` and `chain blocks` walk the adopted chain in `--data-dir` and do
not consult the node, so those two need that database; a transfer still in the
mempool appears in neither. Once mined, each history row shows the height, type,
signed delta, fee, counterparty and txid:

```text
     7 transfer           -12.00001000 sccpt1qrrx... 4f0c1a...
```

`wallet balance`, `chain account` and `GET /v1/accounts/{address}` show the same
numbers:

```bash
sccp --data-dir ./data chain account sccpt1qrvk... --network testnet
curl -s http://127.0.0.1:8645/v1/accounts/sccpt1qrvk...
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
that settler automatically. After the bootstrap window the node never builds an
empty block, but it does build a *heartbeat* block whenever the mempool holds at
least one transaction; such a block mints no subsidy, so skipping proofs never
pays, and it carries exactly one transaction, so a backlog of ledger work turns
into one block per transaction -- the chain's clock only advances that way when
no proof is available. Because the stack's miners commit availability and reveal
it every epoch, a transfer normally confirms well within one epoch. See
[`RUNBOOK.md`](RUNBOOK.md) for the exact bootstrap and settlement behaviour.

---

## 6. Machine-readable output

```bash
sccp --json wallet send \
    --network testnet --key alice.key --to sccpt1... --amount 12 \
    --node http://127.0.0.1:8645
```

```json
{"accepted": true, "amount": 1200000000, "fee": 1000, "from": "sccpt1...", "network": "testnet", "nonce": 3, "source": "http://127.0.0.1:8645", "to": "sccpt1...", "txid": "...", "type": "transfer"}
```

`wallet history --json` returns `{"address", "network", "history": [...]}`, with
one object per transaction (`txid`, `height`, `type`, `delta`, `fee`,
`counterparty`). `wallet balance --json` returns `address`, `network`,
`spendable`, `immature`, `bonded`, `total` (those three amounts in base units) and
`source`. In both commands `source` is where the numbers came from: the node's
URL when it answered, otherwise the path of the chain database that was used
instead.

---

## 7. Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `no chain database at ...` | No node answered (the default is `http://127.0.0.1:8645`) and `--data-dir` holds no `chain.sqlite`. | Pass `--node` with the node's URL, or start the node with that `--data-dir`. |
| `note: ... holds only the genesis block ...` | No node answered and the fallback database has no blocks. Every balance read from it is zero. | Pass `--node` with the URL of the node that holds the coins. |
| `... has 0.00000000 spendable` although the node reports a balance | The trailing clause says where the number came from. If it names the local chain, `--data-dir` points at another chain -- often one at height 0, or a copy taken without `chain.sqlite-wal`. | Pass `--node` with the URL of the node that holds the coins, or point `--data-dir` at that node's own data directory. |
| `cannot read sccpt1... from the node at <url>: it did not answer` | A `--node` you named is unreachable, so the command refuses rather than guess. | Start that node, fix the URL (`http://host:port`), or drop `--node` to build from `--data-dir`. |
| `the node serves mainnet, not testnet: ...` | `--node` serves another network's chain, so its balance and nonce would be wrong for this keyfile. | Use the matching `--network` and keyfile, or point `--node` at a node serving your network. |
| `... has N.NNNNNNNN spendable, the transfer needs M.MMMMMMMM` | `amount + fee` exceeds spendable coins (bonded/immature do not count). | Lower the amount, wait for maturity, or use another address. |
| `fee N base units is below the network minimum of M base units` | `--fee` is too small. | Omit `--fee` or use at least `1000`. |
| `a transfer amount must be positive` | `--amount` is zero or negative. | Use a positive integer. |
| `a transfer must name a different recipient` | `--to` is the sender's own address. | Send to another address. |
| `'...' is not a valid testnet address` | Typo, checksum error, or another network's address. | Re-copy the address; check the prefix. |
| `keyfile belongs to network '...', but --network '...' was requested` | The keyfile is for another network. | Use the matching network or keyfile. |
| `connection refused` / `cannot reach http://127.0.0.1:8645` | No node is listening at `--node`. | Start `sccp node run`, or pass the correct URL. |
| `... already has a pending transaction with nonce N` | A second send was built from the same chain height. | Wait for the first transfer to be mined. |
| A node-side rejection (mempool error, nonce clash, malformed signature) | The node's chain or mempool disagrees with the locally built transaction. | Read the node's reason; re-sync the data directory before retrying. |

Exit codes: `0` signed and submitted, `1` refused by the CLI or the node, `2`
invalid usage.

### Two chains from one genesis

Testnet and devnet addresses share a prefix, and genesis funds the same faucet key
on both. Two chains started from the same genesis but never peered therefore look
identical to a wallet -- same address, same faucet -- while holding different
coins. A node only pull-syncs from `--peer` **at startup**, so a node that was
started without a peer never learns about blocks mined elsewhere, and a wallet
that reads it faithfully reports that fork's balances.

Choose the side that holds the coins and stay on it: pass `--node` with that
node's URL for every wallet command, and restart a node you started without
`--peer` so it pulls the chain it is missing on the way up.

---

## 8. Safety checklist

- [ ] Re-read the recipient address before pressing Enter; transfers are final.
- [ ] Confirm the network: `sccpt1...` on testnet/devnet, `sccp1...` on mainnet.
- [ ] Keep the passphrase out of the command line; use a mode-0600
      `--passphrase-file` or `SCCP_WALLET_PASSPHRASE`.
- [ ] Send a small amount first when using a new address.
- [ ] Do not hand-edit nonces; the CLI reads the next one from the node (or from
      `--data-dir` when it builds offline).
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
