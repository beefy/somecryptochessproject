# The big picture: a wallet, a bridge and a DEX

> **Status.** Forward-looking design. Nothing in this document is implemented.
> It plans three additions on top of the protocol v1 ledger described in
> [`PROTOCOL.md`](PROTOCOL.md) -- a browser wallet, a trustless ETH<->SCCP
> bridge and a native DEX -- and the variant-token model they exist to serve.
> Consensus-visible work lands as protocol **v2**; the wallet and the Ethereum
> side of the bridge need no fork. The prioritized work on the chain as it
> stands today stays in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## 1. The goal

An Ethereum holder should be able to turn ETH into SCCP -- the native token of
the standard-chess chain, this repository -- without handing custody to anyone,
and a miner should be able to turn mined SCCP into ETH the same way. Every other
token on the network is a **chess variant** token (Chess960, Crazyhouse, ...),
minted only by settled games of that variant, so the supply of each asset tracks
a corpus of computer chess games rather than a promise. Every key, signature and
authorisation on the SCCP side is hybrid Ed25519 + ML-DSA-65; there is no
classical fallback, and assets inherit that rule instead of introducing a second
cryptographic scheme.

That gives four deliverables, in dependency order:

| Deliverable | What it does | Consensus change | Release |
| --- | --- | --- | --- |
| Web wallet | creates, holds and signs in the browser | none | 0.20.0 |
| Multi-asset ledger + variants | variant tokens mined by variant games | protocol v2 | 0.21.0 |
| Native DEX | pools, LP positions and swaps between on-chain assets | protocol v2 | 0.22.0 |
| ETH<->SCCP bridge | HTLC atomic swaps; no custodian, no wrapped peg | protocol v2 + one Ethereum contract | 0.23.0 |

Optionally, 0.24.0 adds a `wETH` asset so that ETH itself can be traded in the
native DEX; that is a bonded federation and is explicitly out of the critical
path (section 7.7).

## 2. Where the project stands

What already exists and will be reused rather than replaced:

| Piece | Where | What the new work needs from it |
| --- | --- | --- |
| Hybrid accounts | `crypto/pq.py`, `crypto/address.py` | every new asset, pool and bridge claim is owned by one of these |
| Signed transactions | `chain/tx.py`, `ALLOWED` payload set | the extension point for the new transaction types |
| Browser signing | `src/web/src/lib/keys.ts`, `src/web/src/lib/tx.ts` | `register`, `availability` and `reveal` already sign in a tab |
| Vector parity | `scripts/make_web_vectors.py`, `tests/unit/test_web_vectors.py` | the mechanism that keeps the TypeScript port byte-identical |
| REST node | `/v1/tx`, `/v1/blocks`, `/v1/accounts/{address}`, `/v1/mempool` | the only transport the wallet and the DEX UI need |
| Explorer and table | `src/web`, `sccp node run --web` | the shell the wallet and swap views live in |

What is missing, and is the subject of the rest of this document:

- `buildTransfer` and the rest of the wallet transaction builders;
- a keystore, so the browser can hold a key across reloads;
- Send, Receive, Assets, Swap and Bridge views;
- multi-asset balances, the asset registry and per-variant parameters;
- pool and swap transactions, quotes and LP accounting;
- HTLC transactions on SCCP, an `EthHtlc` contract and a signed order book.

## 3. Token model

### 3.1 One native asset, N variant assets

- **SCCP** is the native asset and the chain's fee and subsidy unit. It is mined
  by standard chess exactly as today: `max_supply` 21,000,000, halving every
  210,000 blocks, 8 decimals (`UNITS_PER_COIN = 100_000_000`). None of this
  changes; standard chess is not one variant among many but the chain's own
  unit of account.
- **A variant asset is minted only by the coinbase of that variant's proofs.**
  There is no premine, no sale, and no administrative mint. If nobody settles
  Crazyhouse games, no CZH exists.
- **Assets are consensus objects, not contracts.** An asset has an id, a supply
  counter and a minting rule. It has no bytecode, no issuer privileges and no
  upgrade path, so there is nothing for an issuer to rug and nothing for a
  holder to trust beyond the chain's own rules.

### 3.2 Asset identity

An asset id is the domain hash of its immutable parameters:

```text
asset_id = H("sccp/v2/asset", {symbol, name, decimals, variant,
                               max_supply, halving_interval})
```

Ids are derived, so two nodes cannot disagree about which asset a transfer
touches; the symbol is display only and consensus never compares it. `src/web`
ships a verified list (id -> symbol, name, logo) and the wallet warns -- never
blocks -- on an id the list does not contain, which is what stops a lookalike
symbol from being mistaken for a variant asset.

Proposed parameters (decisions 1 and 3 in section 12):

