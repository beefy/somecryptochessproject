"""Wallet service: balances, transfers, staking and history (PROTOCOL.md).

This is the read/build layer the CLI and miner use. It never holds a decrypted
key longer than one call: callers pass a :class:`~sccp.crypto.keys.Keypair` (or a
:class:`~sccp.crypto.pq.HybridKeypair`) and get a signed, canonical transaction
back. The scheme is carried by the key itself, so ``build_*`` needs no flag.

Everything is derived from a :class:`~sccp.chain.state.State`, so the same calls
work against the tip, a fork or a test fixture. History additionally needs a
chain, because the state alone keeps balances, not the transactions that produced
them.

A transfer needs two things from that state -- the spendable balance and the next
nonce -- so :meth:`WalletService.from_account_view` rebuilds them from one node's
``GET /v1/accounts/{address}`` answer. A wallet can then be used against a node
whose chain it has never synced.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from sccp.chain import dex
from sccp.chain.chain import Chain
from sccp.chain.state import Account, HtlcRecord, State, epoch_of
from sccp.chain.tx import sign_transaction, txid
from sccp.config import NetworkParams, format_coins, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import HybridKeypair
from sccp.errors import ConfigError, StateError, WalletServiceError
from sccp.protocol.models import Transaction, TxType

DEFAULT_SOURCE = "the local chain in --data-dir"
"""Where a wallet's state came from, when the caller does not say."""


def derive_address(keypair: HybridKeypair, params: NetworkParams) -> str:
    """The bech32 address of ``keypair`` on ``params``."""
    return encode_address(keypair.public_key_bytes, params)


