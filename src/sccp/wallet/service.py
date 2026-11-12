"""Wallet service: balances, transfers, staking and history (plan section 5.3).

This is the read/build layer the CLI and miner use. It never holds a decrypted
key longer than one call: callers pass a :class:`~sccp.crypto.keys.Keypair` and
get a signed, canonical transaction back.

Everything is derived from a :class:`~sccp.chain.state.State`, so the same calls
work against the tip, a fork or a test fixture. History additionally needs a
chain, because the state alone keeps balances, not the transactions that produced
them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sccp.chain.chain import Chain
from sccp.chain.state import Account, State, epoch_of
from sccp.chain.tx import sign_transaction, txid
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.keys import Keypair
from sccp.errors import WalletServiceError
from sccp.protocol.models import Transaction, TxType


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
    ) -> None:
        self.params = get_network(params)
        self.state = state
        self.chain = chain

    # -- reads --------------------------------------------------------------

    @property
    def network(self) -> str:
        """The network id of this service."""
        return self.params.network_id

    def address_for(self, keypair: Keypair) -> str:
        """The bech32 address of ``keypair`` on this network."""
        return encode_address(keypair.public_key_bytes, self.params)

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
                f"fee {fee} is below the network minimum of {self.params.min_fee}"
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

    def _require_address(self, address: str) -> None:
        if not validate_address(address, self.params):
            raise WalletServiceError(f"{address!r} is not a valid {self.params.network_id} address")

    # -- transaction builders ----------------------------------------------

    def build_transfer(
        self,
        keypair: Keypair,
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
                f"{sender} has {spendable} spendable, the transfer needs {total}"
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
        keypair: Keypair,
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
                f"{sender} cannot cover a {resolved_bond} bond plus a {resolved_fee} fee"
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
        keypair: Keypair,
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
        target = epoch_of(self.state.height, self.params) + self.params.availability_lead
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
        keypair: Keypair,
        nonce_secret: str,
        *,
        epoch: int | None = None,
        fee: int | None = None,
        tx_nonce: int | None = None,
    ) -> Transaction:
        """Build and sign the reveal matching an availability commitment."""
        sender = self.address_for(keypair)
        target = epoch_of(self.state.height, self.params) + 1 if epoch is None else epoch
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


__all__ = ["Balance", "HistoryEntry", "WalletService"]
