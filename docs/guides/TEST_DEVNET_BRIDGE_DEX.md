# Test the devnet bridge, DEX and Chess960

This is an end-to-end devnet runbook for the three newest pieces of the stack:

- the trustless **ETH ↔ SCCP bridge** (`EthHtlc`, signed orders, SCCP HTLCs);
- the on-chain **DEX** (constant-product pools, LP shares, swaps);
- the **Chess960 / C960** path (variant registration, a relay match, a settled
  C960 proof, and C960 balances and transfers).

It gives both a **CLI** path and a **web UI** path for every user action. The
node, relay and miners are intentionally CLI-only: they are operator processes,
not browser features.

The runbook is written for a fresh checkout and a fresh devnet. It uses four
terminals plus a browser; the long-lived processes are the node, the relay and
two (or four) miners.

> **Two facts that explain the shape of this guide.**
>
> 1. The devnet SCCP faucet funds **SCCP only**. C960 is not minted by the
>    faucet: consensus creates C960 when a block settles a Chess960 proof, plus
>    the fixed testnet/mainnet genesis allocation. This guide uses devnet, so it
>    starts two Chess960 miners once, lets Alice's node miner collect the C960
>    coinbase, and only then creates the SCCP/C960 pool. The hosted testnet needs
>    none of that: its genesis already seeds the pool and a bridge example.
> 2. In this build the C960 coinbase is credited to the **block miner**
>    (`header.miner`, the node's `--miner-address`), not directly to the game
>    winner. The guide uses Alice as both the node miner and the LP, so the C960
>    lands in the wallet that will fund the pool.

---

## 0. Prerequisites and a clean slate

You need:

- Python 3.11+ and the checkout's virtualenv (`make install` or
  `pip install -e ".[dev]"`);
- Node.js 20.19+ or 22.12+ and npm, for the web bundle;
- Docker only if you already use the testnet Compose stack; it is not required
  for this devnet run;
- **Foundry** (`anvil`, `cast`, `forge`) for the local Ethereum leg. Install it
  from <https://book.getfoundry.sh/getting-started/installation>; the commands
  below assume `anvil`, `cast` and `forge` are on `PATH`;
- two browser profiles (or one normal and one private window) for the
  two-wallet web flows. Each profile keeps its own IndexedDB keyfiles and its
  own MetaMask account.

Use four terminals:

| Terminal | Process |
| --- | --- |
| 1 | Anvil (devnet Ethereum) |
| 2 | SCCP node, also serving the web UI |
| 3 | relay and the two standard miners |
| 4 | optional Chess960 miners and all CLI commands |

Each terminal starts in the repository root. **Shell variables do not cross
terminals**, so repeat the relevant `export` lines in each one; the commands
below repeat the important ones where a new terminal is introduced.

Set the passphrase once. Every keyfile created below uses this same devnet
passphrase, and the stack's published passphrase file is the same value:

```bash
cd /path/to/somecryptochessproject
source .venv/bin/activate

export SCCP_WALLET_PASSPHRASE='sccp-devnet-passphrase'
export SCCP_NETWORK=devnet
export NODE=http://127.0.0.1:8645
export RELAY=http://127.0.0.1:8646
export CHAIN=.devnet-stack/chain
```

`sccp` reads `SCCP_NETWORK` when a command does not pass its own `--network`;
an explicit flag still wins. Check the three programs:

```bash
sccp --version
anvil --version
forge --version
cast --version
```

The ports used here are `8545` (Anvil), `8645` (SCCP node/web UI) and `8646`
(relay). If a testnet Compose stack is already using `8645`/`8646`, stop it or
substitute another node port consistently (`--port`, `SCCP_NODE`, `--node`).

---

## 1. Start devnet Ethereum and deploy `EthHtlc`

The SCCP node never holds an Ethereum key. It only publishes the chain id and
contract address a wallet should use, and the SCCP chain enforces its own HTLC.

### 1.1 Start Anvil

In terminal 1:

```bash
anvil --chain-id 31337 --host 127.0.0.1 --port 8545
```

Anvil prints ten prefunded accounts and their private keys. This runbook uses
account 0 as the **maker** and account 1 as the **taker**:

```bash
export ETH_MAKER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
export ETH_TAKER=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
```

If your Anvil output differs, copy those two addresses into the variables. Both
accounts are unlocked by Anvil, which is what lets the CLI send the ETH leg
without a browser wallet.

### 1.2 Deploy the contract

In terminal 4, one time per checkout:

```bash
cd contracts
forge install foundry-rs/forge-std
```

Then deploy against the Anvil chain. The deploy script checks the chain id
before broadcasting:

```bash
export ANVIL_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
SCCP_NETWORK=devnet forge script script/Deploy.s.sol:Deploy \
  --rpc-url http://127.0.0.1:8545 \
  --private-key "$ANVIL_KEY" \
  --broadcast
```

Copy the printed `EthHtlc:` address into the node's environment. From the
repository root:

```bash
cd ..
export SCCP_ETH_HTLC_CONTRACT=0x...       # the address forge printed
export SCCP_ETH_RPC_URL=http://127.0.0.1:8545
```

The node must be started **after** these exports. `GET /v1/bridge` reports the
contract and the fixed environment (`devnet` → chain `31337`, confirmations
`1`, SCCP margin `24`).

### 1.3 The devnet ETH faucet

Anvil's accounts are already funded with 10,000 ETH each, so no bridge-specific
faucet exists. To top up any address from the Anvil "faucet", use:

```bash
# 1 ETH = 10^18 wei = 0xde0b6b3a7640000
cast rpc anvil_setBalance "$ETH_TAKER" 0xde0b6b3a7640000 \
  --rpc-url http://127.0.0.1:8545

cast balance "$ETH_TAKER" --rpc-url http://127.0.0.1:8545
```

For the web UI, import one of Anvil's private keys into MetaMask and add the
network:

| Field | Value |
| --- | --- |
| Network name | `Anvil devnet` |
| RPC URL | `http://127.0.0.1:8545` |
| Chain ID | `31337` |
| Currency symbol | `ETH` |