| Asset | Symbol | Minted by | `max_supply` | Decimals |
| --- | --- | --- | --- | --- |
| SCCP | SCCP | standard-chess proofs | 21,000,000 | 8 |
| Chess960 | C960 | chess960 proofs | 21,000,000 | 8 |
| Crazyhouse | CZH | crazyhouse proofs | 21,000,000 | 8 |

Each variant carries its own cap so SCCP's schedule is untouched. Giving every
variant the same cap and halving interval makes "one variant is one chain's worth
of scarcity" legible at launch; both are `VariantParams` fields and can diverge
later only by a fork.

### 3.3 The token is the games

The minting rule *is* the work, and the work is useful: the output is a growing
corpus of computer chess games, not a hash prefix. To receive CZH a miner must
settle Crazyhouse games that pass the same checks as standard ones: hybrid
co-signed match certificates, beacon-derived pairings, bonded registration,
replayable PGNs and `min_plies`. An asset's supply is therefore a count of
verified work in that variant, and the PGN store the chain already keeps is the
public good that work produced. Two consequences shape the design:

1. **A variant asset can be valued from its corpus.** An indexer over
   `GET /v1/blocks` can publish games per variant, distinct engines, opening
   spread, opponent diversity and settlement rate. The wallet shows those
   numbers next to the asset, so "what is CZH worth" has an observable answer
   that is not just a price chart.
2. **The protocol keeps the structural rules that keep the corpus diverse**,
   because those are the rules that can be decided exactly: distinct-opponent
   wins, per-opponent caps, opening-book stratification, populated
   funder/subnet fields, and one proof per game hash. They need no judgement
   about chess quality, which stays off chain (`PROTOCOL.md` section 9.2).

## 4. Workstream A -- the web wallet (0.20.0)

No consensus change. This is the piece that turns `src/web` from an explorer with
a playable seat into a wallet, and it is first because everything else needs a
way to sign in a browser.

**Status (0.20.0).** Shipped: the keystore port (§4.1), `buildTransfer` (§4.2),
the review screen and local nonce tracking (§4.3), the create/restore/open,
receive, send, history and settings views (§4.4), deployed at
`wallet.chessproofofwork.com` with the explorer. History is implemented as the
bounded block scan described in §4.3, not as a full index: the tab names the
heights it covered and caches that window in IndexedDB. The table moved in too:
`#/wallet/play` registers and signs with the wallet's unlocked key instead of a
throwaway seat in the explorer (`#/play` remains an alias). Still open from this
workstream: the QR code and the PWA manifest/service worker (§4.4), and the
Playwright devnet test (§4.5).

### 4.1 Keystore: one file, two implementations

The browser must open the same keyfile `sccp wallet` writes, and the CLI must
open the one the browser exports. `crypto/wallet.py` already fixes the format --
scrypt (`n = 2**17`, `r = 8`, `p = 1`, 16-byte salt), AES-256-GCM with a 12-byte
nonce, and the canonical JSON of the header as associated data -- and
`@noble/hashes` (already a dependency) provides scrypt in the browser while
`crypto.subtle` provides AES-GCM. So the plan is a straight port, not a second
format:

| Concern | Browser implementation |
| --- | --- |
| KDF | `@noble/hashes/scrypt`, same parameters as the CLI |
| AEAD | WebCrypto `AES-GCM`, same AAD construction |
| backup | BIP-39 24 words, the lossless encoding of the 32-byte master seed |
| export | the keyfile JSON, byte-compatible with `sccp wallet new` and restorable with `wallet import-mnemonic` |
| at rest | IndexedDB, encrypted; never `localStorage`, never the URL |
| unlock | password prompt; auto-lock timer; re-auth before revealing the phrase |

The seed never leaves the tab unencrypted and is never written to logs, the
clipboard, or a form field, and the derived keys are cleared on lock. The only
thing sent to a node is a signed transaction.

### 4.2 Transaction builders to add to `lib/tx.ts`

`buildTransfer` is the immediate gap; the rest land with their workstreams but
are listed here because the wallet is where they are used.

| Builder | Payload | Needed by |
| --- | --- | --- |
| `buildTransfer` | `{to, amount, public_key}` (v1), plus `asset?`/`memo?` (v2) | Send |
| `buildPoolCreate` | `{asset_a, asset_b, fee_bps}` | DEX |
| `buildAddLiquidity` | `{pool, amount_a, amount_b, min_shares}` | DEX |
| `buildRemoveLiquidity` | `{pool, shares, min_a, min_b}` | DEX |
| `buildSwap` | `{pool, asset_in, amount_in, min_out, deadline_height}` | DEX |
| `buildHtlcLock` | `{hashlock, timelock_height, beneficiary, amount, asset?}` | bridge |
| `buildHtlcClaim` | `{htlc_id, preimage}` | bridge |
| `buildHtlcRefund` | `{htlc_id}` | bridge |

