# The big picture: a wallet, a bridge and a DEX

> **Status (tree at 0.33.0).** Implemented except for the optional `wETH`
> federation and the items marked **not built**. The browser wallet (section 4),
> variant assets (section 5), the native DEX (section 6) and the trustless
> ETH<->SCCP HTLC bridge (section 7) are in the tree; the bridge contract is
> `contracts/EthHtlc.sol`; the SCCP side is `htlc_lock`/`htlc_claim`/
> `htlc_refund` with `HtlcRecord` in state, and signed orders live in the
> off-consensus `OrderBook`. Each section describes the shipped protocol and names
> the difference where the code diverges from what was planned -- the DEX executes
> in transaction order with no clearing batch (section 6.2) and has no
> `deadline_height` (section 6.1). Where an early draft said `sccp/v2`, the
> implementation kept the `sccp/v1` domain prefix: the additional transaction
> types and state fields did not need the fork the plan assumed (section 5.1). The
> Ethereum-side environment mapping is fixed per SCCP network
> (`sccp.bridge.config`). Still future work: batch clearing and `deadline_height`
> (section 6.2), the automated `sccp mm` quoter (section 7.6), per-asset corpus
> metrics (section 3.3), the optional federated `wETH` asset (section 7.7), more
> bridge chains and an external audit. The prioritized work on the chain as it
> stands today stays in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md), and the
> per-release record is `CHANGELOG.md`.

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
| Web wallet | creates, holds and signs in the browser | none | 0.22.0, shipped |
| Multi-asset ledger + variants | variant tokens mined by variant games | add `asset`/`variant` to payloads, per-asset balances and maturity queues | 0.24.0, shipped |
| Native DEX | pools, LP positions and swaps between on-chain assets | new transaction types, `pools` in state | 0.24.0, shipped |
| ETH<->SCCP bridge | HTLC atomic swaps; no custodian, no wrapped peg | new transaction types, `htlcs` in state, one Ethereum contract | 0.24.0, shipped |

All four shipped between 0.22.0 and 0.24.0, and the tree is at 0.33.0. The
`sccp/v2` fork this document assumed did not happen: `PROTOCOL_VERSION` is still
1, the domains stayed `sccp/v1/...`, and `PROTOCOL.md` documents the new
transaction types as part of v1 (section 5.1).

Optionally, a later release adds a `wETH` asset so that ETH itself can be traded
in the native DEX; that is a bonded federation and is explicitly out of the
critical path (section 7.7).

## 2. Where the project stands

What already exists and will be reused rather than replaced:

| Piece | Where | What the new work needs from it |
| --- | --- | --- |
| Hybrid accounts | `crypto/pq.py`, `crypto/address.py` | every new asset, pool and bridge claim is owned by one of these |
| Signed transactions | `chain/tx.py`, `ALLOWED` payload set | the extension point for the new transaction types |
| Browser signing | `src/web/src/lib/keys.ts`, `src/web/src/lib/tx.ts` | every transaction type signs in a tab, including pools, swaps and HTLCs |
| Vector parity | `scripts/make_web_vectors.py`, `tests/unit/test_web_vectors.py` | the mechanism that keeps the TypeScript port byte-identical |
| REST node | `/v1/tx`, `/v1/blocks`, `/v1/accounts/{address}`, `/v1/mempool`, `/v1/pools`, `/v1/orders`, `/v1/htlcs` | the only transport the wallet and the DEX UI need |
| Explorer and table | `src/web`, `sccp node run --web` | the shell the wallet and swap views live in |

What this document set out to add, and where it landed:

| Planned piece | Shipped as |
| --- | --- |
| `buildTransfer` and the rest of the wallet transaction builders | `lib/tx.ts`: `buildRegister`, `buildAvailability`, `buildReveal`, `buildTransfer`, `buildCreatePool`, `buildAddLiquidity`, `buildRemoveLiquidity`, `buildSwap`, `buildHtlcLock`, `buildHtlcClaim`, `buildHtlcRefund` |
| a keystore, so the browser can hold a key across reloads | `lib/keystore.ts` over IndexedDB in the CLI's keyfile format, `lib/vault.ts` for the unlocked key, `lib/mnemonic.ts` for the 24-word backup |
| Send, Receive, Assets, Swap and Bridge views | `views/wallet` (overview, send, receive, faucet, history, settings), `views/dex` (swap, liquidity, create pool), `views/bridge` (buy, sell, activity); per-asset balances appear on the overview and in history rather than in a separate tab |
| multi-asset balances, the asset registry and per-variant parameters | `Account.assets`, `VariantParams` served by `GET /v1/params`, projected into `lib/assets.ts` |
| pool and swap transactions, quotes and LP accounting | `chain/dex.py`, the same curve in `lib/dex.ts`, `sccp dex quote` and `/v1/pools` |
| HTLC transactions on SCCP, an `EthHtlc` contract and a signed order book | `chain/tx.py`, `contracts/EthHtlc.sol`, `bridge/orders.py` with `/v1/orders` and `sccp bridge` |

## 3. Token model

### 3.1 One native asset, N variant assets

- **SCCP** is the native asset and the chain's fee and subsidy unit. It is mined
  by standard chess exactly as today: `max_supply` 21,000,000, halving every
  210,000 blocks, 8 decimals (`UNITS_PER_COIN = 100_000_000`). None of this
  changes; standard chess is not one variant among many but the chain's own
  unit of account.
- **A variant asset is minted by the coinbase of that variant's proofs.** The
  one exception is a fixed genesis allocation: 5% of each variant's cap, public
  and auditable, for liquidity and ecosystem incentives. There is no sale and no
  administrative mint after genesis. If nobody settles variant games, no variant
  token is mined -- C960 today, since a variant exists only if the network defines
  it (only `chess` and `chess960` are accepted; §5.4).