The Bridge page also offers to add/switch the network, because `GET /v1/bridge`
publishes the same values.

---

## 2. Create the two SCCP wallets

Alice is the maker, the LP and the node's coinbase address. Bob is the taker and
the second wallet. Both are ordinary devnet keyfiles.

### 2.1 CLI

In terminal 4, from the repository root:

```bash
export ALICE_KEY=alice-devnet.key
export BOB_KEY=bob-devnet.key

ALICE=$(sccp --network devnet wallet new --out "$ALICE_KEY" | tail -n1)
BOB=$(sccp --network devnet wallet new --out "$BOB_KEY" | tail -n1)

echo "Alice $ALICE"
echo "Bob   $BOB"

sccp --network devnet wallet address --key "$ALICE_KEY"
sccp --network devnet wallet address --key "$BOB_KEY"
```

The command prints the address on its last line; with
`SCCP_WALLET_PASSPHRASE` already set it does not prompt. The keyfiles are mode
`0600` and contain only an encrypted seed. Re-running on an existing keyfile is
refused; pass `--force` only when you intend to replace it.

`wallet new` does **not** print the 24-word backup. If this is a wallet you want
to restore, run:

```bash
sccp wallet export-mnemonic --key "$ALICE_KEY"
```

### 2.2 Web UI

In browser profile A:

1. Open `http://127.0.0.1:8645/#/wallet`.
2. Under **Open a wallet**, press **Create a wallet**.
3. Write down the 24-word phrase shown by the page, enter a passphrase twice,
   and confirm.
4. The wallet is now unlocked and its address is shown in the header.

Repeat in browser profile B for Bob. The two profiles are deliberate: a tab
holds one unlocked key, and the bridge/DEX tests need two independent wallets.

To move a browser wallet into the CLI, use **Settings → Export keyfile** in the
wallet, save the JSON, and open it with `sccp` (`wallet address --key ...`). To
move a CLI wallet into the browser, use **Open a keyfile**, then **Choose a file…**
and pick the `.key` file (or paste the JSON).
The passphrase is the one you chose; the seed never leaves the browser
unencrypted.

---



## 3. Build and start the devnet stack

The deterministic devnet stack funds, registers, commits and reveals two miners
before a relay can have a roster. It writes `chain.sqlite` and a `relay.json`
derived from the chain; no node has to be running while it does that.

### 3.1 Write the stack and the bootstrap chain

```bash
python deploy/setup_devnet_stack.py \
  --out .devnet-stack \
  --passphrase "$SCCP_WALLET_PASSPHRASE" \
  --fresh
```

Expected shape:

```text
wrote 2 devnet keyfiles and .devnet-stack/stack.json
built 3 bootstrap blocks: height 3, epoch 3 matchable
relay config for epoch 3 written to .devnet-stack/relay.json
... the node, relay and miner commands for those files
```

`--fresh` deletes only the chain database, never the keys. The chain stops at
height 3: epoch 3 is matchable and epoch 4's availability commit/reveal are
already on chain.

### 3.2 Build the web bundle

The node serves `src/web/dist` when it is started with `--web`. Build it once
(and again after changing the web sources):

```bash
cd src/web
npm ci
npm run build
cd ../..
```

### 3.3 Start the node and the devnet web UI

In terminal 2, re-export the Ethereum environment from section 1.2 (this is a
new shell) and read Alice's address from the keyfile:

```bash
export SCCP_ETH_HTLC_CONTRACT=0x...       # the address forge printed
export SCCP_ETH_RPC_URL=http://127.0.0.1:8545
export CHAIN=.devnet-stack/chain

export ALICE=$(sccp --network devnet wallet address --key alice-devnet.key)

sccp --network devnet --data-dir "$CHAIN" node run \
  --port 8645 --web \
  --mine --miner-address "$ALICE" --no-mine-bootstrap
```

`--mine` is the settlement miner: it includes pending ledger transactions and
settles announced proofs. `--no-mine-bootstrap` is required because the chain was
bootstrapped by the script, not by this node. `--miner-address "$ALICE"` is
important here: it is where this build credits the C960 coinbase when a
Chess960 proof settles, so Alice is the wallet that will seed the DEX.

The output should include:

```text
node on 127.0.0.1:8645 for devnet height 3 (0 txs, 0 proofs pending)
mining proof blocks for sccpt1...
```

Open `http://127.0.0.1:8645/`. The header shows `devnet` and the height. The
hash routes are:

| Page | URL |
| --- | --- |
| Explorer | `http://127.0.0.1:8645/#/` |
| Wallet | `http://127.0.0.1:8645/#/wallet` |
| Faucet | `http://127.0.0.1:8645/#/wallet/faucet` |
| Send | `http://127.0.0.1:8645/#/wallet/send` |
| Play | `http://127.0.0.1:8645/#/play` |
| DEX | `http://127.0.0.1:8645/#/dex` |
| Bridge | `http://127.0.0.1:8645/#/bridge` |

The hamburger menu switches between Explorer, Wallet, Play, DEX and Bridge
without leaving the origin, so the unlocked key stays in the tab.

#### Vite dev server instead of `--web`

If you want hot reload, start the node **without** `--web` and run Vite in
another terminal:

```bash
# terminal 2
sccp --network devnet --data-dir "$CHAIN" node run \
  --port 8645 --mine --miner-address "$ALICE" --no-mine-bootstrap

# terminal 5
cd src/web
SCCP_NODE=http://127.0.0.1:8645 \
VITE_SCCP_RELAY=http://127.0.0.1:8646 \
npm run dev
```

Then open `http://127.0.0.1:5173/#/wallet`, `#/dex`, `#/bridge` and `#/play`.
In this mode the relay must also allow the Vite origin:

```bash
--origin http://127.0.0.1:5173
```

---

### 3.4 Start the relay

In terminal 3 (new shell):

```bash
export NODE=http://127.0.0.1:8645
export RELAY=http://127.0.0.1:8646

sccp --network devnet matchmaker run \
  --config .devnet-stack/relay.json \
  --port 8646 \
  --origin http://127.0.0.1:8645 \
  --node "$NODE"
```