Every payload also carries `public_key` -- the hybrid key the chain checks against
the signature, exactly as `ALLOWED[TxType.TRANSFER]` already requires -- and the
other rows omit it for brevity. `_check_unknown_keys` means adding a field is a
strict change, which is one more reason v2 is a fork rather than an extension.

Every one of them signs the same envelope (`network`, `type`, `from`, `nonce`,
`fee`, `payload`) with the same hybrid blob over `H("sccp/v1|v2/tx", body)`, so
the existing `signTransaction` and `toWireTransaction` stay unchanged. The hybrid
public key travels in the payload, and `resolve_public_key` resolves it against
the account's registered key or derives the address from it for a first
transaction, exactly as it does for `transfer` today.

### 4.3 Nonce, fee and confirmation handling

`GET /v1/accounts/{address}` reports committed state, not the mempool
projection -- the same trap the runbook documents for two transfers in a row. The
wallet therefore keeps a per-account local counter, seeds it from the committed
nonce on unlock, increments pessimistically on submit, re-reads it on rejection
and reconciles against `GET /v1/mempool` when a transaction is not mined within
a few blocks. Fees are shown in SCCP with the `UNITS_PER_COIN` scale and a hard
ceiling in the review screen.

Confirmations are counted from the block that includes the transaction, and the
UI distinguishes *included* from *final* at `reorg_depth + 1` (11 on mainnet,
where `reorg_depth` is 10). There is no history endpoint in v1, so the wallet
scans `GET /v1/blocks` back from the tip and caches the result in IndexedDB,
keyed by account and height.

### 4.4 Views

| View | Contents |
| --- | --- |
| Receive | bech32 address with checksum, QR, hybrid public-key fingerprint, network badge |
| Send | asset picker (SCCP only until 0.21.0), amount, fee, recipient; a review screen showing the canonical payload digest and every field that will be signed |
| Assets | per-asset balances, pending transactions, confirmed history, mining rewards |
| Swap | quote, slippage, `min_out`, review, submit (workstream C) |
| Bridge | orders, lock/claim/refund state machine with block heights (workstream D) |
| Settings | node URL, network, verified-asset list, auto-lock, keyfile export/backup |

The wallet is a PWA (manifest plus a service worker) served from the explorer
origin, so it installs to a home screen without an app store. A browser extension
with an injected provider is a separate design and is not in this phase.

### 4.5 Testing

- extend `scripts/make_web_vectors.py` with transfer, swap and HTLC bodies and
  their txids, and re-check them from `tests/unit/test_web_vectors.py`;
- vitest parity tests for every builder and for the keystore round-trip;
- a Playwright devnet test (new devDependency): create a wallet, fund it from the
  faucet, send to a CLI address, mine, then open the exported keyfile with
  `sccp wallet balance --key` in CI.

### 4.6 Done when

A fresh browser profile can create a wallet, receive from the faucet, send SCCP,
and export a keyfile that the CLI opens and signs with -- with vector parity and
the devnet e2e green, and no unencrypted seed written anywhere.

## 5. Workstream B -- protocol v2: assets and variants (0.21.0)

### 5.1 The fork

`PROTOCOL_VERSION` becomes 2, so every hash domain moves from `sccp/v1/...` to
`sccp/v2/...` (`version.py: DOMAIN_PREFIX`). That is a clean break by
construction, not an accident of the change: a v1 node cannot validate a v2
block, and no replay path is offered. Consequences:

- `genesis/{devnet,testnet,mainnet}.json` are regenerated; the hosted testnet
  resets, which it already does on every deploy;
- the golden chain and every pinned vector are regenerated (`make check`,
  `scripts/make_web_vectors.py`, `scripts/make_openapi_fixture.py`);
- `PROTOCOL.md` gains the v2 sections and keeps v1 readable under a version
  note;
- **v2 is the mainnet launch candidate.** No v1 mainnet has been declared. If one
  ever is, the migration is a snapshot import into a new genesis allocation, not
  a replay of v1 blocks.

### 5.2 Account and supply accounting

The smallest change that carries N assets is to leave the native field alone and
add a map beside it:

```python
@dataclass
class Account:
    ...
    balance: int                 # native SCCP, unchanged
    assets: dict[str, int]       # asset id (hex) -> units
```

Accessors -- `balance_of(account, asset)`, `credit`, `debit` -- mean the proof,
fee and coinbase code keeps one call site per balance change instead of knowing
about the map. Per-asset `minted` and `burned` counters live in state, and the
invariant `sum(balance_of(a, asset)) + burned == minted <= max_supply` is checked
on every block. Snapshot and restore gain `assets`; the state root changes, as
expected in a fork.

### 5.3 Transaction payloads

