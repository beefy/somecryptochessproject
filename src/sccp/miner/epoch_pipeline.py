"""Keep a miner's availability and reveals ahead of the chain (PROTOCOL.md 6-7).

A miner is on epoch ``E``'s roster only if it committed availability while the
chain was in ``E - availability_lead`` and revealed the nonce during ``E - 1``.
Both are ordinary transactions, so a miner that waits for a match to reach it
can never become matchable again: the windows for the epochs it could play are
already closed by the time it reacts. This module submits those two
transactions every epoch, from the miner's own tick loop, so the roster stays
populated without an operator running the wallet by hand.

The pipeline is deliberately dumb about ordering: it reads the chain's height
and the account nonce from the node, then hands out consecutive nonces for the
transactions it has not submitted for the current epoch window yet. Each
``(kind, epoch)`` pair is submitted at most once per window; when the window
moves on, the entry is dropped and its nonce (if the transaction never landed)
is handed out again. Re-submitting the same commitment after a restart is
harmless: the chain only rejects a *different* commitment for the same epoch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sccp.chain.state import State, epoch_of
from sccp.chain.tx import txid
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair
from sccp.errors import EpochPipelineError, RelayError
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService


class PipelineNode(Protocol):
    """The node calls the pipeline needs: tip height, account view, submission."""

    def status(self) -> Mapping[str, object]:
        """The node's ``/v1/status`` document."""

    def account(self, address: str) -> Mapping[str, object]:
        """The node's view of one account."""

    def submit_transaction(self, tx: Transaction) -> Mapping[str, object]:
        """Broadcast one signed transaction and return the node's receipt."""


@dataclass(frozen=True, slots=True)
class PipelineAction:
    """One availability or reveal transaction the pipeline submitted."""

    kind: str
    epoch: int
    nonce: int
    txid: str

    def describe(self) -> str:
        """A one-line summary for the miner's log."""
        return f"{self.kind} for epoch {self.epoch} (nonce {self.nonce}, {self.txid[:16]})"


@dataclass
class EpochPipeline:
    """Submit the availability commitment and reveal that keep a miner matchable."""

    params: NetworkParams
    keypair: Keypair
    node: PipelineNode
    nonce_secret: str
    max_games: int = 1
    _pending: dict[tuple[str, int], int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.params = get_network(self.params)
        if self.max_games < 1:
            raise EpochPipelineError(f"max_games must be at least 1, not {self.max_games}")
        try:
            secret = bytes.fromhex(self.nonce_secret)
        except ValueError:
            raise EpochPipelineError("the availability nonce secret is not hex") from None
        if len(secret) != 32:
            raise EpochPipelineError(
                f"the availability nonce secret must be 32 bytes, not {len(secret)}"
            )

    @property
    def address(self) -> str:
        """The miner address these transactions are signed with."""
        return encode_address(self.keypair.public_key_bytes, self.params)

    @property
    def pending(self) -> dict[tuple[str, int], int]:
        """``(kind, epoch) -> nonce`` for transactions not yet in a block."""
        return dict(self._pending)

    def step(self) -> tuple[PipelineAction, ...]:
        """Submit whatever the chain's current epoch still lets the miner commit.

        Returns the transactions submitted by this call, which is empty once both
        windows are covered. Raises :class:`EpochPipelineError` when the miner
        cannot be matchable at all (unregistered, or the node rejected a
        transaction).
        """
        status = self.node.status()
        height = _int_field(status, "height", where="/v1/status")
        account = self.node.account(self.address)
        chain_nonce = _int_field(account, "nonce", where=f"/v1/accounts/{self.address}")
        registered_until = account.get("registered_until_epoch")

        epoch = epoch_of(height + 1, self.params)
        availability_epoch = epoch + self.params.availability_lead
        reveal_epoch = epoch + 1
        if not isinstance(registered_until, int) or isinstance(registered_until, bool):
            raise EpochPipelineError(
                f"{self.address} is not registered on {self.params.network_id}; "
                "register it before mining"
            )
        if registered_until < availability_epoch:
            raise EpochPipelineError(
                f"the registration covers epoch {registered_until}, not "
                f"{availability_epoch}; the bond is missing or unbonding"
            )

        desired = (
            (TxType.AVAILABILITY.value, availability_epoch),
            (TxType.REVEAL.value, reveal_epoch),
        )
        self._forget(keep=desired)
        submitted: list[PipelineAction] = []
        for kind, target in desired:
            if (kind, target) in self._pending:
                continue
            nonce = self._next_nonce(chain_nonce)
            tx = self._build(kind, height, nonce)
            landed = tx.payload.get("epoch")
            if landed != target:
                raise EpochPipelineError(
                    f"the {kind} transaction targets epoch {landed}, not {target}"
                )
            try:
                document = self.node.submit_transaction(tx)
            except RelayError as exc:
                raise EpochPipelineError(f"the node rejected the {kind}: {exc}") from exc
            digest = document.get("txid")
            self._pending[(kind, target)] = nonce
            submitted.append(
                PipelineAction(
                    kind=kind,
                    epoch=target,
                    nonce=nonce,
                    txid=digest if isinstance(digest, str) else txid(tx),
                )
            )
        return tuple(submitted)

    # -- internals -----------------------------------------------------------

    def _forget(self, keep: tuple[tuple[str, int], ...]) -> None:
        """Drop transactions whose epoch window has closed.

        An entry outlives its confirmation on purpose: its nonce falls behind the
        chain's, so :meth:`_next_nonce` ignores it, but forgetting it while the
        window is still open would resubmit the same availability every tick.
        """
        for key in list(self._pending):
            if key not in keep:
                self._pending.pop(key, None)

    def _next_nonce(self, chain_nonce: int) -> int:
        """The lowest nonce neither the chain nor a pending transaction has taken."""
        return max([chain_nonce, *(nonce + 1 for nonce in self._pending.values())])

    def _build(self, kind: str, height: int, nonce: int) -> Transaction:
        """Build ``kind`` against a one-account view of the tip at ``height``."""
        state = State(params=self.params, height=height)
        state.ensure(self.address).nonce = nonce
        service = WalletService(self.params, state)
        if kind == TxType.AVAILABILITY.value:
            return service.build_availability(
                self.keypair,
                nonce_secret=self.nonce_secret,
                max_games=self.max_games,
            )
        return service.build_reveal(self.keypair, self.nonce_secret)


def _int_field(document: Mapping[str, object], key: str, *, where: str) -> int:
    """Read a non-negative integer field, or raise."""
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EpochPipelineError(f"{where} did not return a usable {key!r}: {value!r}")
    return value


__all__ = ["EpochPipeline", "PipelineAction", "PipelineNode"]