If you use Vite, add a second `--origin http://127.0.0.1:5173`.

The relay starts on epoch 3 and follows the node from there. A round opens every
60 seconds (`round_seconds` in the generated config); the relay pairs within a
variant, so a standard round and a Chess960 round are separate matches.

Check it:

```bash
curl -s "$RELAY/v1/status"
curl -s "$RELAY/v1/matches" | python -m json.tool
```

### 3.5 Start the two standard miners

In terminal 3 (or two more terminals, one miner each):

```bash
sccp --network devnet --passphrase-file .devnet-stack/passphrase miner run \
  --key .devnet-stack/keys/miner-1.key \
  --relay "$RELAY" --node "$NODE" \
  --nonce-secret-file .devnet-stack/keys/miner-1.nonce \
  --engine-profile mock --resign-after-plies 60

sccp --network devnet --passphrase-file .devnet-stack/passphrase miner run \
  --key .devnet-stack/keys/miner-2.key \
  --relay "$RELAY" --node "$NODE" \
  --nonce-secret-file .devnet-stack/keys/miner-2.nonce \
  --engine-profile mock --resign-after-plies 60
```

`--resign-after-plies 60` is a devnet convenience: two mock engines can shuffle
for hundreds of plies and draw, and a draw has no proof. A resignation is a
decisive result and settles normally.

The miners keep availability committed and revealed each epoch. The node prints
`mined ...` as blocks appear, and the relay prints `opened round ...` every
minute.

### 3.6 Health checks

```bash
curl -s "$NODE/v1/status" | python -m json.tool
curl -s "$NODE/v1/bridge" | python -m json.tool
curl -s "$NODE/v1/pools" | python -m json.tool
```

`/v1/status` must say `"network": "devnet"`. `/v1/bridge` must say
`"configured": true` and show the Anvil chain id and the contract address you
exported. `/v1/pools` is empty until the first pool is created.

---


## 4. Fund the wallets, then make C960

### 4.1 The SCCP faucet

The devnet genesis funds one public faucet key with 1,000,000 SCCP. The HTTP
faucet is rate-limited; the operator CLI spends the same key without the HTTP
cap. Use the CLI for a repeatable test:

```bash
sccp --network devnet faucet send --node "$NODE" --to "$ALICE" --amount 200
sccp --network devnet faucet send --node "$NODE" --to "$BOB"   --amount 200
```

In the web UI, in each browser profile:

1. Open `#/wallet/faucet`.
2. Press **Request testnet funds**.
3. Wait for the grant transaction to be included; the balance refreshes on the
   node's block.

The HTTP faucet allows one grant per address per hour and five per client per
hour. The CLI path exists so a test that funds several addresses does not wait.

The grant is a transaction in the mempool, not a balance. On this stack the node
mines it in the next heartbeat block. Check:

```bash
sccp --network devnet wallet balance --key "$ALICE_KEY" --node "$NODE"
sccp --network devnet wallet balance --key "$BOB_KEY"   --node "$NODE"

curl -s "$NODE/v1/mempool" | python -m json.tool
```

`wallet balance` now prints native SCCP and every non-native asset the account
holds, including immature balances.

### 4.2 Why the faucet does not mint C960

C960 has no issuer and no faucet allocation. It is created only when a block
settles a proof whose certificate names the `chess960` variant; the block's
`variant_rewards` coinbase entry is consensus-checked against the proof. A
faucet transfer cannot create it.

So the phrase "both tokens on a single account" means:

- **SCCP** comes from the faucet;
- **C960** comes from a settled Chess960 proof (or from a transfer/swap after
  someone else has some).

In this runbook Alice is the node's `--miner-address`, so Alice receives the C960
coinbase when the Chess960 proof settles. That gives one account both tokens
without a cross-wallet transfer.

### 4.3 Seed C960 with two Chess960 miners

The deterministic stack's two miners are standard-chess seats. Start two more
devnet seats registered for Chess960. They do not have to be strong: mock engines
plus a resignation policy produce a decisive game.

Create and fund the two Chess960 wallets:

```bash
export C960_A_KEY=c960-a.key
export C960_B_KEY=c960-b.key

C960_A=$(sccp --network devnet wallet new --out "$C960_A_KEY" | tail -n1)
C960_B=$(sccp --network devnet wallet new --out "$C960_B_KEY" | tail -n1)

sccp --network devnet faucet send --node "$NODE" --to "$C960_A" --amount 50
sccp --network devnet faucet send --node "$NODE" --to "$C960_B" --amount 50

# The registration reads the local chain, so let both grants settle first.
sccp --network devnet wallet balance --key "$C960_A_KEY" --node "$NODE"
sccp --network devnet wallet balance --key "$C960_B_KEY" --node "$NODE"
```

Wait until each shows 50 SCCP spendable, then register both for the variant. The engine fingerprint must be the one the miner
will later claim; get the mock fingerprint once:

```bash
MOCK_ENGINE=$(python -c \
  'from sccp.chess.engines import open_engine; print(open_engine("mock", seed=0).fingerprint)')
echo "$MOCK_ENGINE"
```

Register against the local bootstrap chain, then submit to the node:

```bash
sccp --network devnet --data-dir "$CHAIN" miner register \
  --key "$C960_A_KEY" --endpoint tcp://127.0.0.1:7101 \
  --engine "$MOCK_ENGINE" --bond 0 --variant chess960 --node "$NODE"

sccp --network devnet --data-dir "$CHAIN" miner register \
  --key "$C960_B_KEY" --endpoint tcp://127.0.0.1:7102 \
  --engine "$MOCK_ENGINE" --bond 0 --variant chess960 --node "$NODE"
```

Create the two availability secrets (any 32-byte hex value; keep the files):

```bash
openssl rand -hex 32 > c960-a.nonce
openssl rand -hex 32 > c960-b.nonce
chmod 600 c960-a.nonce c960-b.nonce
```

