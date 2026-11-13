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

from collections.abc import Mapping
from dataclasses import dataclass

from sccp.chain.chain import Chain
from sccp.chain.state import Account, State, epoch_of
from sccp.chain.tx import sign_transaction, txid
from sccp.config import NetworkParams, format_coins, get_network
from sccp.crypto.address import encode_address, encode_hybrid_address, validate_address
from sccp.crypto.pq import HybridKeypair, SigningKey
from sccp.errors import StateError, WalletServiceError
from sccp.protocol.models import Transaction, TxType

DEFAULT_SOURCE = "the local chain in --data-dir"
"""Where a wallet's state came from, when the caller does not say."""


def derive_address(keypair: SigningKey, params: NetworkParams) -> str:
    """The bech32 address of ``keypair`` on ``params``.

    The scheme decides the program version: an Ed25519 key derives a version-0
    address, a hybrid key a version-1 one. Both expose ``public_key_bytes``, so
    only the version byte and the hash domain differ.
    """
    if isinstance(keypair, HybridKeypair):
        return encode_hybrid_address(keypair.public_key_bytes, params)
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

    def address_for(self, keypair: SigningKey) -> str:
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
        keypair: SigningKey,
        to: str,
        amount: int,
        *,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a transfer, raising if the sender cannot afford it."""
        sender = self.address_for(keypair)
        self._require_address(to)
        if amount <= 0:
            raise WalletServiceError("a transfer amount must be positive")
        if to == sender:
            raise WalletServiceError("a transfer must name a different recipient")
        resolved_fee = self.required_fee(fee)
        total = amount + resolved_fee
        spendable = self.state.spendable(sender)
        if spendable < total:
            raise WalletServiceError(
                f"{sender} has {format_coins(spendable)} spendable, the transfer needs "
                f"{format_coins(total)} ({format_coins(amount)} plus a "
                f"{format_coins(resolved_fee)} fee); this balance came from {self.source}"
            )
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.TRANSFER,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "to": to,
                "amount": amount,
                "public_key": keypair.public_key_hex,
            },
        )
        return sign_transaction(keypair, tx)

    def build_register(
        self,
        keypair: SigningKey,
        *,
        endpoint: str,
        engine: str,
        bond: int | None = None,
        fee: int | None = None,
        nonce: int | None = None,
    ) -> Transaction:
        """Build and sign a miner registration locking ``bond``."""
        sender = self.address_for(keypair)
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
        tx = Transaction(
            network=self.params.network_id,
            type=TxType.REGISTER,
            sender=sender,
            nonce=self._resolve_nonce(sender, nonce),
            fee=resolved_fee,
            payload={
                "public_key": keypair.public_key_hex,
                "endpoint": endpoint,
                "engine": engine,
                "bond": resolved_bond,
            },
        )
        return sign_transaction(keypair, tx)

    def build_availability(
        self,
        keypair: SigningKey,
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
        keypair: SigningKey,
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