- **Assets are consensus objects, not contracts.** An asset has an id, a supply
  counter and a minting rule. It has no bytecode, no issuer privileges and no
  upgrade path, so there is nothing for an issuer to rug and nothing for a
  holder to trust beyond the chain's own rules.

### 3.2 Asset identity

An asset id is the domain hash of its immutable parameters. As shipped
(`config.py: CHESS960_ASSET_ID`):

```text
asset_id = H("sccp/v1/asset", {symbol, name, decimals, variant})
```

The id commits to the variant that mints the token and to its display
parameters; `max_supply`, `halving_interval` and the subsidy schedule are
`VariantParams` fields fixed per network, so changing them is a protocol
release and not something an asset id has to carry. Ids are derived, so two
nodes cannot disagree about which asset a transfer touches, and the symbol is
display only: consensus never compares it. `src/web` builds its token list from
`GET /v1/params` (`lib/assets.ts`), so a wallet can only print a symbol the
network itself defines; an asset id the params do not name is rendered as
`asset` with its id beside it, never mapped to a guessed symbol. Because the
universe is closed, a lookalike token has no route into the UI at all.

Shipped and proposed parameters (decisions 1 and 3 in section 12):

| Asset | Symbol | Minted by | `max_supply` | Decimals | Status |
| --- | --- | --- | --- | --- | --- |
| SCCP | SCCP | standard-chess proofs | 21,000,000 | 8 | shipped |
| Chess960 | C960 | chess960 proofs | 21,000,000 | 8 | shipped (0.24.0) |
| Crazyhouse | CZH | crazyhouse proofs | 21,000,000 | 8 | **not built** |

Each variant carries its own cap so SCCP's schedule is untouched. Giving every
variant the same cap and halving interval makes "one variant is one chain's worth
of scarcity" legible at launch; `max_supply`, `halving_interval` and the subsidy
curve are `VariantParams` fields, so they can diverge later only by a fork. Adding
a third variant is a protocol release plus its own genesis tranche, not a config
edit.

Mainnet genesis pre-allocates 1 basis point of the SCCP cap (2,100 SCCP) as a
bootstrap grant and no variant supply: C960 is mined, and only testnet seeds
demo liquidity. The grant is public in `genesis/mainnet-premine.json`; see
[`PROTOCOL.md` §13](PROTOCOL.md#13-genesis-documents).

### 3.3 The token is the games

The minting rule *is* the work, and the work is useful: the output is a growing
corpus of computer chess games, not a hash prefix. To receive C960 a miner must
settle Chess960 games that pass the same checks as standard ones: hybrid
co-signed match certificates, beacon-derived pairings, bonded registration,
replayable PGNs with the variant headers and the certified start FEN, and
`min_plies`. An asset's supply is therefore a count of verified work in that
variant, and the PGN store the chain already keeps is the public good that work
produced. Two consequences shape the design:

1. **A variant asset can be valued from its corpus.** An indexer over
   `GET /v1/blocks` can publish games per variant, distinct engines, opening
   spread, opponent diversity and settlement rate, and the wallet could show
   those numbers next to the asset, so "what is C960 worth" has an observable
   answer that is not just a price chart. That indexer is **not built**: the
   explorer reads blocks live, keeps no history and has no such page (§4.4, §6.4).
2. **The protocol keeps the structural rules that keep the corpus diverse**,
   because those are the rules that can be decided exactly: distinct-opponent
   wins, per-opponent caps, opening-book stratification, populated
   funder/subnet fields, and one proof per game hash. They need no judgement
   about chess quality, which stays off chain (`PROTOCOL.md` section 9.2).

## 4. Workstream A -- the web wallet (shipped in 0.22.0)

No consensus change. This is the piece that turns `src/web` from an explorer with
a playable seat into a wallet, and it is first because everything else needs a
way to sign in a browser.

**Status (0.22.0, extended through 0.30.0).** Shipped: the keystore port (§4.1),
every transaction builder (§4.2), the review screen and local nonce tracking
(§4.3), the create/restore/open, receive, send, faucet, history and settings
views (§4.4) and the DEX and Bridge pages (§6.4, §7.6), deployed at
`https://app.proofofchess.com/#/wallet` with the explorer. 0.29.0 added
**Choose a file…**, which reads a keyfile straight from the computer so the file
`sccp wallet new` wrote goes in without being copied by hand, and gave a swap one
history row that names the input, the asset the pool paid out and the
`min_amount_out` floor the chain enforced. History is the bounded block scan
described in §4.3, not a full index: the tab names the heights it covered and
caches that window in IndexedDB (version 2 since the swap rows changed shape).
The table moved in too: `#/wallet/play` registers and signs with the wallet's
unlocked key instead of a throwaway seat in the explorer (`#/play` remains an
alias). Still open from this workstream: the QR code and the PWA manifest/service
worker (§4.4), and the Playwright devnet test (§4.5).

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

### 4.2 Transaction builders

All of these shipped in `lib/tx.ts`; the table records the payload each builder
signs today.

| Builder | Payload | Needed by |
| --- | --- | --- |
| `buildTransfer` | `{to, amount, public_key, asset?}`; no field is reserved for a memo | Send |
| `buildCreatePool` | `{asset_a, amount_a, asset_b, amount_b, public_key}` with the pair ordered by asset id; the fee is the network's `dex_fee_bps`, not a pool parameter | DEX |
| `buildAddLiquidity` | `{pool_id, amount_a, amount_b, public_key}` | DEX |
| `buildRemoveLiquidity` | `{pool_id, shares, public_key}` | DEX |
| `buildSwap` | `{pool_id, asset_in?, amount_in, min_amount_out, public_key}`; no `deadline_height` (§6.2) | DEX |
| `buildHtlcLock` | `{hashlock, timelock_height, beneficiary, amount, public_key, asset?}` | bridge |
| `buildHtlcClaim` | `{htlc_id, preimage, public_key}` | bridge |
| `buildHtlcRefund` | `{htlc_id, public_key}` | bridge |

Each payload carries `public_key` -- the hybrid key the chain checks against the
signature, exactly as `ALLOWED[TxType.TRANSFER]` requires -- and `asset` when the
transaction moves a variant token rather than native SCCP. `_check_unknown_keys`
means adding a field is a strict change, so a new field needs a `PROTOCOL_VERSION`
bump; the fields above were all added inside v1 because they arrived with the
transaction types that consume them.

Every one of them signs the same envelope (`network`, `type`, `from`, `nonce`,
`fee`, `payload`) with the same hybrid blob over `H("sccp/v1/tx", body)`, so
`signTransaction` and `toWireTransaction` were unchanged by all four workstreams.
The hybrid public key travels in the payload, and `resolve_public_key` resolves it
against the account's registered key or derives the address from it for a first
transaction, exactly as it does for `transfer`.

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

| View | Contents | Status |
| --- | --- | --- |
| Receive | bech32 address with checksum, QR, hybrid public-key fingerprint, network badge | shipped without the QR code |
| Send | asset picker (native SCCP and each variant token, e.g. C960), amount, fee, recipient; a review screen showing the canonical payload digest and every field that will be signed | shipped |
| Assets | per-asset balances, pending transactions, confirmed history, mining rewards | shipped as the overview's per-asset balances plus the history rows, not as its own tab |
| Swap | quote, slippage, `min_out`, review, submit (workstream C) | shipped (`views/dex`): swap, liquidity and create-pool panels |
| Bridge | orders, lock/claim/refund state machine with block heights (workstream D) | shipped (`views/bridge`): buy, sell and activity panels |
| Settings | node URL, network, token list from `/v1/params`, auto-lock, keyfile export/backup | shipped |

The wallet is served from the explorer origin as a normal single-page app. The
planned PWA manifest and service worker were not built, so there is no offline
cache and no install prompt beyond whatever the browser adds on its own. A
browser extension with an injected provider is a separate design and is not in
this phase.

### 4.5 Testing

Shipped:

- vitest parity tests for every builder (`lib/tx.test.ts`), the keystore
  round-trip (`lib/keystore.test.ts`, `lib/mnemonic.test.ts`) and the curve
  (`lib/dex.test.ts`, mirroring `chain/dex.py`);
- `scripts/make_web_vectors.py` regenerates the register and transfer vectors,
  the keyfile vector and the match/ledger fixtures, and
  `tests/unit/test_web_vectors.py` re-checks the rendered file, so a drift in a
  shared canonicalisation fails `make check`.

Still open:

- the vector file stops at `register` and `transfer`. The pool, swap and HTLC
  builders are covered only by vitest assertions written against the Python
  implementation by hand, so a change to `sccp/chain/tx.py` that moved a pool or
  HTLC txid would not fail `make check`. Extending the generator to those bodies
  is the fix, and it is listed in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md);
