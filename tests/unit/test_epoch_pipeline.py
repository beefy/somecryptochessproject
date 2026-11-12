"""The availability/reveal pipeline that keeps a miner matchable (PROTOCOL.md 6-7)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sccp.chain.state import State
from sccp.chain.tx import apply_transaction, txid, validate_transaction
from sccp.config import TESTNET
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, keypair_from_seed
from sccp.errors import EpochPipelineError, RelayError
from sccp.miner.epoch_pipeline import EpochPipeline, PipelineAction
from sccp.protocol.beacon import commitment
from sccp.protocol.models import Availability, Transaction, TxType

SECRET = "11" * 32
"""The 32-byte hex nonce every availability commitment in this module uses."""

OPEN_EPOCH = 3
"""TESTNET's epoch 3 covers heights 15-19, the first epoch with an open window."""

OPEN_HEIGHT = 15
"""The last block of :data:`OPEN_EPOCH`, so the next block is still in it."""


@dataclass
class _StubNode:
    """The ``/v1/status``, ``/v1/accounts`` and ``/v1/tx`` calls the pipeline makes."""

    height: int
    nonce: int = 0
    registered_until: int | None = 99
    reject: str | None = None
    submitted: list[Transaction] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def status(self) -> dict[str, object]:
        return {"height": self.height}

    def account(self, address: str) -> dict[str, object]:
        self.asked.append(address)
        return {"nonce": self.nonce, "registered_until_epoch": self.registered_until}

    def submit_transaction(self, tx: Transaction) -> dict[str, object]:
        if self.reject is not None:
            raise RelayError(self.reject)
        self.submitted.append(tx)
        return {"txid": txid(tx)}


@pytest.fixture
def keypair() -> Keypair:
    return keypair_from_seed(bytes(range(32)))


@pytest.fixture
def node() -> _StubNode:
    return _StubNode(height=OPEN_HEIGHT)


@pytest.fixture
def pipeline(keypair: Keypair, node: _StubNode) -> EpochPipeline:
    return EpochPipeline(
        TESTNET,
        keypair,
        node,
        nonce_secret=SECRET,
        max_games=TESTNET.rounds_per_epoch,
    )


def _state(
    address: str,
    keypair: Keypair,
    *,
    height: int,
    nonce: int = 0,
    registered_until: int = 99,
    bond: int | None = None,
    committed_epoch: int | None = None,
) -> State:
    """A one-account tip state tall enough to validate the pipeline's transactions.

    ``committed_epoch`` seeds the availability a reveal in ``committed_epoch + 1``
    refers to: that commitment was published ``availability_lead`` epochs before.
    """
    state = State(params=TESTNET, height=height)
    account = state.ensure(address)
    account.public_key = keypair.public_key_hex
    account.nonce = nonce
    account.balance = 10**12
    account.bonded = TESTNET.min_bond if bond is None else bond
    account.registered_until_epoch = registered_until
    if committed_epoch is not None:
        state.availability[committed_epoch] = {
            address: Availability(
                network=TESTNET.network_id,
                miner=address,
                epoch=committed_epoch,
                max_games=TESTNET.rounds_per_epoch,
                commitment=commitment(address, committed_epoch, SECRET),
            )
        }
    return state