| Type | Change | Payload |
| --- | --- | --- |
| `transfer` | extended | `{to, amount, asset?, memo?}`; `memo` is arbitrary bytes <= 64, signed but otherwise ignored by consensus |
| `create_pool` | new | `{asset_a, asset_b, fee_bps}` |
| `add_liquidity` | new | `{pool, amount_a, amount_b, min_shares}` |
| `remove_liquidity` | new | `{pool, shares, min_a, min_b}` |
| `swap` | new | `{pool, asset_in, amount_in, min_out, deadline_height}` |
| `htlc_lock` | new | `{hashlock, timelock_height, beneficiary, amount, asset?}` |
| `htlc_claim` | new | `{htlc_id, preimage}` |
| `htlc_refund` | new | `{htlc_id}` |
| `availability` | extended | `{epoch, max_games, variants, commitment}`; the commitment binds the variant set |
| `register` / `reveal` | extended | optional `variants`; the reveal's commitment covers it |

There is deliberately **no `create_asset` transaction.** The asset set is fixed by
`NetworkParams`, which is what makes a variant token a claim on a game corpus
rather than a promise by an issuer, and it removes lookalike and spam assets by
construction. New assets are a protocol release, not a transaction.

### 5.4 Variants in consensus

`NetworkParams` gains `variants: tuple[VariantParams, ...]`, with standard chess
first and unchanged:

```python
@dataclass(frozen=True)
class VariantParams:
    name: str                 # "standard", "chess960", "crazyhouse"
    asset: str                # asset id hex; SCCP for standard
    max_supply: int
    halving_interval: int
    wins_required: int
    min_plies: int
    max_games: int            # per roster slot and epoch
    max_games_per_block: int  # how much of a variant a block may settle
    opening_book: str         # book id, or "none"
    engine_family: str        # "stockfish" | "fairy-stockfish"
```

Rules that follow from it:

- **Proofs carry a variant.** The game hash domain includes it
  (`sccp/v2/game/<variant>`), because a short PGN can be legal under more than one
  rule set and must not be replayable across them. The dedupe set is per variant
  for the same reason.
- **Matching is within a variant.** `availability` declares the set of variants a
  miner will play; pairings, rosters and `MinerRating` are keyed
  `(variant, address)`. One engine can therefore hold different ratings per
  variant, which is the honest model -- Crazyhouse strength is not standard
  strength.
- **Coinbase is per variant.** For every variant with settled proofs in a block,
  `variant_subsidy(height)` of that variant's asset is minted and split between
  the winners pro-rata by wins. Standard's output is identical in effect to v1.
- **Throughput is the per-variant knob.** Block production stays one block per
  interval; `max_games_per_block` and `wins_required` decide how much of each
  variant's work can reach it. Raising them for a busy variant raises the work
  needed per unit of its asset -- the same effect target difficulty has on a
  hash chain, expressed in games.
- **Endowments are optional and off by default.** A variant may name an
  endowment account whose balance is paid to that variant's miners over
  `endowment_drain_epochs`, adding to (never taxing) the subsidy. Default
  `0` in 0.21, enabled per network once the demand for it is measurable.
- **Fees stay in SCCP.** A variant asset is a reward and a store of the corpus,
  not a fee unit; one fee market keeps mempool policy and miner economics
  unchanged.

### 5.5 Engines and tooling

- `data/engines.json` gains `fairy-stockfish` entries with the same URL and
  SHA-256 pinning `scripts/fetch_engine.py` already verifies; Chess960 uses the
  pinned Stockfish with `UCI_Chess960=true`.
- `sccp miner` gains `--variant`; `scripts/play_miner.py`,
  `scripts/export_block_games.py`, the devnet fixtures and the explorer gain the
  same dimension.
- `sccp chain account` and the roster views report per-variant ratings;
  `PROTOCOL.md` documents the extended `availability` payload.

### 5.6 Done when

A two-variant devnet (standard + chess960, then crazyhouse) settles proofs of
both kinds in the same blocks, `GET /v1/accounts/{address}` reports SCCP and the
variant asset, coinbase amounts match the per-variant schedule, a standard game
is rejected when submitted as Crazyhouse, and the full suite plus the regenerated
golden chain is green.

## 6. Workstream C -- native DEX (0.22.0)

The DEX trades the assets the chain mints: SCCP against variant assets, and
variant assets against each other. It has no admin key, no pause, no fee switch
and no upgrade path; changing any of those is a fork, exactly like a consensus
rule.

### 6.1 Pools as consensus objects

- **Constant product** with reserves `(r_a, r_b)` and an immutable fee
  `fee_bps` in `1..100`. The pool id is derived --
  `H("sccp/v2/pool", {asset_a, asset_b, fee_bps})` -- with assets in
  lexicographic id order, so a pair and a fee level identify exactly one pool and
  duplicates are impossible.
- **LP positions are share counts**, `Account.pools: dict[pool_id, int]` against
  a per-pool `total_shares`. They are not transferable in 0.22: nothing on chain
  needs them to be, and a non-transferable position keeps the asset universe to
  game-minted assets only. If a lending or staking market later needs LP tokens,
  that is a v3 change with a reason.