- a Playwright devnet test (new devDependency): create a wallet, fund it from the
  faucet, send to a CLI address, mine, then open the exported keyfile with
  `sccp wallet balance --key` in CI. The web app is vitest-only today.

### 4.6 Done when

A fresh browser profile can create a wallet, receive from the faucet, send SCCP,
and export a keyfile that the CLI opens and signs with -- with vector parity and
the devnet e2e green, and no unencrypted seed written anywhere.

Every clause except the devnet e2e holds today; the e2e is the outstanding item.
Wallet creation, faucet funding, sending and keyfile export are all exercised by
the CLI and by hand on the hosted testnet, but not by a browser test in CI.

## 5. Workstream B -- variant assets (shipped in 0.24.0, no protocol bump)

### 5.1 The fork that was not needed

The plan called for `PROTOCOL_VERSION` 2, every domain moving from `sccp/v1/...`
to `sccp/v2/...`, regenerated genesis files and a testnet reset. None of that
happened, and it should not have: the shipped change is additive.

- `PROTOCOL_VERSION` stayed 1 and `version.py: DOMAIN_PREFIX` still yields
  `sccp/v1/...` for every domain, including the new asset, pool and HTLC ids.
- The new transaction types are enum members with their own `ALLOWED` key sets,
  so a node that predates them rejects an unknown `type` at schema time instead
  of misreading it -- the protection the fork was meant to buy.
- `State` gained `minted_assets`, `pools` and `htlcs`, and the keyed-by-id checks
  in `State.__post_init__` cover them, but `Account.balance` and every v1 field
  kept its meaning, so a v1 block is still a valid prefix of the chain and
  genesis did not have to move.

What the plan got right is the discipline around payload keys: `_check_unknown_keys`
is strict, so a new field is still a protocol release. What it got wrong is that
the v1 envelope could not absorb the new types -- it could, because a transaction
type is an enum value, not a rule change.

### 5.2 Account and supply accounting

The map beside the native balance is what shipped:

```python
@dataclass
class Account:
    ...
    balance: int                        # native SCCP, unchanged
    immature: int                       # native coinbase not yet mature
    assets: dict[str, int]              # asset id (hex) -> units
    immature_assets: dict[str, int]     # variant coinbase not yet mature
```

`balance_of` / `asset_balance` and `credit` / `debit` keep the proof, fee and
coinbase code at one call site per balance change instead of knowing about the
map. The running total is `State.minted_assets`, a per-asset counter that genesis
seeds with the premine and the coinbase adds to.

There is no per-asset `burned` counter, and the plan's invariant
`sum(balance_of(a, asset)) + burned == minted <= max_supply` is not checked on
every block. It could not be, as stated: a bonded balance is held outside
`balance`, and `slash_evidence` destroys a witness bond outright
(`chain/timeout.py: apply_slash`). The cap is enforced where the token is
created instead -- `variant_subsidy` clamps every mint to
`max_supply - minted_assets[asset]` (`chain/block.py`) -- while the load-time
checks in `State.__post_init__` and the per-op checks in `chain/dex.py` keep
reserves, shares and keys well formed. Snapshot and restore carry the new maps;
the state root changed when the fields landed, so the switchover was a breaking
state change rather than a payload addition, and the golden chain, the pinned
genesis hashes and the web vectors were regenerated with it (0.24.0 and 0.25.0).