@dataclass(frozen=True, slots=True)
class Balance:
    """A full picture of one account's funds."""

    address: str
    spendable: int
    immature: int
    bonded: int

    @property
    def total(self) -> int:
        """Everything the account owns."""
        return self.spendable + self.immature + self.bonded


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One transaction that touched an address."""

    txid: str
    height: int
    type: TxType
    delta: int
    fee: int
    counterparty: str | None = None


class WalletService:
    """Balance, transfer and staking helpers over one ledger state."""

    def __init__(
        self,
        params: NetworkParams | str,
        state: State,
        *,
        chain: Chain | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> None:
        self.params = get_network(params)
        self.state = state
        self.chain = chain
        self.source = source
        """Where the state came from, quoted in refusals so the user can check it."""

    @classmethod
    def from_account_view(
        cls,
        params: NetworkParams | str,
        document: Mapping[str, object],
        *,
        height: int = 0,
        source: str = "the node's account view",
    ) -> WalletService:
        """A wallet over one account exactly as a node reported it.

        A transfer needs a spendable balance and the next nonce, and both are in
        ``GET /v1/accounts/{address}``: the blocks that produced them are not
        consulted, so no chain database and no sync is needed. The document is
        validated against ``params``, which is what catches a ``--node`` that
        serves another network. The result has no chain, so :meth:`history` is
        not available on it.
        """
        network = get_network(params)
        reported = document.get("network")
        if not isinstance(reported, str):
            raise WalletServiceError("the node's account document names no network")
        if reported != network.network_id:
            raise WalletServiceError(
                f"the node serves {reported}, not {network.network_id}: pass --network "
                f"{reported} with a {reported} keyfile, or point --node at a "
                f"{network.network_id} node"
            )
        address = document.get("address")
        if not isinstance(address, str):
            raise WalletServiceError("the node's account document names no address")
        try:
            account = Account.from_dict(document)
        except StateError as exc:
            raise WalletServiceError(f"the node's account document is unreadable: {exc}") from exc
        return cls(
            network,
            State(network, height=height, accounts={address: account}),
            source=source,
        )

    # -- reads --------------------------------------------------------------

    @property
    def network(self) -> str:
        """The network id of this service."""
        return self.params.network_id

    def address_for(self, keypair: HybridKeypair) -> str:
        """The bech32 address of ``keypair`` on this network (0x00 or 0x01)."""
        return derive_address(keypair, self.params)

    def account(self, address: str) -> Account:
        """The account at ``address`` (a zeroed one if it never appeared)."""
        self._require_address(address)
        return self.state.account(address)

    def balance(self, address: str) -> Balance:
        """Spendable, immature and bonded balances for ``address``."""
        account = self.account(address)
        return Balance(
            address=address,
            spendable=account.balance,
            immature=account.immature,
            bonded=account.bonded,
        )

    def next_nonce(self, address: str) -> int:
        """The nonce the next transaction from ``address`` must use."""
        return self.account(address).nonce

    def required_fee(self, fee: int | None) -> int:
        """``fee`` if given and at least the network minimum, else the minimum."""
        if fee is None:
            return self.params.min_fee
        if fee < self.params.min_fee:
            raise WalletServiceError(
                f"fee {fee} base units is below the network minimum of "
                f"{self.params.min_fee} base units ({format_coins(self.params.min_fee)} coins)"
            )
        return fee

    def history(self, address: str) -> list[HistoryEntry]:
        """Every transaction in the adopted chain that touched ``address``."""
        if self.chain is None:
            raise WalletServiceError("history needs a chain, not just a state")
        self._require_address(address)
        entries: list[HistoryEntry] = []
        for stored in self.chain.main_chain():
            for tx in stored.block.transactions:
                entry = self._history_entry(tx, stored.height, address)
                if entry is not None:
                    entries.append(entry)
        return entries

    # -- helpers ------------------------------------------------------------

    def _resolve_nonce(self, sender: str, nonce: int | None) -> int:
        expected = self.next_nonce(sender)
        if nonce is None:
            return expected
        if nonce != expected:
            raise WalletServiceError(f"{sender} must use nonce {expected}, not {nonce}")
        return nonce

    def _epoch_of_next_block(self) -> int:
        """The epoch a transaction built now will be included in.

        Epoch windows are checked against the block a transaction lands in, not
        the tip it was built against, and the two differ in the last block of an
        epoch. A transaction built against the tip would be one epoch early
        exactly when it matters most (boundary blocks).
        """
        return epoch_of(self.state.height + 1, self.params)

    def _require_address(self, address: str) -> None:
        if not validate_address(address, self.params):
            raise WalletServiceError(f"{address!r} is not a valid {self.params.network_id} address")

    # -- transaction builders ----------------------------------------------

    def build_transfer(
        self,
        keypair: HybridKeypair,
        to: str,
        amount: int,
        *,
        fee: int | None = None,
        nonce: int | None = None,
        asset: str | None = None,
    ) -> Transaction:
        """Build and sign a transfer, raising if the sender cannot afford it.

        ``asset`` accepts an asset id, a variant name or its symbol. Omitting it
        (or naming SCCP) transfers the native coin and keeps the protocol-v1
        payload byte-identical. A variant transfer still pays its fee in SCCP.
        """
        sender = self.address_for(keypair)
        self._require_address(to)
        if amount <= 0:
            raise WalletServiceError("a transfer amount must be positive")
        if to == sender:
            raise WalletServiceError("a transfer must name a different recipient")
        selected = None
        if asset is not None:
            selected = next(
                (
                    variant
                    for variant in self.params.variants
                    if asset in (variant.asset, variant.name, variant.symbol)
                ),
                None,
            )
            if selected is None:
                raise WalletServiceError(f"unknown asset {asset!r}")
        resolved_fee = self.required_fee(fee)
        if selected is not None and not selected.native:
            if self.state.asset_balance(sender, selected.asset or "") < amount:
                raise WalletServiceError(
                    f"{sender} has "
                    f"{self.state.asset_balance(sender, selected.asset or '')} "
                    f"{selected.symbol}, the transfer needs {amount}"
                )
            if self.state.spendable(sender) < resolved_fee:
                raise WalletServiceError(
                    f"{sender} cannot pay the {format_coins(resolved_fee)} SCCP fee"
                )
        else:
            total = amount + resolved_fee
            spendable = self.state.spendable(sender)
            if spendable < total:
                raise WalletServiceError(
                    f"{sender} has {format_coins(spendable)} spendable, the transfer needs "
                    f"{format_coins(total)} ({format_coins(amount)} plus a "
                    f"{format_coins(resolved_fee)} fee); this balance came from {self.source}"
                )
        payload: dict[str, object] = {
            "to": to,
            "amount": amount,
            "public_key": keypair.public_key_hex,
        }
        if selected is not None and not selected.native:
            payload["asset"] = selected.asset
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.TRANSFER,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload=payload,
        )
        return sign_transaction(keypair, tx)

    def _trade_asset(self, asset: str | None) -> str:
        """Resolve a pool side to an asset id; native SCCP is the empty string."""
        resolved = self._resolve_asset_name(asset)
        return dex.NATIVE_ASSET if resolved is None else resolved

    def _require_trade_balance(
        self,
        address: str,
        asset: str,
        amount: int,
        fee: int,
        what: str,
    ) -> None:
        """Require ``amount`` of ``asset`` and the SCCP fee for one pool side."""
        spendable = self.state.spendable(address)
        if spendable < fee:
            raise WalletServiceError(f"{address} cannot pay the {format_coins(fee)} SCCP fee")
        if asset == dex.NATIVE_ASSET:
            if spendable < amount + fee:
                raise WalletServiceError(
                    f"{address} has {format_coins(spendable)} spendable, {what} needs "
                    f"{format_coins(amount)} plus a {format_coins(fee)} fee"
                )
            return
        available = self.state.asset_balance(address, asset)
        if available < amount:
            raise WalletServiceError(
                f"{address} has {available} units of {asset}, {what} needs {amount}"
            )

    def _require_pool_id(self, pool_id: str) -> None:
        """Check a pool id has the 32-byte lowercase hex form the payload needs."""
        if len(pool_id) != 64 or pool_id != pool_id.lower():
            raise WalletServiceError("a pool id must be 32-byte lowercase hex")
        try:
            bytes.fromhex(pool_id)
        except ValueError:
            raise WalletServiceError("a pool id must be hexadecimal") from None

    def build_create_pool(
        self,
        keypair: HybridKeypair,
        *,
        asset_a: str | None,
        amount_a: int,
        asset_b: str | None,
        amount_b: int,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a ``create_pool`` with an initial two-sided deposit.

        Omit a side (or name native ``SCCP``) to pair the native coin with a
        variant asset. Amounts are base units. The chain orders the pair by
        asset id, and this builder does the same so the payload names the sides
        in the order the pool will store them.
        """
        sender = self.address_for(keypair)
        if amount_a <= 0 or amount_b <= 0:
            raise WalletServiceError("both initial pool amounts must be positive")
        resolved_a = self._trade_asset(asset_a)
        resolved_b = self._trade_asset(asset_b)
        if resolved_a == resolved_b:
            raise WalletServiceError("a pool needs two different assets")
        resolved_a, amount_a, resolved_b, amount_b = dex.normalize_pair(
            resolved_a, amount_a, resolved_b, amount_b
        )
        pool_id = dex.pool_id(resolved_a, resolved_b)
        if pool_id in self.state.pools:
            raise WalletServiceError(f"pool {pool_id} already exists")
        if dex.initial_shares(amount_a, amount_b) <= 0:
            raise WalletServiceError("the initial deposit is too small to mint a share")
        resolved_fee = self.required_fee(fee)
        self._require_trade_balance(sender, resolved_a, amount_a, resolved_fee, "the first side")
        self._require_trade_balance(sender, resolved_b, amount_b, resolved_fee, "the second side")
        payload: dict[str, object] = {
            "amount_a": amount_a,
            "amount_b": amount_b,
            "public_key": keypair.public_key_hex,
        }
        if resolved_a != dex.NATIVE_ASSET:
            payload["asset_a"] = resolved_a
        if resolved_b != dex.NATIVE_ASSET:
            payload["asset_b"] = resolved_b
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.CREATE_POOL,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload=payload,
        )
        return sign_transaction(keypair, tx)

    def build_add_liquidity(
        self,
        keypair: HybridKeypair,
        *,
        pool_id: str,
        amount_a: int,
        amount_b: int,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a proportional ``add_liquidity`` deposit."""
        sender = self.address_for(keypair)
        self._require_pool_id(pool_id)
        if amount_a <= 0 or amount_b <= 0:
            raise WalletServiceError("both liquidity amounts must be positive")
        pool = self.state.pools.get(pool_id)
        resolved_fee = self.required_fee(fee)
        if pool is None:
            if self.state.spendable(sender) < resolved_fee:
                raise WalletServiceError(
                    f"{sender} cannot pay the {format_coins(resolved_fee)} SCCP fee"
                )
        else:
            shares = dex.added_shares(
                pool.reserve_a,
                pool.reserve_b,
                pool.total_shares,
                amount_a,
                amount_b,
            )
            if shares <= 0:
                raise WalletServiceError("the deposit is too small to mint a share")
            self._require_trade_balance(sender, pool.asset_a, amount_a, resolved_fee, "the deposit")
            self._require_trade_balance(sender, pool.asset_b, amount_b, resolved_fee, "the deposit")
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.ADD_LIQUIDITY,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "pool_id": pool_id,
                "amount_a": amount_a,
                "amount_b": amount_b,
                "public_key": keypair.public_key_hex,
            },
        )
        return sign_transaction(keypair, tx)

    def build_remove_liquidity(
        self,
        keypair: HybridKeypair,
        *,
        pool_id: str,
        shares: int,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a ``remove_liquidity`` redemption of pool shares."""
        sender = self.address_for(keypair)
        self._require_pool_id(pool_id)
        if shares <= 0:
            raise WalletServiceError("liquidity shares must be positive")
        pool = self.state.pools.get(pool_id)
        if pool is not None:
            owned = pool.shares.get(sender, 0)
            if shares > owned:
                raise WalletServiceError(
                    f"{sender} has {owned} pool shares, cannot remove {shares}"
                )
            amount_a, amount_b = dex.removed_amounts(
                pool.reserve_a, pool.reserve_b, pool.total_shares, shares
            )
            if amount_a <= 0 or amount_b <= 0:
                raise WalletServiceError("the redemption is too small to pay both sides")
        resolved_fee = self.required_fee(fee)
        if self.state.spendable(sender) < resolved_fee:
            raise WalletServiceError(
                f"{sender} cannot pay the {format_coins(resolved_fee)} SCCP fee"
            )
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.REMOVE_LIQUIDITY,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "pool_id": pool_id,
                "shares": shares,
                "public_key": keypair.public_key_hex,
            },
        )
        return sign_transaction(keypair, tx)

    def build_swap(
        self,
        keypair: HybridKeypair,
        *,
        pool_id: str,
        asset_in: str | None,
        amount_in: int,
        min_amount_out: int,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a constant-product ``swap`` with a signed minimum out."""
        sender = self.address_for(keypair)
        self._require_pool_id(pool_id)
        if amount_in <= 0:
            raise WalletServiceError("a swap input must be positive")
        if min_amount_out < 0:
            raise WalletServiceError("a swap minimum output cannot be negative")
        resolved_in = self._trade_asset(asset_in)
        pool = self.state.pools.get(pool_id)
        if pool is not None:
            if resolved_in not in (pool.asset_a, pool.asset_b):
                raise WalletServiceError(f"pool {pool_id} does not trade asset {resolved_in!r}")
            native_in = resolved_in == pool.asset_a
            reserve_in = pool.reserve_a if native_in else pool.reserve_b
            reserve_out = pool.reserve_b if native_in else pool.reserve_a
            amount_out = dex.swap_output(
                reserve_in, reserve_out, amount_in, self.params.dex_fee_bps
            )
            if amount_out < min_amount_out:
                raise WalletServiceError(
                    f"the swap pays {amount_out}, below the requested minimum {min_amount_out}"
                )
            self._require_trade_balance(sender, resolved_in, amount_in, 0, "the swap input")
        resolved_fee = self.required_fee(fee)
        if self.state.spendable(sender) < resolved_fee:
            raise WalletServiceError(
                f"{sender} cannot pay the {format_coins(resolved_fee)} SCCP fee"
            )
        payload: dict[str, object] = {
            "pool_id": pool_id,
            "amount_in": amount_in,
            "min_amount_out": min_amount_out,
            "public_key": keypair.public_key_hex,
        }
        if resolved_in != dex.NATIVE_ASSET:
            payload["asset_in"] = resolved_in
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.SWAP,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload=payload,
        )
        return sign_transaction(keypair, tx)

    def _resolve_asset_name(self, asset: str | None) -> str | None:
        """Resolve an asset id, variant name or symbol to an id (native is None)."""
        if asset is None:
            return None
        for variant in self.params.variants:
            if asset in (variant.asset, variant.name, variant.symbol):
                return variant.asset
        raise WalletServiceError(f"unknown asset {asset!r}")

    def build_htlc_lock(
        self,
        keypair: HybridKeypair,
        *,
        hashlock: str,
        timelock_height: int,
        beneficiary: str,
        amount: int,
        fee: int | None = None,
        nonce: int | None = None,
        asset: str | None = None,
    ) -> Transaction:
        """Build and sign an ``htlc_lock`` transaction.

        ``hashlock`` is the SHA-256 of a 32-byte preimage, the same bytes the
        Ethereum contract checks. ``timelock_height`` is an absolute SCCP height;
        the transaction is invalid once the chain reaches it.
        """
        sender = self.address_for(keypair)
        self._require_address(beneficiary)
        if amount <= 0:
            raise WalletServiceError("an htlc amount must be positive")
        if len(hashlock) != 64 or hashlock != hashlock.lower():
            raise WalletServiceError("an htlc hashlock must be 32-byte lowercase hex")
        try:
            bytes.fromhex(hashlock)
        except ValueError:
            raise WalletServiceError("an htlc hashlock must be hexadecimal") from None
        if timelock_height <= self.state.height:
            raise WalletServiceError(
                f"the timelock {timelock_height} is not after the wallet's height "
                f"{self.state.height}"
            )
        selected = self._resolve_asset_name(asset)
        resolved_fee = self.required_fee(fee)
        if selected is None:
            if self.state.spendable(sender) < amount + resolved_fee:
                raise WalletServiceError(f"{sender} cannot lock {amount} plus a {resolved_fee} fee")
        else:
            if self.state.asset_balance(sender, selected) < amount:
                raise WalletServiceError(f"{sender} does not hold {amount} of {selected}")
            if self.state.spendable(sender) < resolved_fee:
                raise WalletServiceError(f"{sender} cannot pay the {resolved_fee} SCCP fee")
        payload: dict[str, object] = {
            "public_key": keypair.public_key_hex,
            "hashlock": hashlock,
            "timelock_height": timelock_height,
            "beneficiary": beneficiary,
            "amount": amount,
        }
        if selected is not None:
            payload["asset"] = selected
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.HTLC_LOCK,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload=payload,
        )
        return sign_transaction(keypair, tx)

    def _htlc_for(self, htlc_id: str) -> HtlcRecord:
        record = self.state.htlcs.get(htlc_id)
        if record is None:
            raise WalletServiceError(f"unknown htlc {htlc_id!r}")
        return record

    def build_htlc_claim(
        self,
        keypair: HybridKeypair,
        *,
        htlc_id: str,
        preimage: str,
        fee: int | None = None,
        nonce: int | None = None,
        record: HtlcRecord | None = None,
    ) -> Transaction:
        """Build and sign an ``htlc_claim`` that reveals ``preimage``.

        ``record`` lets a caller that read the lock from a node (which does not
        carry consensus state in an account view) supply the terms it is
        claiming. Without it the record is read from local state.
        """
        sender = self.address_for(keypair)
        stored = record if record is not None else self._htlc_for(htlc_id)
        if stored.htlc_id != htlc_id:
            raise WalletServiceError("the htlc record names a different lock")
        if sender != stored.beneficiary:
            raise WalletServiceError(f"the htlc pays {stored.beneficiary}, not {sender}")
        if len(preimage) != 64 or preimage != preimage.lower():
            raise WalletServiceError("an htlc preimage must be 32-byte lowercase hex")
        try:
            preimage_bytes = bytes.fromhex(preimage)
        except ValueError:
            raise WalletServiceError("an htlc preimage must be hexadecimal") from None
        if hashlib.sha256(preimage_bytes).hexdigest() != stored.hashlock:
            raise WalletServiceError("the preimage does not open this htlc")
        resolved_fee = self.required_fee(fee)
        spendable = self.state.spendable(sender)
        if stored.asset:
            if spendable < resolved_fee:
                raise WalletServiceError(f"{sender} cannot pay the {resolved_fee} SCCP fee")
        elif spendable + stored.amount < resolved_fee:
            raise WalletServiceError("the htlc amount cannot cover the network fee")
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.HTLC_CLAIM,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "public_key": keypair.public_key_hex,
                "htlc_id": htlc_id,
                "preimage": preimage,
            },
        )
        return sign_transaction(keypair, tx)

    def build_htlc_refund(
        self,
        keypair: HybridKeypair,
        *,
        htlc_id: str,
        fee: int | None = None,
        nonce: int | None = None,
        record: HtlcRecord | None = None,
    ) -> Transaction:
        """Build and sign an ``htlc_refund``; valid only at/after the timelock.

        ``record`` is the same node-view escape hatch as :meth:`build_htlc_claim`.
        """
        sender = self.address_for(keypair)
        stored = record if record is not None else self._htlc_for(htlc_id)
        if stored.htlc_id != htlc_id:
            raise WalletServiceError("the htlc record names a different lock")
        if sender != stored.sender:
            raise WalletServiceError(f"only {stored.sender} may refund this htlc")
        resolved_fee = self.required_fee(fee)
        spendable = self.state.spendable(sender)
        if stored.asset:
            if spendable < resolved_fee:
                raise WalletServiceError(f"{sender} cannot pay the {resolved_fee} SCCP fee")
        elif spendable + stored.amount < resolved_fee:
            raise WalletServiceError("the htlc amount cannot cover the network fee")
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.HTLC_REFUND,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={"public_key": keypair.public_key_hex, "htlc_id": htlc_id},
        )
        return sign_transaction(keypair, tx)

    def build_register(
        self,
        keypair: HybridKeypair,
        *,
        endpoint: str,
        engine: str,
        bond: int | None = None,
        fee: int | None = None,
        nonce: int | None = None,
        variant: str = "standard",
    ) -> Transaction:
        """Build and sign a miner registration locking ``bond``.

        ``variant`` names the rules this miner plays. Standard is the default
        and is omitted from the payload, so a standard registration has exactly
        the protocol-v1 transaction id.
        """
        sender = self.address_for(keypair)
        try:
            selected = self.params.variant(variant)
        except ConfigError as exc:
            raise WalletServiceError(str(exc)) from None
        resolved_bond = self.params.min_bond if bond is None else bond
        if resolved_bond < self.params.min_bond:
            raise WalletServiceError(
                f"bond {resolved_bond} is below the minimum {self.params.min_bond}"
            )
        resolved_fee = self.required_fee(fee)
        if self.state.spendable(sender) < resolved_bond + resolved_fee:
            raise WalletServiceError(
                f"{sender} cannot cover a {format_coins(resolved_bond)} bond plus a "
                f"{format_coins(resolved_fee)} fee"
            )
        payload: dict[str, object] = {
            "public_key": keypair.public_key_hex,
            "endpoint": endpoint,
            "engine": engine,
            "bond": resolved_bond,
        }
        if selected.name != "standard":
            payload["variant"] = selected.name
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.REGISTER,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload=payload,
        )
        return sign_transaction(keypair, tx)

    def build_register_witness(
        self,
        keypair: HybridKeypair,
        *,
        endpoint: str,
        bond: int | None = None,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a ``register_witness`` locking a clock-witness bond.

        The chain stores the sender's hybrid public key beside the endpoint and
        bond, so a later claim can verify receipt and checkpoint signatures
        against the witness registry instead of trusting whoever presents them.
        A witness cannot register twice: the transaction layer rejects a second
        registration from the same address.
        """
        sender = self.address_for(keypair)
        resolved_bond = self.params.clock_witness_bond if bond is None else bond
        if resolved_bond < self.params.clock_witness_bond:
            raise WalletServiceError(
                f"witness bond {resolved_bond} is below the minimum "
                f"{self.params.clock_witness_bond}"
            )
        resolved_fee = self.required_fee(fee)
        if self.state.spendable(sender) < resolved_bond + resolved_fee:
            raise WalletServiceError(
                f"{sender} cannot cover a {format_coins(resolved_bond)} witness bond plus a "
                f"{format_coins(resolved_fee)} fee"
            )
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.REGISTER_WITNESS,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "public_key": keypair.public_key_hex,
                "endpoint": endpoint,
                "bond": resolved_bond,
            },
        )
        return sign_transaction(keypair, tx)

    def build_availability(
        self,
        keypair: HybridKeypair,
        *,
        nonce_secret: str,
        max_games: int = 1,
        epoch: int | None = None,
        fee: int | None = None,
        tx_nonce: int | None = None,
    ) -> Transaction:
        """Build an availability commitment for the epoch two ahead."""
        from sccp.protocol.beacon import commitment

        sender = self.address_for(keypair)
        target = self._epoch_of_next_block() + self.params.availability_lead
        if epoch is not None and epoch != target:
            raise WalletServiceError(
                f"availability must be committed for epoch {target}, not {epoch}"
            )
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.AVAILABILITY,
            sender=sender,
            nonce=self._resolve_nonce(sender, tx_nonce),
            fee=self.required_fee(fee),
            payload={
                "public_key": keypair.public_key_hex,
                "epoch": target,
                "max_games": max_games,
                "commitment": commitment(sender, target, nonce_secret),
            },
        )
        return sign_transaction(keypair, tx)

    def build_reveal(
        self,
        keypair: HybridKeypair,
        nonce_secret: str,
        *,
        epoch: int | None = None,
        fee: int | None = None,
        tx_nonce: int | None = None,
    ) -> Transaction:
        """Build and sign the reveal matching an availability commitment."""
        sender = self.address_for(keypair)
        target = self._epoch_of_next_block() + 1 if epoch is None else epoch
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.REVEAL,
            sender=sender,
            nonce=self._resolve_nonce(sender, tx_nonce),
            fee=self.required_fee(fee),
            payload={
                "public_key": keypair.public_key_hex,
                "epoch": target,
                "nonce": nonce_secret,
            },
        )
        return sign_transaction(keypair, tx)

    def _history_entry(self, tx: Transaction, height: int, address: str) -> HistoryEntry | None:
        if tx.sender == address:
            if tx.type is TxType.TRANSFER:
                amount = tx.payload.get("amount")
                delta = -(amount if isinstance(amount, int) else 0) - tx.fee
                to = tx.payload.get("to")
                return HistoryEntry(
                    txid=txid(tx),
                    height=height,
                    type=tx.type,
                    delta=delta,
                    fee=tx.fee,
                    counterparty=to if isinstance(to, str) else None,
                )
            return HistoryEntry(
                txid=txid(tx), height=height, type=tx.type, delta=-tx.fee, fee=tx.fee
            )
        if tx.type is TxType.TRANSFER and tx.payload.get("to") == address:
            amount = tx.payload.get("amount")
            return HistoryEntry(
                txid=txid(tx),
                height=height,
                type=tx.type,
                delta=amount if isinstance(amount, int) else 0,
                fee=0,
                counterparty=tx.sender,
            )
        return None


__all__ = ["Balance", "HistoryEntry", "WalletService", "derive_address"]