- **First deposit** mints `isqrt(amount_a * amount_b)` shares and permanently
  locks `MIN_LIQUIDITY = 1_000` of them to an address derived from
  `H("sccp/v2/burn")` that no key can spend from, so the first LP cannot set an
  arbitrary price and then donate a unit to grief later depositors.
- **Invariants, checked per block:** `k_after >= k_before` per touched pool,
  rounding always in the pool's favour, `sum(reserves)` consistent with
  `minted - burned`, and each pool's share total matching the sum of positions.
- **`deadline_height`** on every swap and liquidity change, so a transaction that
  was stuck in a mempool cannot execute minutes later at a different price.

### 6.2 Clearing model: one price per block

A 600-second block with a public mempool makes first-come-first-served pricing a
gift to whoever orders the block. Swaps therefore clear as a **batch auction**
per pool, which changes the ordering guarantee without changing the invariant:

1. collect every `swap` for a pool in the block;
2. net the aggregate trade against the pool's pre-block reserves;
3. settle every included swap at the resulting single clearing price, honouring
   each transaction's `min_out`;
4. iterate -- drop orders whose `min_out` fails, recompute, repeat until stable
   (bounded passes, deterministic order by txid) -- then refund any remainder.

Two properties follow and are worth stating in the UI: no transaction can be
sandwiched by another in the same block, and the price a user gets is not visible
until the block is built. For orders large enough that pre-trade visibility
matters, 0.22 also accepts a **commit-reveal** pair (`swap_commit` in block N,
`swap` in N+1) reusing the commitment machinery from `protocol/commitments.py`;
the commit hides the intent and the reveal executes it under the same batch
rules.

### 6.3 API

| Endpoint | Returns |
| --- | --- |
| `GET /v1/assets` | the fixed asset list: id, symbol, name, decimals, variant, supply, minted |
| `GET /v1/assets/{id}` | one asset plus its minting rule |
| `GET /v1/pools` | pools with reserves, `total_shares`, fee, volume and TVL |
| `GET /v1/pools/{id}` | one pool |
| `GET /v1/pools/{id}/quote?asset_in&amount_in` | `amount_out`, `fee`, `price`, the height it was computed at, and a suggested `min_out` |
| `GET /v1/blocks/{reference}` | gains `swaps` and `pool_deltas`, so the explorer charts prices without an indexer |

Quotes are computed from committed state and stamped with a height; the wallet
re-quotes at submit time and refuses to sign a stale quote whose implied
slippage exceeds the user's setting. `scripts/make_openapi_fixture.py` is
regenerated with the new routes.

### 6.4 Wallet and explorer

- Swap view: pick a pool (or two assets and let it route through SCCP), amount,
  fee and price impact, slippage in bps, `min_out`, review, submit.
- Liquidity view: existing positions, share of pool, fees earned, add and remove
  with `min_a`/`min_b` and `deadline_height`.
- Pools list: TVL, 24 h volume, fee APR, price sparkline from `pool_deltas`.
- Asset pages show corpus metrics from section 3.3 beside the market data, which
  is the part a generic DEX UI cannot show.

### 6.5 Spam and safety

- `create_pool` costs `pool_creation_fee` in SCCP, burned. The asset universe is
  already closed, so the fee only needs to make pointless pools pointless.
- No oracle: prices are pool ratios, and the UI labels thin pools with their
  reserves rather than pretending a price is a market price.
- The verified-asset list is a UI hint keyed by asset id; consensus does not
  consult it and cannot be fooled by a symbol.

### 6.6 Done when

On devnet a wallet swaps SCCP -> CZH -> SCCP through the UI, quotes match
execution to the unit at the quoted height, `k` never decreases across a
randomised property test, batch clearing resists an in-block sandwich (an
adversarial test with a deliberately front-run order), and removing all
liquidity after the fee accrual returns the expected amounts to the unit.

## 7. Workstream D -- ETH<->SCCP bridge (0.23.0)

### 7.1 Principle: no custody, one secret

The bridge is a pair of hash time-locked contracts, one on each chain, sharing a
single random secret. Nobody holds the ETH, nobody holds the SCCP, and there is no
mint, peg, federation or exchange account. The trade is atomic: either both legs
settle or both refund. That is what makes "someone with ETH can buy this token in
a decentralized way" true without introducing a party to trust.

### 7.2 The SCCP side: HTLC transactions

```text
htlc_lock   {hashlock, timelock_height, beneficiary, amount, asset?}
htlc_claim  {htlc_id, preimage}
htlc_refund {htlc_id}
```

- `htlc_id = H("sccp/v2/htlc", {from, nonce})`; the locked funds move out of the
  sender's `balance` into a consensus-held HTLC record. No new key type, no new
  signature scheme, no contract VM, no new address space.