0.33.0 adds a **native-only** `State.burned` display counter for the timeout
stall slash and the clock-witness bond slash. It is persisted in snapshots and
reported as `burned` by `/v1/status`, but it is deliberately not a `state_root`
leaf: the reduced account balances already commit the burn, so adding the counter
must not move the consensus root. A snapshot written before the field restores it
as zero; a fresh replay from genesis counts every historical slash.

### 5.3 Transaction payloads

| Type | Change | Payload |
| --- | --- | --- |
| `transfer` | extended | `{to, amount, public_key, asset?}`; the omitted `asset` means native SCCP. No `memo` field was added -- the payload is strict, so a memo would be a protocol release |
| `create_pool` | new | `{asset_a?, amount_a, asset_b?, amount_b, public_key}`; the pair is ordered by asset id, and the fee is the network's `dex_fee_bps`, not a pool field |
| `add_liquidity` | new | `{pool_id, amount_a, amount_b, public_key}` |
| `remove_liquidity` | new | `{pool_id, shares, public_key}` |
| `swap` | new | `{pool_id, asset_in?, amount_in, min_amount_out, public_key}` |
| `htlc_lock` | new | `{hashlock, timelock_height, beneficiary, amount, public_key, asset?}` |
| `htlc_claim` | new | `{htlc_id, preimage, public_key}` |
| `htlc_refund` | new | `{htlc_id, public_key}` |
| `register` | extended | `{public_key, endpoint, engine, bond, variant?}`; `variant` defaults to `standard` and is fixed until the account unregisters |
| `availability` | unchanged | `{epoch, max_games, commitment, public_key}`; the variant comes from the account, so a registration plays one discipline |

There is deliberately **no `create_asset` transaction.** The asset set is fixed by
`NetworkParams`, which is what makes a variant token a claim on a game corpus
rather than a promise by an issuer, and it removes lookalike and spam assets by
construction. New assets are a protocol release, not a transaction.

### 5.4 Variants in consensus

`NetworkParams` gained `variants: tuple[VariantParams, ...]`, with standard chess
first and unchanged. As shipped (`config.py`), a variant is:

```python
@dataclass(frozen=True)
class VariantParams:
    name: str                 # "standard", "chess960"
    rules: str                # "chess" | "chess960" -- validated
    asset: str | None         # asset id hex; None for standard, which mints SCCP
    symbol: str               # "C960"
    display_name: str
    decimals: int
    subsidy: int
    halving_interval: int | None
    max_supply: int | None    # set together with halving_interval
    min_plies: int
    max_games_per_block: int  # declared and served, not consulted by consensus
    opening_book: str         # book id, or "none"
```

Rules that follow from it:

- **Proofs carry a variant.** It is a field of the game-hash payload, so the same
  PGN cannot be replayed under a different rule set: `chess` and `chess960`
  produce different game hashes and the dedupe set is keyed by that hash.
- **A registration is one discipline.** `availability` does not enumerate
  variants; the account's `variant` decides which games its seat may be matched
  for, and switching means unregistering first. `MinerRating` stays a single
  Glicko-2 figure per address, and every leaderboard row names the account's
  registered variant so a reader can tell which discipline the number came from
  (`PROTOCOL.md`, ratings). Per-variant ratings were planned and not built: with
  one registration per address there is nothing to key a second number on yet.
- **Coinbase is per variant.** For every variant represented in a block, one
  variant subsidy is minted (`minted_variant_rewards`) and declared in the
  coinbase payload's `variant_rewards` map, paid to the block miner exactly as
  the native subsidy is; a block that settles only variant games mints that asset
  and no SCCP. Standard's output is unchanged from v1.
- **Block capacity is `wins_required`, not per variant.** A block must carry
  exactly the scheduled number of proofs (or be an allowed proofless block that
  carries exactly one transaction), so how much of a variant reaches the chain is
  decided by k and by the roster, not by `max_games_per_block`. That field is
  declared, validated and published in `/v1/params`, and no consensus check reads
  it -- a live knob that does nothing yet, flagged in
  [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).
- **Endowments were not built.** No variant parameter pays a founding account
  over `endowment_drain_epochs`; issuance is the subsidy schedule alone.
- **Fees stay in SCCP.** A variant asset is a reward and a store of the corpus,
  not a fee unit; one fee market keeps mempool policy and miner economics
  unchanged.

### 5.5 Engines and tooling

- `data/engines.json` still pins Stockfish only (`stockfish-19`, `stockfish-18`,
  plus the test mock); there are no `fairy-stockfish` entries, and Chess960 runs
  the pinned Stockfish with `UCI_Chess960=true`. Crazyhouse would need the fairy
  build, so it is not a parameter change but a new engine profile plus a book and
  a legality rule set.
- `sccp miner` gained `--variant` (and `sccp miner register` with it), which is
  how the hosted testnet runs two standard and two Chess960 miners from one
  compose file (`docker-compose.yml`).
- The explorer names the variant on every games row, on the game detail page and
  in the account view, and `GET /v1/games`, `/v1/blocks` and `/v1/params` all
  carry it, which is what `sccp chain account`, `sccp chain status` and the web
  client read.