Start the two Chess960 miners in terminal 4. `--nonce-secret-file` makes each
miner commit and reveal its own availability every epoch; `--variant chess960`
makes it open Stockfish with `UCI_Chess960` (the mock engine ignores that option
but the certificate and relay still use the variant).

```bash
sccp --network devnet --passphrase-file .devnet-stack/passphrase miner run \
  --key "$C960_A_KEY" --variant chess960 --engine-profile mock \
  --relay "$RELAY" --node "$NODE" \
  --nonce-secret-file c960-a.nonce \
  --resign-after-plies 60

sccp --network devnet --passphrase-file .devnet-stack/passphrase miner run \
  --key "$C960_B_KEY" --variant chess960 --engine-profile mock \
  --relay "$RELAY" --node "$NODE" \
  --nonce-secret-file c960-b.nonce \
  --resign-after-plies 60
```

Availability is committed two epochs ahead and revealed one epoch ahead. On
devnet an epoch is one block, so give the pipeline a few blocks. The relay's
epoch follower will move onto the first epoch whose roster contains the two
Chess960 seats and print `relay moved to epoch ...`.

Watch for the match and the proof:

```bash
curl -s "$RELAY/v1/matches" | python -m json.tool
curl -s "$NODE/v1/games?limit=5" | python -m json.tool
curl -s "$NODE/v1/status" | python -m json.tool
```

A match certifies `"variant": "chess960"` and a start position selected from the
beacon. The winner's miner announces a proof; the node's `--mine` includes it in
the next block. `/v1/status` then shows a non-zero entry in `minted_assets` keyed
by the C960 asset id.

> **The reward is the block miner's.** In this build the coinbase pays
> `header.miner`, which is the node's `--miner-address` (Alice), not
> necessarily the game winner. That is why Alice's address was used for
> `--miner-address` above.

---


### 4.4 Wait for maturity and read both balances

The C960 credit is a coinbase output, so it matures like native coinbase (20
blocks on devnet). `wallet balance` distinguishes spendable from immature:

```bash
sccp --network devnet wallet balance --key "$ALICE_KEY" --node "$NODE"
```

Expected shape after the proof settles:

```text
sccpt1... 250.00000000 (spendable ..., bonded ..., immature ...)
  C960 spendable 0.00000000, immature 50.00000000
```