- `htlc_claim` checks `sha256(preimage) == hashlock` -- the plain hash, with no
  domain prefix, so the Ethereum contract verifies the same 32 bytes -- pays
  `beneficiary`, and is actually signed by the beneficiary's hybrid key. A leaked
  preimage alone cannot redirect funds, and `PROTOCOL.md`'s rule that a
  transaction is authorised by the key in `from` is preserved exactly.
- `htlc_refund` is signed by the original `from` and is only valid at
  `height >= timelock_height`, after which the claim becomes invalid. The two
  paths are mutually exclusive by construction, and both are ordinary
  hybrid-signed transactions, so the bridge inherits the ledger's security
  instead of extending it.
- The HTLC record is part of state and therefore part of the state root and the
  snapshot; `GET /v1/accounts/{address}` gains an `htlcs` list and
  `GET /v1/htlc/{id}` reads one.

### 7.3 The Ethereum side: `contracts/EthHtlc.sol`

A single minimal contract, deployed once, used by every swap:

```solidity
function lock(bytes32 hashlock, uint64 timelock, address beneficiary)
    external payable returns (bytes32 id);
function claim(bytes32 id, bytes32 preimage) external;
function refund(bytes32 id) external;
```

- no proxy, no owner, no admin function, no upgrade path, no ERC-20 support in
  the first version (ETH only);
- `claim` pays `beneficiary`; `refund` is only callable by the depositor and only
  after the timelock; the preimage check is `sha256` so the same 32 bytes work on
  both chains;
- `test/EthHtlc.t.sol` under Foundry with the adversarial cases below, a
  deployment script, and an external audit before mainnet.

The SCCP node never gains an EVM, an `eth_*` endpoint or a Solidity runtime. The
contract lives on the counterparty chain; it is the only Solidity in this
repository and it runs nowhere near consensus.

The cryptography is asymmetric and the UI says so: the Ethereum leg is secp256k1
and keccak-based, the SCCP leg is hybrid Ed25519 + ML-DSA-65, and a swap is only
as strong as the weaker leg. That is inherent to trading with Ethereum today --
the post-quantum guarantee covers the SCCP side, which is where the chess assets
live.

### 7.4 Swap protocol

Let `T_eth` be the Ethereum unlock time and `T_sccp` the SCCP timelock height. The
rule is always: **the second leg carries the longer timelock.**

```text
T_sccp >= T_eth + margin      margin = 20 blocks on mainnet (~200 min)
                              margin =  4 blocks on devnet/testnet
```

Direction A -- an ETH holder buys SCCP from a miner:

1. The maker (miner) publishes a signed sell order (section 7.5).
2. The taker locks ETH in `EthHtlc` with hashlock `H = sha256(s)`,
   timelock `T_eth`, beneficiary = maker's ETH address.
3. The maker verifies the Ethereum lock, then locks SCCP with the same `H`,
   timelock `T_sccp > T_eth + margin`, beneficiary = taker's SCCP address.
4. The taker claims SCCP, revealing `s` in the SCCP transaction; the maker reads
   `s` from the chain and claims the ETH with it.

Direction B -- a miner sells SCCP for ETH -- is the same protocol with the legs
swapped: the taker locks SCCP first (long timelock), the maker locks ETH second
(short timelock), the maker claims SCCP and the taker claims ETH.

Failure paths, all of which are tested rather than assumed:

| Stall | Outcome |
| --- | --- |
| taker locks ETH, maker never locks SCCP | taker refunds ETH at `T_eth` |
| maker locks SCCP, taker never claims | maker refunds SCCP at `T_sccp` |
| maker locks SCCP, taker claims but maker does not claim ETH | maker claims ETH later with the published preimage; the secret is public by then |
| either party submits a wrong preimage, claims twice, or claims after refund | reverted by the contract / invalid transaction |

Operational rules that keep it safe:

- never lock the second leg before the first leg has `reorg_depth + 1`
  confirmations (`reorg_depth` is 10 on mainnet);
- never reveal `s` before the counter-leg is final;
- every order is single-use (`nonce`), expiring (`expiry`), and binds both the
  maker's SCCP and ETH addresses, so a signature cannot be replayed on another
  order or redirected to another beneficiary.

### 7.5 Orders: signed, off-consensus

```json
{
  "maker_sccp": "sccpt1...", "maker_eth": "0x...",
  "give_asset": "SCCP", "give_amount": 2500000000,
  "want_asset": "ETH",  "want_amount": 1000000000000000,
  "min_sccp_timelock": 3620, "expiry": 1790000000,
  "nonce": 7, "signature": "01..."
}
```

The maker signs `H("sccp/v2/order", body)` with the hybrid key, so an order is
verified with the same code path as a transfer and cannot be forged or
tampered with. The node exposes `POST /v1/orders`, `GET /v1/orders` (filter by
pair), and `DELETE /v1/orders/{id}` (a signed cancel). The order book is **not
consensus**: it holds no funds, it can be mirrored by anyone, and losing it
costs nothing but a re-post.