- `scripts/play_miner.py` and `scripts/export_block_games.py` were **not**
  extended: neither takes a variant, so a locally played or exported game is
  standard chess. Both are developer conveniences, and the gap is noted in
  [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

### 5.6 Done when

A two-variant devnet (standard + chess960) settles proofs of both kinds in the
same blocks, `GET /v1/accounts/{address}` reports SCCP and the variant asset,
coinbase amounts match the per-variant schedule, a standard game is rejected when
submitted as Chess960, and the full suite plus the regenerated golden chain is
green.

That holds for standard + Chess960 on the hosted testnet and in the fixtures. The
Crazyhouse half of the sentence is not reachable yet: no `fairy-stockfish` entry,
no book, and `VariantParams.rules` accepts only `chess` and `chess960`.

## 6. Workstream C -- native DEX (shipped in 0.24.0)

The DEX trades the assets the chain mints: SCCP against variant assets, and
variant assets against each other. It has no admin key, no pause, no fee switch
and no upgrade path; changing any of those is a fork, exactly like a consensus
rule. What shipped is a sequential constant-product AMM with a floor; the batch
clearing model that was planned for 0.24.0 is not in it (section 6.2).

### 6.1 Pools as consensus objects

As shipped (`chain/dex.py`, `State.pools`):

- **Constant product** with reserves `(r_a, r_b)`. The fee is **not** a pool
  parameter: it is the network's `dex_fee_bps` (30 on every shipped network), so
  the pool id is `H("sccp/v1/pool", {asset_a, asset_b})`, computed with the pair
  in lexicographic asset-id order and `""` for native SCCP. A pair therefore
  identifies exactly one pool, and there is no way to create a second one at a
  different fee level -- a fee change is a network parameter change, not a pool
  choice.
- **LP positions are share counts inside the pool**: `Pool.shares: dict[address,
  int]` against `Pool.total_shares`, not an entry on `Account`. They are not
  transferable -- `remove_liquidity` only burns the sender's own shares and pays
  both reserves pro rata -- so an LP position cannot be sold or used as collateral.
  The asset universe stays closed to game-minted tokens.
- **First deposit** mints `isqrt(amount_a * amount_b)` shares and locks nothing:
  the `MIN_LIQUIDITY` burn the plan proposed was not built. The griefing case it
  addressed (donate one unit so later deposits round to zero shares) is bounded
  instead at validation: a first deposit that would mint zero shares, or a later
  deposit that would mint zero, fails as `the deposit is too small to mint a
  share`. A griefer can only make their own deposit unprofitable.
- **Rounding is in the pool's favour, and the invariant holds by construction.**
  `swap_output` floors, `added_shares` takes the minimum of the two sides,
  `removed_amounts` floors, a swap whose output floors to zero is rejected, and a
  redemption that would pay zero of either side is rejected. There is no per-block
  sweep over pools comparing `k`; each operation recomputes reserves from the
  integer formulas above, which is why the audit surface is the four functions in
  `chain/dex.py` and not the block.
- **There is no `deadline_height`**, on swaps or on liquidity changes. A swap
  carries `min_amount_out` only, so a transaction that sat in a mempool executes
  at whatever price the block it lands in produces, bounded solely by that floor.
  This is the largest design-versus-code gap in the DEX; closing it means a new
  payload field, which the strict payload check turns into a protocol release. It
  is written up in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

### 6.2 Clearing model: what shipped

The plan above describes a **batch auction**: every `swap` for a pool in a block
nets against the pool's pre-block reserves and settles at one clearing price, with
a bounded drop-recompute loop over orders that miss `min_out`, plus an optional
`swap_commit`/`swap` pair that hides intent for large orders.

The shipped chain does none of that. Transactions are applied **in the order the
block lists them**, one at a time, each against the reserves the previous one
left, and the only protection a swapper has is `min_amount_out`. That is enough
to make a sandwich unprofitable to force through a floor, but it does not give the
two properties the auction was for:

- a swap included later in the same block pays a price the earlier swaps moved,
  so ordering inside a block is worth something to whoever builds it;
- pre-trade visibility is total -- anybody watching the mempool sees the size and
  the floor before the block exists.

Sequential execution is also why `min_amount_out` cannot be replaced by
`deadline_height` alone. A deadline bounds *when* a swap may execute; it does not
bound the price drift between quote and fill. Both fields belong in a batch model;
in a sequential one, only the floor does anything, which is why it shipped first
and the deadline did not.

Deferring the auction is a deliberate trade, not an oversight: the constant-product
code is ~120 lines with four integer functions to audit, while a clearing model
adds a solver, a deterministic iteration order, a commit-reveal mempool path and a
new family of tests for the iterate-until-stable loop. What the chain has today is
correct per swap and order-sensitive per block. Making it order-insensitive is the
single largest open design item, and
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) tracks it as such rather than as
a bug.

### 6.3 API

| Endpoint | Returns | Status |
| --- | --- | --- |
| `GET /v1/params` | the fixed asset list, each variant with id, symbol, name, decimals, supply and schedule | shipped; there is no separate `/v1/assets`, because the asset set *is* a network parameter |
| `GET /v1/pools` | every pool with `asset_a`/`asset_b` (`null` = native), reserves, `total_shares`, `fee_bps` and the full `shares` map | shipped |
| `GET /v1/pools/{id}` | one pool | shipped |
| `GET /v1/pools/{id}/quote?asset_in&amount_in` | `amount_out`, fee, price, height and a suggested `min_amount_out` | **not built** -- the wallet imports the same curve in `lib/dex.ts` and quotes locally against committed reserves, which needs one `GET /v1/pools` instead of one request per keystroke |
| `GET /v1/blocks/{reference}` | the block, its position and every transaction payload, swaps included | shipped, but with no `swaps`/`pool_deltas` summary: the explorer reads a block's swap payloads and asks `/v1/pools` for the pair, so there is no per-block price series |
| `GET /v1/htlcs`, `/v1/htlcs/{id}` | locks by account or hashlock, and one lock | shipped |