def test_the_first_step_submits_availability_and_a_reveal(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    """One tick covers both open windows from the same nonce sequence."""
    actions = pipeline.step()

    assert [action.kind for action in actions] == [
        TxType.AVAILABILITY.value,
        TxType.REVEAL.value,
    ]
    availability, reveal = node.submitted
    assert availability.payload["epoch"] == OPEN_EPOCH + TESTNET.availability_lead
    assert reveal.payload["epoch"] == OPEN_EPOCH + 1
    assert (availability.nonce, reveal.nonce) == (0, 1)
    assert availability.payload["max_games"] == TESTNET.rounds_per_epoch
    assert availability.payload["commitment"] == commitment(
        pipeline.address, availability.payload["epoch"], SECRET
    )
    assert [action.nonce for action in actions] == [0, 1]
    assert [action.txid for action in actions] == [txid(availability), txid(reveal)]
    assert pipeline.pending == {
        (TxType.AVAILABILITY.value, OPEN_EPOCH + TESTNET.availability_lead): 0,
        (TxType.REVEAL.value, OPEN_EPOCH + 1): 1,
    }
    assert node.asked == [pipeline.address]


def test_the_node_accepts_what_the_pipeline_builds(
    pipeline: EpochPipeline, keypair: Keypair, node: _StubNode
) -> None:
    """The transactions satisfy the consensus validator at the tip they target."""
    pipeline.step()
    availability, reveal = node.submitted
    state = _state(
        pipeline.address,
        keypair,
        height=OPEN_HEIGHT,
        committed_epoch=OPEN_EPOCH + 1,
    )

    validate_transaction(availability, TESTNET, state, height=OPEN_HEIGHT)
    apply_transaction(state, availability, height=OPEN_HEIGHT)
    validate_transaction(reveal, TESTNET, state, height=OPEN_HEIGHT)
    apply_transaction(state, reveal, height=OPEN_HEIGHT)

    assert state.reveals[OPEN_EPOCH + 1] == {pipeline.address: SECRET}
    assert set(pipeline.pending) == {
        (TxType.AVAILABILITY.value, OPEN_EPOCH + TESTNET.availability_lead),
        (TxType.REVEAL.value, OPEN_EPOCH + 1),
    }


def test_a_second_tick_in_the_same_epoch_submits_nothing(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    pipeline.step()

    assert pipeline.step() == ()
    assert len(node.submitted) == 2


def test_the_next_epoch_moves_both_windows_forward(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    pipeline.step()
    node.height = OPEN_HEIGHT + TESTNET.epoch_length
    node.nonce = 2

    actions = pipeline.step()

    assert [(action.kind, action.epoch, action.nonce) for action in actions] == [
        (TxType.AVAILABILITY.value, OPEN_EPOCH + TESTNET.availability_lead + 1, 2),
        (TxType.REVEAL.value, OPEN_EPOCH + 2, 3),
    ]
    assert set(pipeline.pending) == {
        (TxType.AVAILABILITY.value, OPEN_EPOCH + TESTNET.availability_lead + 1),
        (TxType.REVEAL.value, OPEN_EPOCH + 2),
    }


def test_unconfirmed_transactions_do_not_hold_a_nonce(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    """A transaction that never landed is replaced once its window closes."""
    pipeline.step()
    node.height = OPEN_HEIGHT + TESTNET.epoch_length

    actions = pipeline.step()

    # The chain nonce never moved, so the closed epoch's nonces are handed out again.
    assert [action.nonce for action in actions] == [0, 1]


def test_a_restarted_pipeline_resubmits_the_open_window(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    """Resubmitting an identical commitment is harmless, and covers a lost mempool."""
    pipeline.step()
    restarted = EpochPipeline(
        TESTNET,
        pipeline.keypair,
        node,
        nonce_secret=SECRET,
        max_games=TESTNET.rounds_per_epoch,
    )

    actions = restarted.step()

    assert [action.kind for action in actions] == [
        TxType.AVAILABILITY.value,
        TxType.REVEAL.value,
    ]
    assert [action.epoch for action in actions] == [OPEN_EPOCH + 2, OPEN_EPOCH + 1]


def test_an_unregistered_miner_is_refused(pipeline: EpochPipeline, node: _StubNode) -> None:
    node.registered_until = None

    with pytest.raises(EpochPipelineError) as caught:
        pipeline.step()

    assert "is not registered on testnet" in str(caught.value)
    assert node.submitted == []


def test_a_registration_shorter_than_the_window_is_refused(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    node.registered_until = OPEN_EPOCH + 1

    with pytest.raises(EpochPipelineError) as caught:
        pipeline.step()

    message = str(caught.value)
    assert f"covers epoch {OPEN_EPOCH + 1}" in message
    assert f"not {OPEN_EPOCH + TESTNET.availability_lead}" in message


def test_a_node_rejection_is_reported_as_a_pipeline_error(
    pipeline: EpochPipeline, node: _StubNode
) -> None:
    node.reject = "fee is below the minimum"

    with pytest.raises(EpochPipelineError) as caught:
        pipeline.step()

    assert "the node rejected the availability" in str(caught.value)
    assert pipeline.pending == {}


@pytest.mark.parametrize("secret", ["zz" * 32, "11" * 31, ""])
def test_the_nonce_secret_must_be_32_hex_bytes(
    keypair: Keypair, node: _StubNode, secret: str
) -> None:
    with pytest.raises(EpochPipelineError):
        EpochPipeline(TESTNET, keypair, node, nonce_secret=secret)


def test_max_games_must_be_positive(keypair: Keypair, node: _StubNode) -> None:
    with pytest.raises(EpochPipelineError) as caught:
        EpochPipeline(TESTNET, keypair, node, nonce_secret=SECRET, max_games=0)

    assert "max_games must be at least 1" in str(caught.value)


def test_the_address_is_derived_from_the_mining_key(
    pipeline: EpochPipeline, keypair: Keypair
) -> None:
    assert pipeline.address == encode_address(keypair.public_key_bytes, TESTNET)


def test_a_pipeline_action_describes_itself() -> None:
    action = PipelineAction(kind="availability", epoch=5, nonce=7, txid="ab" * 32)

    assert action.describe() == "availability for epoch 5 (nonce 7, abababababababab)"