The C960 subsidy is 50 C960 (the variant's subsidy), not 50 SCCP; read the exact
number from the output. While it is immature, keep blocks coming. One simple
way is a short faucet loop; each grant is one transaction and therefore one
heartbeat block:

```bash
for i in $(seq 1 24); do
  sccp --network devnet faucet send --node "$NODE" --to "$BOB" --amount 1
  sleep 2
done
```

Then repeat `wallet balance` until the `C960 spendable` line is positive. If the
proof has not settled yet, keep the relay and both Chess960 miners running and
check `GET /v1/games` for a `chess960` match; rounds open every 60 seconds.

At this point Alice has **both** tokens in one account:

```bash
sccp --network devnet wallet balance --key "$ALICE_KEY" --node "$NODE"
```

### 4.5 Exercise the Chess960 web flow

The Play page is the same protocol as `sccp miner run`, with the wallet as the
seat key. This is the UI check for the Chess960 changes.

What this path is checking:

- a non-standard registration carries `variant: "chess960"` and a miner opens
  its engine with `UCI_Chess960=true`;
- the relay matches within a variant and derives one of the 960 start positions
  from the epoch beacon and match id, committing its Shredder-FEN in the signed
  certificate;
- a Chess960 PGN is valid only with `Rules "chess960"`, `SetUp`, `Variant` and
  the certified start FEN; a standard PGN cannot be replayed as Chess960;
- the settled proof mints C960 instead of SCCP, and the token appears in the
  account, explorer, wallet, DEX picker and transfer asset picker.

In a fresh browser profile (or Alice's profile if she has not registered yet):

1. Open `#/play`.
2. In the token/variant picker choose **C960 / chess960**. The helper text says
   winning Chess960 proofs mint C960 while the bond and fees stay in SCCP.
3. Press **Faucet** if the account has no SCCP, then **Register**.
4. Press **Commit availability** when the page reaches that stage. The page
   stores the secret in `sessionStorage`; keep the tab open.
5. When the chain reaches the reveal window, press **Reveal**.
6. Wait for the page to show the relay match. The certified start position is
   the beacon-selected Chess960 FEN; the board and castling follow Chess960
   rules.
7. Join the match and play to a decisive result, or resign after the network's
   minimum plies. A draw, a flag and a too-short game produce no proof.
8. Press **Settle** when the page offers it. The proof is announced to the node
   and the node mines it.

The UI check is not only the board:

- **Explorer → Games**: the game row names `Chess960`, and the game detail shows
  the variant and start FEN.
- **Wallet → Overview**: C960 appears beside SCCP, with immature and spendable
  amounts separated.
- **DEX**: the token picker includes C960.
- **Play**: the proof panel names the variant and the C960 reward token.

If your web seat is the only Chess960 seat in the current roster, it sits out
until another Chess960 seat is matchable. The two CLI miners from section 4.3
are the reliable opponents for this test.

---


## 5. Set up the maker's bridge position

An order is a maker's signed promise to sell SCCP for ETH. It binds the maker's
SCCP address, ETH address, amount, price, minimum SCCP timelock and expiry into
one signature. Nothing is escrowed when the order is published.

Use:

- 20 SCCP for 0.05 ETH (`50_000_000_000_000_000` wei);
- a 24-hour order;
- Alice's SCCP key and Anvil account 0 as `ETH_MAKER`.

### 5.1 CLI: publish the order

```bash
export PRICE_WEI=50000000000000000

ORDER_JSON=$(sccp --json bridge sell \
  --key "$ALICE_KEY" --eth-address "$ETH_MAKER" \
  --amount 20 --price-wei "$PRICE_WEI" --expiry-hours 24 --node "$NODE")
ORDER_ID=$(printf '%s' "$ORDER_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["order_id"])')
echo "$ORDER_ID"
```

The human form prints `published order ...`. Check the book:

```bash
sccp bridge orders --node "$NODE"
sccp bridge order --order-id "$ORDER_ID" --node "$NODE"
```

`bridge order` shows `take: none` until the taker locks ETH and publishes its
take.

### 5.2 Web UI: publish the order

In browser profile A:

1. Open `#/bridge`.
2. Select the **Sell SCCP** tab.
3. Press **Connect Ethereum** and choose the Anvil network and account 0. If the
   network is missing, the page asks to add it with chain id `31337` and RPC
   `http://127.0.0.1:8545`.
4. Enter **SCCP to sell** `20`, **ETH to receive** `0.05`, and an expiry of
   `24` hours.
5. Press **Publish sell order**. The order is signed with the SCCP wallet key;
   the ETH address is bound into that signature.
6. The order appears under **My orders** on the same tab.

The status line should say `Anvil devnet ready`. If it says `ETH not
configured`, the node was started without `SCCP_ETH_HTLC_CONTRACT`; fix section
1.2 and restart the node.

---

## 6. Take the order and move ETH → SCCP

The taker locks ETH first. The maker verifies that lock on Ethereum, then locks
SCCP under the same SHA-256 hashlock. The taker claims SCCP and reveals the
preimage; the maker claims ETH with the now-public preimage.

The second leg must expire *before* the first by `sccp_margin` blocks (24 on
devnet). The CLI computes a safe SCCP height automatically; the web page does
the same.

### 6.1 CLI: generate the secret and lock ETH

```bash
SECRET=$(sccp --json bridge secret)
PREIMAGE=$(printf '%s' "$SECRET" | python -c 'import json,sys; print(json.load(sys.stdin)["preimage"])')
HASHLOCK=$(printf '%s' "$SECRET" | python -c 'import json,sys; print(json.load(sys.stdin)["hashlock"])')
ETH_TIMELOCK=$(( $(date +%s) + 21600 ))

echo "preimage $PREIMAGE"
echo "hashlock $HASHLOCK"
echo "eth lock $ETH_TIMELOCK"
```

Lock 0.05 ETH from Anvil account 1. The `--from` account must be unlocked by the
RPC; Anvil unlocks all ten default accounts:

```bash
LOCK_JSON=$(sccp --json bridge eth-lock \
  --from "$ETH_TAKER" \
  --hashlock "$HASHLOCK" \
  --timelock "$ETH_TIMELOCK" \
  --beneficiary "$ETH_MAKER" \
  --value-wei "$PRICE_WEI" \
  --node "$NODE")
ETH_TX=$(printf '%s' "$LOCK_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["tx_hash"])')
ETH_LOCK=$(printf '%s' "$LOCK_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["lock_id"])')
```

Each `eth-lock` creates a new lock; run it once and keep these values.

The contract-side check is independent of the SCCP node:

```bash
sccp bridge eth-lock-info --lock-id "$ETH_LOCK" --node "$NODE"
```

It must show the taker as depositor, Alice's Anvil address as beneficiary, the
same hashlock and the 0.05 ETH amount, with `claimed=false refunded=false`.

---


### 6.2 CLI: publish the take

```bash
sccp --json bridge take \
  --key "$BOB_KEY" \
  --order-id "$ORDER_ID" \
  --eth-address "$ETH_TAKER" \
  --hashlock "$HASHLOCK" \
  --eth-timelock "$ETH_TIMELOCK" \
  --eth-tx-hash "$ETH_TX" \
  --node "$NODE"
```

The node enforces the SCCP/ETH timelock rule at admission. Without
`--sccp-timelock-height`, the CLI chooses the largest safe height below the ETH
lock, respecting the order's minimum. The output prints the height it chose.

Now the maker can read the take:

```bash
sccp bridge order --order-id "$ORDER_ID" --node "$NODE"
```

### 6.3 CLI: maker locks SCCP

```bash
TAKE_HEIGHT=$(curl -s "$NODE/v1/orders/$ORDER_ID" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["take"]["sccp_timelock_height"])')

LOCK_SCCP=$(sccp --json bridge lock \
  --key "$ALICE_KEY" \
  --hashlock "$HASHLOCK" \
  --timelock-height "$TAKE_HEIGHT" \
  --beneficiary "$BOB" \
  --amount 20 \
  --node "$NODE")
SCCP_HTLC=$(printf '%s' "$LOCK_SCCP" | python -c 'import json,sys; print(json.load(sys.stdin)["htlc_id"])')
```

Wait for the lock to be mined and for the taker to see enough confirmations:

```bash
sccp bridge htlc --htlc-id "$SCCP_HTLC" --node "$NODE"
sccp bridge htlcs --hashlock "$HASHLOCK" --node "$NODE"
```

The taker must wait for `reorg_depth + 1` confirmations (11 on devnet) before
claiming. The web page shows the count explicitly; the CLI check is
`GET /v1/status` and the lock's `created_at_height`.

### 6.4 CLI: taker claims SCCP

```bash
sccp --json bridge claim \
  --key "$BOB_KEY" \
  --htlc-id "$SCCP_HTLC" \
  --preimage "$PREIMAGE" \
  --node "$NODE"
```

Bob's SCCP balance increases by 20; the claim's fee is paid in SCCP, and a native
claim may pay that fee out of the locked amount if the account has no SCCP. The
HTLC now records the preimage on chain:

```bash
sccp bridge htlc --htlc-id "$SCCP_HTLC" --node "$NODE"
```

Wait for the claim to reach `reorg_depth` confirmations, then the maker can use
the public preimage.

---


### 6.5 CLI: maker claims ETH

```bash
sccp --json bridge eth-claim \
  --from "$ETH_MAKER" \
  --lock-id "$ETH_LOCK" \
  --preimage "$PREIMAGE" \
  --node "$NODE"
```

Verify both sides:

```bash
sccp bridge eth-lock-info --lock-id "$ETH_LOCK" --node "$NODE"
curl -s "$NODE/v1/accounts/$ALICE" | python -m json.tool
curl -s "$NODE/v1/accounts/$BOB"   | python -m json.tool
```

The ETH lock reports `claimed=true`; Alice's SCCP balance falls by 20 plus the
lock fee, and Bob's rises by 20.

### 6.6 Refund paths

If the maker never locks SCCP, the taker waits until `ETH_TIMELOCK` and runs:

```bash
sccp --json bridge eth-refund --from "$ETH_TAKER" --lock-id "$ETH_LOCK" --node "$NODE"
```

If the taker never claims SCCP, the maker waits until the SCCP height in the
take and runs:

```bash
sccp --json bridge refund --key "$ALICE_KEY" --htlc-id "$SCCP_HTLC" --node "$NODE"
```

The contract and the SCCP chain are independent: each refund is valid only on
its own timelock, and the other leg cannot be claimed once its counterparty has
taken the refund path.

### 6.7 Web UI: the same swap with two browser profiles

Use profile A as the maker and profile B as the taker. Each profile has its own
SCCP wallet and its own MetaMask account.

**Maker (profile A)**

1. Open `#/bridge → Sell SCCP` and publish the order as in section 5.2.
2. After the taker acts, the order under **My orders** shows the taker, the ETH
   transaction and both timelocks.
3. Press **Verify ETH lock and lock SCCP**. The page reads the lock through the
   Ethereum provider, checks the depositor, hashlock, timelock, beneficiary and
   value, and then signs the SCCP `htlc_lock`.
4. Wait for **SCCP status: claimed**. The page shows when the claim has reached
   `reorg_depth` confirmations.
5. Press **Claim ETH with preimage**. The preimage comes from the on-chain SCCP
   record, so no secret is typed or copied.

**Taker (profile B)**

1. Open `#/bridge → Buy SCCP`.
2. Select the 20 SCCP order. The page shows the ETH price, the maker, the expiry
   and the order id.
3. Press **Connect Ethereum** and choose Anvil account 1.
4. Keep the 6-hour ETH lock (or choose 12/24 hours), then press **Lock ETH and
   take order**. MetaMask signs the `lock` call. The page stores the preimage in
   the browser's local swap store before publishing the take.
5. Wait for the maker's SCCP lock. When it has the required confirmations, press
   **Claim SCCP and reveal secret**.
6. If the maker stalls, wait for the ETH timelock and press **Refund expired
   ETH**.

The **Activity** tab on either profile lists the SCCP hash locks involving that
wallet, their timelocks and the revealed preimage after the claim.

---



## 7. Set up the devnet DEX LP

The DEX is on-chain state, not a browser order book. The first deposit sets the
price, later deposits follow the current reserve ratio, and every swap pays a
0.30% fee to the LP shares. Fees are paid in SCCP even when the input token is
C960.

Use a **50 SCCP / 50 C960** initial pool. That is one C960 proof subsidy at a
1:1 starting price, and it leaves Alice SCCP for the transaction fee. If Alice
has more C960, she can deposit more; use no more than `wallet balance` reports as
spendable C960.

### 7.1 CLI: create the pool

```bash
POOL_JSON=$(sccp --json dex create-pool \
  --key "$ALICE_KEY" \
  --asset-a SCCP --amount-a 50 \
  --asset-b C960 --amount-b 50 \
  --node "$NODE")
POOL_ID=$(printf '%s' "$POOL_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["pool_id"])')
echo "$POOL_ID"
```

The builder sorts the pair by asset id, so the pool is stored as SCCP/C960 no
matter which side you typed. The command prints the pool id and the transaction
id.

Inspect it:

```bash
sccp dex pools --node "$NODE"
sccp dex pool --pool "$POOL_ID" --node "$NODE"
```

`dex pool` prints the reserves in base units, the total LP shares and every LP
holder. Alice owns all initial shares.

### 7.2 CLI: add or remove liquidity

To add a proportional deposit (only if Alice has more C960):

```bash
sccp --json dex add-liquidity \
  --key "$ALICE_KEY" --pool "$POOL_ID" \
  --amount-a 5 --amount-b 5 --node "$NODE"
```

To redeem a position:

```bash
sccp --json dex remove-liquidity \
  --key "$ALICE_KEY" --pool "$POOL_ID" --shares 1000000000 --node "$NODE"
```

Shares are integer units, so read the exact number from `dex pool`; the example
is only a whole share count, not a promise about the current pool. Redeeming all
shares deletes the pool once both reserves are paid.

---


### 7.3 CLI: Bob swaps SCCP for C960

Bob needs SCCP from the faucet (section 4.1) and no C960 initially. Quote first:

```bash
sccp --json dex quote \
  --pool "$POOL_ID" \
  --asset-in SCCP --amount-in 10 --slippage-bps 50 \
  --node "$NODE"
```

The quote is a preview computed from the node's reserves. Then sign a swap with
a minimum output derived from that quote:

```bash
sccp --json dex swap \
  --key "$BOB_KEY" \
  --pool "$POOL_ID" \
  --asset-in SCCP \
  --amount-in 10 \
  --slippage-bps 50 \
  --node "$NODE"
```

You can pass `--min-out <whole coins>` instead, but the minimum is part of the
signed transaction: if the pool moves beyond it before the block, the chain
rejects the swap instead of filling it at a worse price.

Check Bob's balances:

```bash
sccp --network devnet wallet balance --key "$BOB_KEY" --node "$NODE"
```

Bob's SCCP falls by 10 plus the fee; a C960 line appears with the swap output.
The pool's SCCP reserve rises and its C960 reserve falls by the same output.

### 7.4 Web UI: create the pool and swap

**LP (profile A)**

1. Open `#/dex`.
2. Select the **Create pool** tab.
3. Token A: `SCCP`; Token B: `C960`.
4. Enter `50` SCCP and `50` C960.
5. Review the pool screen: pair, initial reserves, LP shares, initial price.
6. Press **Review pool**, then **Sign and send**.

The **Liquidity** tab now shows the pool, Alice's share percentage, her
underlying amounts and the current price. **Add** and **Remove** sign the same
kinds of transaction as the CLI.

**Swapper (profile B)**

1. Open `#/dex → Swap`.
2. Choose the SCCP/C960 pool.
3. Choose direction `SCCP → C960`.
4. Enter `10`.
5. Pick a slippage tolerance (0.5% is the default) and review the estimated and
   minimum receive.
6. Press **Review swap**, then **Sign and send**.

The review screen shows the pool, pay amount, estimated receive, minimum
receive, slippage, SCCP fee and nonce. The minimum output is signed; a pool that
moves past it rejects the transaction.

The swap is one row in `#/wallet/history`, marked `swap`: the exact input, the
asset the pool paid out and the minimum that was signed, as in
`10 SCCP → ≥… C960`, with the pool id and the txid each behind a copy button. The
received side is a floor and not the fill, because a block records the bound the
chain enforced rather than what the pool actually paid.

---


## 8. Send SCCP and C960 wallet to wallet

A transfer is signed locally and submitted to a node. SCCP and C960 share one
account and one nonce, but the amount and asset are separate fields. Every
transfer pays its fee in SCCP.

### 8.1 CLI

Send SCCP from Bob to Alice:

```bash
sccp --json wallet send \
  --key "$BOB_KEY" --to "$ALICE" --amount 5 --node "$NODE"
```

Wait for that transfer to be included (the next heartbeat block), then send
C960. Sending both at once from the same address would reuse the same nonce:

```bash
sleep 2
sccp --json wallet send \
  --key "$BOB_KEY" --to "$ALICE" --amount 3 --asset C960 --node "$NODE"
```

Check both sides:

```bash
sccp --network devnet wallet balance --key "$ALICE_KEY" --node "$NODE"
sccp --network devnet wallet balance --key "$BOB_KEY"   --node "$NODE"
```

The JSON form of `wallet balance` includes `assets` and `immature_assets` maps
keyed by the 64-hex C960 asset id; the human form prints the `C960` symbol.

### 8.2 Web UI

In browser profile B:

1. Open `#/wallet/send`.
2. Choose the **SCCP** token, paste Alice's address, enter `5`, leave the
   network minimum fee, and press **Review transfer**.
3. Press **Sign and send**.
4. Wait for the transaction to appear in History as `included`.
5. Repeat with the **C960** token and `3`. The token picker is the segmented
   control above the recipient field; the review screen names the asset.
6. Optionally give Alice a nickname while sending; it is stored in this
   browser's local contacts, not on chain.

In profile A, the wallet overview shows the incoming amounts after the blocks
land. The History tab scans blocks for both assets; the Overview tab shows
pending rows until the committed nonce passes them.

---


## 9. Verify the whole run

### 9.1 A final command-line checklist

```bash
# Chain and bridge environment
curl -s "$NODE/v1/status" | python -m json.tool
curl -s "$NODE/v1/bridge" | python -m json.tool

# Bridge book and both legs
sccp bridge orders --node "$NODE"
sccp bridge order --order-id "$ORDER_ID" --node "$NODE"
sccp bridge htlcs --hashlock "$HASHLOCK" --node "$NODE"
sccp bridge eth-lock-info --lock-id "$ETH_LOCK" --node "$NODE"

# DEX state and both wallets
sccp dex pools --node "$NODE"
sccp dex pool --pool "$POOL_ID" --node "$NODE"
sccp --network devnet wallet balance --key "$ALICE_KEY" --node "$NODE"
sccp --network devnet wallet balance --key "$BOB_KEY"   --node "$NODE"

# Variant settlement
curl -s "$NODE/v1/status" | python -m json.tool
curl -s "$NODE/v1/games?limit=5" | python -m json.tool
```

What a passing run shows:

| Check | Pass condition |
| --- | --- |
| `/v1/status` | `network: devnet`, height advancing, `minted_assets` contains C960 after the proof |
| `/v1/bridge` | `configured: true`, chain id `31337`, the deployed contract, confirmations `1`, margin `24` |
| `bridge order` | the take names Bob's SCCP address, the hashlock, both timelocks and the ETH tx |
| `bridge htlcs` | one lock created by Alice for Bob, then `status: claimed` with a preimage |
| `eth-lock-info` | the taker as depositor, Alice's Anvil address as beneficiary, `claimed: true` |
| `dex pool` | SCCP/C960 reserves and Alice's LP shares |
| `wallet balance` | Alice has SCCP and C960; Bob has the swap output and the remaining SCCP |
| `/v1/games` | at least one `Chess960` game with a settled proof |

### 9.2 The web UI checklist

| Feature | Where | What to see |
| --- | --- | --- |
| Wallet creation/unlock | `#/wallet` | address, network, passphrase prompt; refresh keeps the encrypted keyfile in IndexedDB |
| SCCP faucet | `#/wallet/faucet` | grant txid and a balance that settles in a later block |
| SCCP/C960 balances | `#/wallet` | separate token rows, immature amounts called out |
| SCCP/C960 send | `#/wallet/send` | token picker, review screen, accepted txid, History row |
| Bridge maker | `#/bridge → Sell SCCP` | published order under **My orders**; `Anvil devnet ready` |
| Bridge taker | `#/bridge → Buy SCCP` | ETH lock and take; SCCP lock appears; claim button |
| Bridge maker claim | `#/bridge → Sell SCCP → My orders` | **Verify ETH lock and lock SCCP**, then **Claim ETH with preimage** |
| DEX pool | `#/dex → Create pool` | SCCP/C960 pool, LP shares, review and accepted tx |
| DEX LP | `#/dex → Liquidity` | share percentage, underlying amounts, add/remove |
| DEX swap | `#/dex → Swap` | quote, minimum, slippage, accepted tx |
| Chess960 play | `#/play` | C960 variant picker, certified FEN, moves, result, proof |
| Explorer | `#/games`, `#/blocks`, `#/accounts/<address>` | variant labels, C960 balances, block proofs by variant |

### 9.3 Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no built explorer found` | node `--web` with no `src/web/dist` | run `npm run build` in `src/web`, or set `SCCP_WEB_DIST` |
| `ETH not configured` | node started without `SCCP_ETH_HTLC_CONTRACT` | export it and restart the node |
| `the RPC ... is on chain ...` | wrong `SCCP_ETH_RPC_URL` or wrong Anvil chain | use `--chain-id 31337` and the devnet RPC |
| `eth_sendTransaction` fails | the `--from` account is not unlocked by the RPC | use an Anvil default account, or the web Bridge page with MetaMask |
| `transaction reverted` on `eth-lock` | timelock in the past, zero value or zero beneficiary | use a future `--timelock` and a non-zero value/address |
| `SCCP timelock ... not before the ETH timelock` | ETH lock is too short for the 24-block margin | choose a longer ETH lock; the web page offers 6/12/24 hours |
| `the htlc cannot be refunded before height ...` | SCCP timelock has not passed | wait for the height, then retry |
| `the preimage does not open this htlc` | wrong secret or wrong lock | use the preimage from the same swap; the claimed HTLC shows it |
| `pool ... does not trade ...` | wrong asset id/name for that pool | read `dex pool` and use `SCCP` or `C960` |
| `the deposit is too small to mint a share` | rounding at tiny amounts | deposit larger amounts on both sides |
| `C960` line is missing | no Chess960 proof has settled, or the reward is immature | run the section 4.3 pair; mine 20 more blocks |
| faucet `429` | HTTP one-grant-per-address/hour | use `sccp faucet send` or wait |
| `already has a pending transaction with nonce N` | two sends from one address before a block | send one, wait for inclusion, then send the next |
| `Address already in use` on 8645/8646 | a Compose stack or stale node is running | stop it or use different ports consistently |
| wallet page says another network | browser wallet's node is not devnet | open the bundle from the devnet node, or set `SCCP_NODE` for Vite |

---


## 10. Quick reference

### 10.1 CLI-only processes

```bash
# Ethereum
anvil --chain-id 31337
cd contracts
SCCP_NETWORK=devnet forge script script/Deploy.s.sol:Deploy \
  --rpc-url http://127.0.0.1:8545 --private-key "$ANVIL_KEY" --broadcast
cd ..

# SCCP chain and web bundle
python deploy/setup_devnet_stack.py --out .devnet-stack \
  --passphrase "$SCCP_WALLET_PASSPHRASE" --fresh
sccp --network devnet --data-dir "$CHAIN" node run \
  --port 8645 --web --mine --miner-address "$ALICE" --no-mine-bootstrap
sccp --network devnet matchmaker run \
  --config .devnet-stack/relay.json --port 8646 \
  --origin http://127.0.0.1:8645 --node "$NODE"

# Two standard miners
sccp --network devnet --passphrase-file .devnet-stack/passphrase miner run \
  --key .devnet-stack/keys/miner-1.key --relay "$RELAY" --node "$NODE" \
  --nonce-secret-file .devnet-stack/keys/miner-1.nonce \
  --engine-profile mock --resign-after-plies 60
# repeat with miner-2
```

### 10.2 User actions, CLI and web

| Action | CLI | Web |
| --- | --- | --- |
| Create wallet | `sccp --network devnet wallet new --out alice.key` | `#/wallet → Create a wallet` |
| SCCP faucet | `sccp --network devnet faucet send --to ...` | `#/wallet/faucet → Request testnet funds` |
| Bridge order | `sccp bridge sell ...` | `#/bridge → Sell SCCP → Publish sell order` |
| Lock ETH | `sccp bridge eth-lock ...` | `#/bridge → Buy SCCP → Lock ETH and take order` |
| Take order | `sccp bridge take ...` | same Buy screen (the lock publishes the take) |
| Lock SCCP | `sccp bridge lock ...` | `#/bridge → Sell SCCP → My orders → Verify ETH lock and lock SCCP` |
| Claim SCCP | `sccp bridge claim ...` | `#/bridge → Buy SCCP → Claim SCCP and reveal secret` |
| Claim ETH | `sccp bridge eth-claim ...` | `#/bridge → Sell SCCP → My orders → Claim ETH with preimage` |
| Refund ETH | `sccp bridge eth-refund ...` | `#/bridge → Buy SCCP → Refund expired ETH` |
| Refund SCCP | `sccp bridge refund ...` | `#/bridge → Sell SCCP → My orders → Refund SCCP` |
| Create pool | `sccp dex create-pool ...` | `#/dex → Create pool` |
| Add/remove LP | `sccp dex add-liquidity` / `remove-liquidity` | `#/dex → Liquidity` |
| Swap | `sccp dex swap ...` | `#/dex → Swap` |
| Send SCCP | `sccp wallet send --amount N --to ...` | `#/wallet/send`, choose SCCP |
| Send C960 | `sccp wallet send --asset C960 --amount N --to ...` | `#/wallet/send`, choose C960 |
| Play Chess960 | `sccp miner run --variant chess960 ...` | `#/play`, choose C960 |
| Inspect | `sccp bridge ...`, `sccp dex ...`, `curl` | `#/bridge`, `#/dex`, explorer |

### 10.3 The properties this run proves

- an ETH lock and an SCCP lock are opened under one SHA-256 hashlock;
- the SCCP leg is not claimable by the taker until it is final, and the maker
  cannot lock SCCP before verifying the ETH leg;
- the SCCP timelock is strictly before the ETH timelock by the node-enforced
  margin, so a last-moment claim cannot strand the maker;
- the preimage is public only after the SCCP claim, and that is what lets the
  maker claim ETH;
- pool creation, LP shares, swaps and both tokens are consensus state, not
  browser state;
- Chess960 proofs settle a C960 coinbase, and C960 transfers pay their fee in
  SCCP.

---

## Related documentation

- [`RUN_A_DEVNET.md`](../setup/RUN_A_DEVNET.md) — the base devnet, faucet,
  wallets, transfers and miner pipeline.
- [`BIG_PICTURE.md`](../design/BIG_PICTURE.md) — the bridge/DEX/variant design
  and the remaining work.
- [`PROTOCOL.md`](../design/PROTOCOL.md) — the transaction, state, HTLC and pool
  rules.
- [`contracts/README.md`](../../contracts/README.md) — the `EthHtlc` interface
  and its deployment.

All pages, by task: [`docs/README.md`](../README.md).