Amounts are integers in each chain's smallest unit -- 8 decimals on SCCP, 18 on
Ethereum -- and the price is implicit in the pair, so there is no oracle and no
rounding rule to argue about. `expiry` is Unix seconds and `min_sccp_timelock` is
an SCCP block height, because the two chains share no clock. Orders are
whole-order in 0.23; a maker bot re-quotes rather than tracking partial fills.

### 7.6 User and miner flows

**Buying SCCP with ETH.** The wallet's Bridge view lists open sell orders with
the price in ETH per SCCP and the maker's rating. Choosing one starts the
four-step flow of section 7.4, with a state machine on screen -- *ETH locked*,
*waiting for maker*, *SCCP locked (confirmed)*, *claimable*, *refundable at block
N* -- and every step is signed locally. A taker may also use the one-click path
against a maker bot: the same protocol, driven by the bot, with the wallet still
signing both of the taker's transactions.

**Selling mined SCCP.** A miner runs `sccp mm` (market maker), a small quoter that
locks its own coins and never anyone else's:

```console
$ sccp mm --sell SCCP --for ETH --price 0.0000042 --max 500 --node https://...
```

It signs orders with the same keyfile the miner already has, re-quotes as
inventory and price move, and cancels by signed message. `sccp miner --auto-sell`
composes on top of it for operators who want rewards converted as they vest. No
custody anywhere: the bot can lose its own inventory to a bug, and nobody else's.

### 7.7 Later, and optional: wrapped ETH (0.24.0)

If users want ETH itself traded inside the native DEX (rather than swapped
peer-to-peer), add a `wETH` asset backed by a **bonded federation**: N operators
hold a threshold hybrid key, attest deposits and redeems, and consensus enforces
caps and per-epoch limits. This is a trust assumption, plainly stated in the UI
and the docs -- it is not a light client. It is out of scope for the goal in
section 1, must not precede an audit, and the HTLC path keeps working without
it. A post-merge Ethereum SPV verifier would replace the federation later; that
is a research item, not a release item.

### 7.8 Done when

On Anvil plus a devnet, an automated swap completes in both directions; every
row of the failure table has a test; a wrong preimage, a double claim, an early
refund, a replayed order and a stale-timelock order are all rejected; the
Ethereum contract passes its Foundry suite and a static analysis run; and the
wallet's Bridge view recovers correctly from a page reload at every step.

## 8. Release train

| Version | Contents | Gate |
| --- | --- | --- |
| 0.19.0 | this document | reviewed; no code change |
| 0.20.0 | web wallet | vectors, vitest, Playwright devnet e2e; keyfile round-trips with the CLI |
| 0.21.0 | protocol v2: assets and variants | v2 spec in `PROTOCOL.md`; regenerated golden chain; two-variant soak |
| 0.22.0 | native DEX | property tests for `k` and rounding; quote equals execution; no admin keys |
| 0.23.0 | HTLC bridge and order book | adversarial swap suite on Anvil + devnet; contract audit |
| 0.24.0 | `wETH` federation (optional) | second audit; consensus caps; explicit trust disclosure |
| 1.0.0 | mainnet | the go/no-go in `IMPLEMENTATION_PLAN.md`, plus the bridge audit |

Each release keeps `make check` green, regenerates the vectors and OpenAPI
fixtures it changes, and updates `PROTOCOL.md`, `README.md` and the operator
guides in the same commit, which is the convention the repository already
follows.

## 9. Architecture at a glance

```text
                        +---------------------------+
   browser wallet  ---> |  sccp node (REST only)    | <--- engine miners
   (PWA, hybrid keys)   |  /v1/tx /v1/blocks        |      (standard, 960,
                        |  /v1/accounts /v1/pools   |       crazyhouse)
                        |  /v1/orders /v1/htlc      |
                        +------------+--------------+
                                     | gossip
                        +------------+--------------+
                        |  consensus: proofs,       |
                        |  assets, pools, HTLCs     |
                        +---------------------------+

   Ethereum  <--- EthHtlc.sol (lock/claim/refund) --->  HTLC swap  <--- SCCP HTLC
                                                          |
                                              signed orders (off-consensus)
```

Data flow for a trade, end to end: a miner's engine plays a game, the relay
pairs seats and co-signs the certificate, a block settles it and mints SCCP (or a
variant asset) to the winner. The winner's wallet can swap that asset for another
on the native DEX, or publish a sell order and swap it for ETH against anyone
holding ETH. The games stay on chain and readable throughout; the assets are
receipts for producing them.

## 10. Security checklist

