"""Bonded quality challenges: validation, bonds and adjudication (M5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain.state import ChallengeRecord, ProofRecord, State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    validate_transaction,
)
from sccp.config import DEVNET, NetworkParams
from sccp.errors import ChallengeError, TxValidationError
from sccp.protocol import challenges
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService

PROOF_HEIGHT = 2
OPEN_HEIGHT = 4
PROOF_ID = "ab" * 32
GAME_HASH = "cd" * 32
BOND = 5_000


def _params(**overrides: Any) -> NetworkParams:
    return DEVNET.model_copy(
        update={"challenge_window": 20, "coinbase_maturity": 10, **overrides}
    )


def _state(
    params: NetworkParams,
    accounts: Mapping[str, Any],
    *,
    height: int = OPEN_HEIGHT,
) -> State:
    state = State(params=params, height=height, timestamps=[1_700_000_000])
    for account in accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000_000
    state.proofs[GAME_HASH] = ProofRecord(
        proof_id=PROOF_ID,
        game_hash=GAME_HASH,
        height=PROOF_HEIGHT,
        winner=accounts["alice"].address,
        miner=accounts["alice"].address,
    )
    state.proof_ids[PROOF_ID] = GAME_HASH
    return state


@pytest.fixture()
def params() -> NetworkParams:
    return _params()


@pytest.fixture()
def ledger(params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    return _state(params, protocol_accounts)


def _open(
    ledger: State,
    accounts: Mapping[str, Any],
    *,
    challenger: str = "bob",
    height: int = OPEN_HEIGHT,
    bond: int = BOND,
    reason: str = "trace does not replay",
) -> ChallengeRecord:
    return challenges.open_challenge(
        ledger,
        challenger=accounts[challenger].address,
        proof_id=PROOF_ID,
        reason=reason,
        evidence="ply 8 expects Qh5 but the PGN plays Qf3",
        bond=bond,
        height=height,
    )


def test_open_locks_the_bond_and_records_the_proof(
    ledger: State, protocol_accounts: Mapping[str, Any], params: NetworkParams
) -> None:
    challenger = protocol_accounts["bob"].address
    before = ledger.ensure(challenger).balance
    record = _open(ledger, protocol_accounts)

    assert ledger.ensure(challenger).balance == before - BOND
    assert record.status == "open"
    assert record.proof_id == PROOF_ID
    assert record.game_hash == GAME_HASH
    assert record.miner == protocol_accounts["alice"].address
    assert record.deadline_height == OPEN_HEIGHT + params.challenge_window
    assert ledger.challenges[record.challenge_id] == record


def test_upheld_returns_the_bond_and_slashes_the_miner(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    challenger = protocol_accounts["bob"].address
    miner = protocol_accounts["alice"].address
    before = ledger.ensure(challenger).balance
    record = _open(ledger, protocol_accounts)

    account = ledger.ensure(miner)
    account.bonded = 3 * BOND
    account.coinbase_queue = [(PROOF_HEIGHT, 700), (1, 300)]
    account.immature = 1_000

    resolved = challenges.resolve_challenge(
        ledger,
        record.challenge_id,
        upheld=True,
        height=OPEN_HEIGHT + 2,
        panel=(protocol_accounts["carol"].address,),
    )

    assert resolved.status == "upheld"
    assert resolved.panel == (protocol_accounts["carol"].address,)
    assert ledger.ensure(challenger).balance == before
    assert ledger.ensure(miner).bonded == 0
    # Only the immature coinbase credited at the proof's height is burned.
    assert ledger.ensure(miner).coinbase_queue == [(1, 300)]
    assert ledger.ensure(miner).immature == 300


def test_matured_coinbase_is_not_burned(
    ledger: State, protocol_accounts: Mapping[str, Any], params: NetworkParams
) -> None:
    record = _open(ledger, protocol_accounts)
    miner = protocol_accounts["alice"].address
    account = ledger.ensure(miner)
    account.coinbase_queue = [(PROOF_HEIGHT, 700)]
    account.immature = 700

    challenges.resolve_challenge(
        ledger,
        record.challenge_id,
        upheld=True,
        height=PROOF_HEIGHT + params.coinbase_maturity,
        panel=(protocol_accounts["carol"].address,),
    )

    assert ledger.ensure(miner).coinbase_queue == [(PROOF_HEIGHT, 700)]
    assert ledger.ensure(miner).immature == 700



def test_rejected_burns_the_challenger_bond(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    challenger = protocol_accounts["bob"].address
    miner = protocol_accounts["alice"].address
    record = _open(ledger, protocol_accounts)
    locked = ledger.ensure(challenger).balance
    miner_before = ledger.ensure(miner).balance

    resolved = challenges.resolve_challenge(
        ledger,
        record.challenge_id,
        upheld=False,
        height=OPEN_HEIGHT + 1,
        panel=(protocol_accounts["carol"].address,),
    )

    assert resolved.status == "rejected"
    assert ledger.ensure(challenger).balance == locked
    assert ledger.ensure(miner).balance == miner_before


def test_resolution_is_idempotent_for_the_same_verdict(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    panel = (protocol_accounts["carol"].address,)
    first = challenges.resolve_challenge(
        ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1, panel=panel
    )
    again = challenges.resolve_challenge(
        ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 5, panel=panel
    )
    assert again is first
    assert again.resolved_height == OPEN_HEIGHT + 1


def test_a_resolved_challenge_cannot_change_verdict(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    challenges.resolve_challenge(ledger, record.challenge_id, upheld=False, height=OPEN_HEIGHT + 1)
    with pytest.raises(ChallengeError, match="already resolved"):
        challenges.resolve_challenge(
            ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 2
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"challenger": "alice"}, "cannot challenge its own proof"),
        ({"bond": 0}, "positive bond"),
        ({"height": PROOF_HEIGHT + 21}, "window"),
        ({"reason": ""}, "needs a reason and evidence"),
    ],
)
def test_open_rejects_invalid_challenges(
    ledger: State,
    protocol_accounts: Mapping[str, Any],
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ChallengeError, match=message):
        _open(ledger, protocol_accounts, **kwargs)


def test_one_open_challenge_per_challenger(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    _open(ledger, protocol_accounts)
    with pytest.raises(ChallengeError, match="already has an open challenge"):
        _open(ledger, protocol_accounts)
    # Another challenger may still open one.
    other = _open(ledger, protocol_accounts, challenger="carol")
    assert other.challenger == protocol_accounts["carol"].address


def test_no_challenge_after_an_upheld_verdict(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    challenges.resolve_challenge(ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1)
    with pytest.raises(ChallengeError, match="already been invalidated"):
        _open(ledger, protocol_accounts, challenger="carol")


def test_panel_rejects_parties_and_duplicates(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    bob = protocol_accounts["bob"].address
    with pytest.raises(ChallengeError, match="parties"):
        challenges.resolve_challenge(
            ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1, panel=(bob,)
        )
    carol = protocol_accounts["carol"].address
    with pytest.raises(ChallengeError, match="duplicate"):
        challenges.resolve_challenge(
            ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1, panel=(carol, carol)
        )


def test_status_prefers_upheld_then_open(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    assert challenges.challenge_status(ledger, GAME_HASH) == "none"
    record = _open(ledger, protocol_accounts)
    assert challenges.challenge_status(ledger, GAME_HASH) == "open"
    rejected = _open(ledger, protocol_accounts, challenger="carol")
    challenges.resolve_challenge(
        ledger, rejected.challenge_id, upheld=False, height=OPEN_HEIGHT + 1
    )
    assert challenges.challenge_status(ledger, GAME_HASH) == "open"
    challenges.resolve_challenge(
        ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 2
    )
    assert challenges.challenge_status(ledger, GAME_HASH) == "upheld"
    assert challenges.challenge_for_proof(ledger, PROOF_ID, status="upheld") == (record,)


def _challenge_tx(
    ledger: State, accounts: Mapping[str, Any], *, bond: int = BOND
) -> Transaction:
    wallet = WalletService(ledger.params, ledger)
    transfer = wallet.build_transfer(
        accounts["bob"].keypair, accounts["carol"].address, 1, fee=ledger.params.min_fee
    )
    return sign_transaction(
        accounts["bob"].keypair,
        transfer.model_copy(
            update={
                "type": TxType.CHALLENGE,
                "payload": {
                    "proof_id": PROOF_ID,
                    "reason": "bad trace",
                    "evidence": "replay diverges at ply 8",
                    "bond": bond,
                },
            }
        ),
    )


def test_transaction_payload_is_strict(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    tx = _challenge_tx(ledger, protocol_accounts)
    extra = sign_transaction(
        protocol_accounts["bob"].keypair,
        tx.model_copy(update={"payload": {**tx.payload, "unexpected": True}}),
    )
    with pytest.raises(TxValidationError) as caught:
        validate_transaction(extra, ledger.params, ledger, height=OPEN_HEIGHT)
    assert caught.value.reason is TxFailure.TYPE
    assert "unknown challenge payload" in str(caught.value)


def test_transaction_locks_bond_and_charges_fee_once(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    tx = _challenge_tx(ledger, protocol_accounts)
    before = ledger.ensure(tx.sender).balance
    validate_transaction(tx, ledger.params, ledger, height=OPEN_HEIGHT)
    apply_transaction(ledger, tx, height=OPEN_HEIGHT)

    record = challenges.challenge_for_proof(ledger, PROOF_ID)[0]
    assert record.challenger == tx.sender
    assert ledger.ensure(tx.sender).balance == before - tx.fee - BOND
    assert ledger.ensure(tx.sender).nonce == 1


def test_snapshot_round_trips_challenges(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    challenges.resolve_challenge(
        ledger,
        record.challenge_id,
        upheld=False,
        height=OPEN_HEIGHT + 1,
        panel=(protocol_accounts["carol"].address,),
    )
    rebuilt = State.from_snapshot(ledger.params, ledger.snapshot())
    assert rebuilt.challenges == ledger.challenges
    assert rebuilt.challenges[record.challenge_id].panel == (protocol_accounts["carol"].address,)


def test_challenge_id_commits_to_every_request_field(
    protocol_accounts: Mapping[str, Any],
) -> None:
    base = challenges.challenge_id(
        "devnet", PROOF_ID, protocol_accounts["bob"].address, "bad", "evidence", BOND, OPEN_HEIGHT
    )
    assert len(base) == 64
    assert base == challenges.challenge_id(
        "devnet", PROOF_ID, protocol_accounts["bob"].address, "bad", "evidence", BOND, OPEN_HEIGHT
    )
    assert base != challenges.challenge_id(
        "devnet", PROOF_ID, protocol_accounts["bob"].address, "bad", "other", BOND, OPEN_HEIGHT
    )