Client-side quoting is exact rather than approximate: `swap_output` is four
integer operations, and `lib/dex.test.ts` pins the TypeScript port against the
Python one. The trade is that a quote carries no height stamp, so the wallet
re-reads `/v1/pools` when the panel loads and labels the impact it computed, and
the chain enforces the floor at execution. `scripts/make_openapi_fixture.py`
regenerates `tests/fixtures/openapi.json`, and
`tests/unit/test_api.py::test_openapi_contract_has_not_changed` fails until it is
regenerated deliberately.

### 6.4 Wallet and explorer

Shipped (`views/dex`, `views/Dex.tsx`):

- **Swap panel**: pick a pool and a side, see the quoted output, the minimum
  received at the chosen slippage (50 bps by default) and the review screen with
  every payload field before signing a `swap` carrying that `min_amount_out`. The
  quote is computed in the tab from `GET /v1/pools`; there is no price-impact
  figure, only the difference between the quoted output and the spot ratio implied
  by the reserves.
- **Liquidity panel**: add and remove liquidity, with the minted or burned share
  count shown before signing.
- **Create-pool panel**: two tokens and both amounts; the pair is ordered for the
  user and the derived pool id is shown before signing.
- **Pools list** on `#/dex` from `GET /v1/pools`, polled every 5 s, with each
  pool's reserves and the unlocked account's shares.

Not built: 24 h volume, fee APR, a price sparkline from `pool_deltas`, and the
corpus metrics from section 3.3 beside the market data. All four need per-block
pool history -- i.e. the indexer the explorer does not have -- so the page shows
exactly what the current state says.

### 6.5 Spam and safety

- **`create_pool` is free** beyond the transaction fee. The planned
  `pool_creation_fee` (burned) was not built, and no code path charges for a new
  pool. The deterrent that exists is economic rather than administrative: a pool
  needs real deposits on both sides, and only the closed set of network assets can
  be deposited, so a spam pool costs its creator a transaction fee plus liquidity
  that earns nothing until someone trades against it. If spam ever appears, the fee
  is the fix, and adding it means a new network parameter plus a charge in
  `_apply_create_pool` -- a protocol release.
- **No oracle.** Prices are pool ratios; the UI shows the reserves next to the
  quote rather than calling any number a market price.
- **No verified-asset list, and none needed.** The token list is built from
  `/v1/params`, so a symbol exists only because the network defines it. A
  lookalike has no way into the UI, which is stronger than a hint keyed by id.

### 6.6 Done when

On devnet a wallet swaps SCCP <-> C960 through the UI, quotes match execution to
the unit at the quoted reserves, `k` never decreases across a randomised property
test, and removing all liquidity after fee accrual returns the expected amounts to
the unit.

That holds on the hosted testnet and in `tests/unit/test_dex.py` and
`lib/dex.test.ts`. The clause the plan also had -- batch clearing resisting an
in-block sandwich, tested with a deliberately front-run order -- does not apply,
because sequential execution is what shipped (section 6.2). CZH is replaced by
C960 throughout: Crazyhouse is not a variant the chain settles.

## 7. Workstream D -- ETH<->SCCP bridge (shipped in 0.24.0)

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

- `htlc_id = H("sccp/v1/htlc", {from, nonce})`; the locked funds move out of the
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
  snapshot; `GET /v1/accounts/{address}` carries the account's locks as `htlcs`,
  `GET /v1/htlcs?address=&hashlock=` lists them, and `GET /v1/htlcs/{htlc_id}`
  reads one.

Shipped in 0.24.0 and exercised end to end on the hosted testnet
(`docs/guides/TEST_DEVNET_BRIDGE_DEX.md`): a lock, a claim with the preimage and a
refund after the timelock each have a unit test, a wallet flow and a row in the
bridge activity panel. Both close paths charge the network fee, and a native claim
or refund may pay it out of the amount being released, so a taker holding no SCCP
at all can still complete a buy (section 7.6).

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
- `contracts/test/EthHtlc.t.sol` under Foundry (7 tests: lock then claim pays the
  beneficiary, refund only after the timelock and only by the depositor, a wrong
  preimage, a claim after the timelock, a double close, a bad lock, and stray
  ether), `contracts/script/Deploy.s.sol`, and an external audit before mainnet --
  the audit is **not done**, and it is a precondition for mainnet, not for testnet.

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
rule is always: **the second leg expires first, leaving the party that locked it
time to claim the first leg.** A second leg that outlives the first lets the
first party wait out the ETH timelock, claim SCCP at its last valid height and
then refund the ETH.

```text
T_sccp <= T_eth - margin      margin = 20 blocks on mainnet (~200 min)
                              margin = 24 blocks on devnet/testnet
```

`T_sccp` is converted to a wall-clock estimate with the SCCP
`target_block_seconds`; the taker picks the height and the maker refuses a take
whose height leaves less than the margin.

Direction A -- an ETH holder buys SCCP from a miner:

1. The maker (miner) publishes a signed sell order (section 7.5).
2. The taker locks ETH in `EthHtlc` with hashlock `H = sha256(s)`,
   timelock `T_eth`, beneficiary = maker's ETH address.