| Area | Threat | Mitigation | Where |
| --- | --- | --- | --- |
| Wallet | a compromised dependency exfiltrates the seed | no remote code, bundled deps, CSP, seed only in memory, no seed in logs/clipboard | 4.1 |
| Wallet | offline brute force of a keyfile | scrypt `n = 2**17` and a 32-byte seed, the same cost the CLI pays | 4.1 |
| Wallet | nonce reuse or a dropped transaction | local counter seeded from committed state, reconciled against `/v1/mempool` | 4.3 |
| Wallet | address substitution | bech32 checksum, key fingerprint, full-payload review before signing | 4.4 |
| DEX | in-block sandwich | single clearing price per block, `min_out` per order | 6.2 |
| DEX | rounding or first-LP price setting | pool-favoured rounding, `k` monotonic, `MIN_LIQUIDITY` locked | 6.1 |
| DEX | stale execution after a mempool delay | `deadline_height` on every pool transaction | 6.1 |
| DEX | pool spam | `pool_creation_fee`, closed asset universe | 6.5 |
| Bridge | one party stalls | timelocks on both legs, refunds signed by the party that locked | 7.4 |
| Bridge | reorg after the second leg locks | second leg waits `reorg_depth + 1` confirmations | 7.4 |
| Bridge | order replay or redirection | `nonce`, `expiry`, both addresses bound in the signed body | 7.5 |
| Bridge | a bug in the Ethereum contract | no admin, no proxy, small surface, Foundry suite, external audit | 7.3 |
| Bridge | a weak classical leg | disclosed in the UI: the SCCP side is post-quantum, the ETH side is not | 7.3 |
| v2 | consensus divergence or asset inflation | `PROTOCOL_VERSION` fork, regenerated golden chain, per-block supply invariant | 5.1, 5.2 |
| v2 | a standard game replayed as a variant | variant inside the game hash domain; per-variant dedupe | 5.4 |
| v2 | an unpinned engine | URL plus SHA-256 pins in `data/engines.json`, verified at fetch | 5.5 |

## 11. Non-goals

- **EVM compatibility.** No `eth_*` endpoints, no chain-id registration, no
  ERC-20 or Solidity on the SCCP node, no MetaMask network switch. The one
  Solidity file is the counterparty HTLC contract and it runs on Ethereum.
- **Permissionless asset issuance.** No `create_asset`, no ICO, no premine, no
  token sale. Assets are protocol-defined and mined.
- **A custodial bridge or deposit addresses.** The HTLC path needs neither. An
  exchange that wants to list SCCP integrates against the same transactions.
- **An AMM over external assets before `wETH`.** No oracle, no wrapped assets in
  0.22.
- **Bughouse and Go in 1.x.** Bughouse is four seats across two boards and needs
  a different match object; Go is a different game protocol entirely.
- **Cross-chain swaps beyond Ethereum,** at least until the ETH path is audited.
  The same HTLC protocol extends to any chain with hash locks and timelocks, one
  contract each; that is future work with no new design.

## 12. Open decisions

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| 1 | Variant supply | own cap per variant / share of SCCP's 21M | own cap; leave SCCP untouched |
| 2 | Are variant assets fee units? | yes / no | no: one fee market, in SCCP |
| 3 | Chess960 | separate asset / a parameter on SCCP | separate asset `C960`, Stockfish with `UCI_Chess960` |
| 4 | Endowment payout | linear per block / per epoch / manual | per epoch, `endowment_drain_epochs`, off by default |
| 5 | LP positions | non-transferable shares / an LP token asset | non-transferable in 0.22 |
| 6 | Swap privacy | batch only / batch plus commit-reveal | batch default, commit-reveal above a size threshold |
| 7 | First bridge chains | Ethereum mainnet / an L2 | mainnet plus one L2 (Base), because claim gas dominates small trades |
| 8 | Timelock margin | 10 / 20 blocks on mainnet | 20 blocks (~200 min), 4 on devnet |
| 9 | Order transport | node REST / gossip | REST first, gossip later, same signed body |
| 10 | Symbols | ticker collision handling | symbol is display only; the verified list is keyed by asset id |

## 13. References

- [`PROTOCOL.md`](PROTOCOL.md) -- section 2 (canonical game hash, the variant
  dimension v2 adds to it), 3 (PGN contract), 10 (transactions, including 10.5 on
  the removed classical scheme), 11 (ledger state), 12 (blocks and fork choice),
  14 (node API), and 9.2 (why quality is not arbitrated on chain).
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) -- the work on the chain as
  it stands, and the mainnet go/no-go.
- [`WHITEPAPER.md`](WHITEPAPER.md) -- how the current implementation works end to
  end.
- `src/sccp/chain/tx.py` -- the transaction envelope and the `ALLOWED` payload
  set that workstreams B to D extend.
- `src/sccp/crypto/wallet.py` -- the keyfile format the browser keystore ports.
- `src/web/src/lib/keys.ts`, `src/web/src/lib/tx.ts` -- the in-browser hybrid
  signer and the transaction builders this plan extends.
- `scripts/make_web_vectors.py` -- the generator that keeps the TypeScript ports
  byte-identical to Python.