3. The maker verifies the Ethereum lock, then locks SCCP with the same `H`,
   timelock `T_sccp <= T_eth - margin`, beneficiary = taker's SCCP address.
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
  "network": "mainnet",
  "maker_sccp": "sccpt1...", "maker_eth": "0x...",
  "give_asset": "SCCP", "give_amount": 2500000000,
  "want_asset": "ETH",  "want_amount": "1000000000000000",
  "min_sccp_timelock": 3620, "expiry": 1790000000,
  "nonce": 7, "public_key": "01...", "signature": "01..."
}
```

The maker signs `H("sccp/v1/order", body)` with the hybrid key, so an order is
verified with the same code path as a transfer and cannot be forged or
tampered with. The node exposes `POST /v1/orders`, `GET /v1/orders` (filter by
pair), `GET /v1/orders/{order_id}` and `POST /v1/orders/{order_id}/takes`; a take
is signed by the taker and is recorded once, so an order can be taken exactly once
and the record of who took it is what the two sides show each other. There is **no
cancel endpoint**: an order lives until `expiry`, and a maker who wants out simply
stops advertising it and lets it lapse. The order book is **not consensus**: it
holds no funds, it can be mirrored by anyone, and losing it costs nothing but a
re-post. A restart loses the in-memory book and every maker re-posts.

Amounts are integers in each chain's smallest unit -- 8 decimals on SCCP, 18 on
Ethereum -- and the price is implicit in the pair, so there is no oracle and no
rounding rule to argue about. The ETH amount is carried as a decimal string
because 18 decimals exceed JavaScript's safe integer range; SCCP amounts stay
JSON integers. `expiry` is Unix seconds and `min_sccp_timelock` is an SCCP block
height, because the two chains share no clock: `sccp_timelock_height` turns the ETH
timestamp into the latest safe height using `target_block_seconds` and
`sccp_margin`, and both the browser and the node refuse a take below it. Orders are
whole-order: one take, one fill. A maker bot re-quotes rather than tracking partial
fills, which keeps the book a list of firm quotes instead of a second, un-audited
matching engine.

### 7.6 User and miner flows

**Buying SCCP with ETH.** The wallet's bridge view (`views/bridge/BuyPanel.tsx`)
lists open sell orders with the price in ETH per SCCP and the maker; choosing one
runs the flow of section 7.4 with every step signed locally: lock ETH in `EthHtlc`
through the browser wallet and post the take, wait for the maker's SCCP lock, then
claim SCCP with the preimage -- or refund the ETH once the lock has expired. Both
close buttons are always visible with the condition that enables them, and the
taker never needs SCCP in advance (section 7.2).

**Selling mined SCCP.** `views/bridge/SellPanel.tsx` publishes a signed order from
an unlocked account, lists the account's orders with their take and both legs, and
drives the maker side: verify the taker's ETH lock on chain, lock SCCP with a
timelock at most `sccp_margin` blocks before the ETH expiry, then claim the ETH
with the preimage once the taker reveals it -- or refund the SCCP if the take never
arrives.

`views/bridge/ActivityPanel.tsx` shows every lock the account is party to, its
status and its timelock, which is what makes a half-finished swap legible instead
of invisible.

Not built: the `sccp mm` market-maker bot and `sccp miner --auto-sell`. Both were
planned as convenience wrappers over the same signed orders, and neither is needed
to complete a swap -- a maker posts and drives the legs from the bridge view. If
automation is added later it should sign with the same keyfile and reuse
`bridge/orders.py` rather than re-implement the protocol; nothing in the chain
changes.

### 7.7 Later, and optional: wrapped ETH

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
wallet's bridge view recovers correctly from a page reload at every step.

What shipped matches the first four clauses: `tests/unit/test_bridge_orders.py`
covers the order and take validations (expiry, replay, margin, one-take),
`tests/unit/test_bridge_eth.py` covers the RPC and lock-verification path against
an Anvil-shaped stub, `tests/unit/test_wallet_htlc.py` covers the browser builders,
and the Foundry suite passes with an `anvil` deployment in the testnet runbook. The
static analysis run and the external audit are **outstanding**, and both gate
mainnet rather than testnet. Page-reload recovery is manual, not Playwright.

## 8. Release train

The train below is history up to the current tag: every row through 0.29.0 is
released, and each is what `CHANGELOG.md` records rather than what was planned.
The remaining rows are the open ones.

| Version | Contents | Gate | Status |
| --- | --- | --- | --- |
| 0.19.0 | this document | reviewed; no code change | shipped (docs only) |
| 0.22.0 | web wallet: keyfile, send, receive, faucet, history | vectors, vitest; keyfile round-trips with the CLI | shipped; Playwright e2e not built |
| 0.23.0 | game pagination, Live games page, relay status filter | vitest; cursor paging across a multi-proof block | shipped (the Live page was removed again in 0.27.0) |
| 0.24.0 | variant assets and Chess960 settlement, native DEX, HTLC bridge and order book | property tests for `k` and rounding; adversarial swap suite; C960 proofs against a C960 roster | shipped; no batch clearing (§6.2); bridge audit open |
| 0.25.0 | genesis premine and testnet bootstrap, seeded DEX and bridge state | pinned mainnet/testnet genesis hashes; cap accounting per asset | shipped |
| 0.26.0 | hosted app and landing page on two origins | deploy smoke tests; cutover and rollback documented | shipped |
| 0.27.0 | Chess960 miners start and settle; explorer loses Live, "Ratings" naming | C960 start through a real Stockfish; a signed C960 proof in a block | shipped |
| 0.28.0 | whole PGNs, slimmer header | vitest; C960 and standard game pages render their moves | shipped |
| 0.29.0 | keyfile import from disk, honest swap rows, bridge/DEX notebook panels | vitest; a CLI keyfile opens from the picker; a swap names both legs | shipped |
| 0.30.0 | clock witnesses and bonded timeout claims, two extra role seats (slow, GNU Chess), a deploy gate that proves them | vitest; pytest; a redeploy seats both roles through `/stack/synthetic-fleet.json` | shipped |
| unscheduled | `wETH` federation (optional) | second audit; consensus caps; explicit trust disclosure | not started |
| 1.0.0 | mainnet | the go/no-go in `IMPLEMENTATION_PLAN.md`, plus the bridge audit | open |

Each release keeps `make check` green, regenerates the vectors and OpenAPI
fixtures it changes, and updates `PROTOCOL.md`, `README.md` and the operator
guides in the same commit, which is the convention the repository already
follows. A release that adds a page also adds its route to the smoke tests, which
is how the missing pieces above (Playwright, an indexer) stay visible instead of
being quietly assumed.

## 9. Architecture at a glance

```text
                        +---------------------------+
   browser wallet  ---> |  sccp node (REST only)    | <--- engine miners
   (hybrid keys)        |  /v1/tx /v1/blocks        |      (standard, chess960)
                        |  /v1/accounts /v1/pools   |
                        |  /v1/orders /v1/htlcs     |
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
| DEX | in-block sandwich | `min_amount_out` per order (no batch clearing was built) | 6.2 |
| DEX | rounding or first-LP price setting | pool-favoured rounding, a zero-share deposit rejected, and no per-block `k` check needed because each path recomputes reserves | 6.1 |
| DEX | stale execution after a mempool delay | **not mitigated**: there is no `deadline_height`; only the output floor binds | 6.1 |
| DEX | pool spam | closed asset universe and the liquidity a pool needs; no creation fee was built | 6.5 |
| Bridge | one party stalls | timelocks on both legs, refunds signed by the party that locked | 7.4 |
| Bridge | reorg after the second leg locks | second leg waits `reorg_depth + 1` confirmations | 7.4 |
| Bridge | order replay or redirection | `nonce`, `expiry`, both addresses bound in the signed body | 7.5 |
| Bridge | a bug in the Ethereum contract | no admin, no proxy, small surface, Foundry suite, external audit (outstanding) | 7.3 |
| Bridge | a weak classical leg | disclosed in the UI: the SCCP side is post-quantum, the ETH side is not | 7.3 |
| Assets | consensus divergence or asset inflation | per-asset cap clamped at mint, `minted_assets` in state, variant inside the game-hash domain | 5.1, 5.2 |
| Assets | a standard game replayed as a variant | variant inside the game hash domain; per-variant dedupe | 5.4 |
| Mining | an unpinned engine | URL plus SHA-256 pins in `data/engines.json`, verified at fetch | 5.5 |

## 11. Non-goals

- **EVM compatibility.** No `eth_*` endpoints, no chain-id registration, no
  ERC-20 or Solidity on the SCCP node, no MetaMask network switch. The one
  Solidity file is the counterparty HTLC contract and it runs on Ethereum.
- **Permissionless asset issuance.** No `create_asset`, no ICO and no token
  sale. Assets are protocol-defined; the only pre-issued supply is the fixed,
  public genesis allocation described in
  [`PROTOCOL.md` §13](PROTOCOL.md#13-genesis-documents).
- **A custodial bridge or deposit addresses.** The HTLC path needs neither. An
  exchange that wants to list SCCP integrates against the same transactions.
- **An AMM over external assets before `wETH`.** No oracle, and no wrapped asset
  in the shipped DEX: pools hold only the tokens the chain mints.
- **Bughouse and Go in 1.x.** Bughouse is four seats across two boards and needs
  a different match object; Go is a different game protocol entirely.
- **Cross-chain swaps beyond Ethereum,** at least until the ETH path is audited.
  The same HTLC protocol extends to any chain with hash locks and timelocks, one
  contract each; that is future work with no new design.

## 12. Open decisions

The recommendation column is what the shipped code does today unless the last
column says otherwise.

| # | Decision | Options | Recommendation | Settled? |
| --- | --- | --- | --- | --- |
| 1 | Variant supply | own cap per variant / share of SCCP's 21M | own cap; leave SCCP untouched | yes, per-variant `VariantParams` |
| 2 | Are variant assets fee units? | yes / no | no: one fee market, in SCCP | yes, every fee is native |
| 3 | Chess960 | separate asset / a parameter on SCCP | separate asset `C960`, Stockfish with `UCI_Chess960` | yes, shipped |
| 4 | Endowment payout | linear per block / per epoch / manual | per epoch, `endowment_drain_epochs`, off by default | **no -- not built** |
| 5 | LP positions | non-transferable shares / an LP token asset | non-transferable | yes, `Pool.shares` |
| 6 | Swap privacy | batch only / batch plus commit-reveal | batch default, commit-reveal above a size threshold | **no -- neither built** |
| 7 | First bridge chains | Ethereum mainnet / an L2 | mainnet plus one L2 (Base), because claim gas dominates small trades | open; the mapping that exists is devnet→Anvil 31337, testnet→Sepolia 11155111, mainnet→Ethereum 1 |
| 8 | Timelock margin | 10 / 20 blocks on mainnet | 20 blocks (~200 min), 24 on devnet/testnet | yes, `sccp_margin` per environment |
| 9 | Order transport | node REST / gossip | REST first, gossip later, same signed body | yes, REST only |
| 10 | Symbols | ticker collision handling | symbol is display only; the token list is keyed by asset id | yes, `lib/assets.ts` |

## 13. References

- [`PROTOCOL.md`](PROTOCOL.md) -- section 2 (canonical game hash and the variant
  dimension it carries), 3 (PGN contract), 10 (transactions, including the pool,
  swap and HTLC payloads), 11 (ledger state: assets, pools, htlcs), 12 (blocks and
  fork choice), 14 (node API), and 9.2 (why quality is not arbitrated on chain).
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) -- what is left on the chain
  as it stands, and the mainnet go/no-go.
- [`THREAT_MODEL.md`](THREAT_MODEL.md) -- the adversary model behind section 10.
- [`WHITEPAPER.md`](WHITEPAPER.md) -- how the current implementation works end to
  end.
- `CHANGELOG.md` -- the release record this document's statuses are drawn from.
- `src/sccp/chain/tx.py` -- the transaction envelope and the `ALLOWED` payload
  set, which the DEX and bridge types extend.
- `src/sccp/chain/dex.py` -- the four integer functions behind section 6.1.
- `src/sccp/crypto/wallet.py` -- the keyfile format the browser keystore ports.
- `src/web/src/lib/keys.ts`, `src/web/src/lib/tx.ts` -- the in-browser hybrid
  signer and the transaction builders.
- `scripts/make_web_vectors.py` -- the generator that keeps the TypeScript ports
  byte-identical to Python for the transaction bodies it covers.

