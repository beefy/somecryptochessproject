"""Bonded quality challenges: validation, bonds and adjudication."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from sccp.chain.state import ZERO_HASH, ChallengeRecord, ProofRecord, State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    validate_transaction,
)
from sccp.config import DEVNET, NetworkParams
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
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
    return DEVNET.model_copy(update={"challenge_window": 20, "coinbase_maturity": 10, **overrides})


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
    challenges.resolve_challenge(ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 2)
    assert challenges.challenge_status(ledger, GAME_HASH) == "upheld"
    assert challenges.challenge_for_proof(ledger, PROOF_ID, status="upheld") == (record,)


def _challenge_tx(ledger: State, accounts: Mapping[str, Any], *, bond: int = BOND) -> Transaction:
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


CANDIDATES = tuple(f"{index:040x}" for index in range(12))


def _bond(ledger: State, *addresses: str, amount: int = BOND) -> tuple[str, ...]:
    for address in addresses:
        ledger.ensure(address).bonded = amount
    return addresses


def test_select_panel_is_deterministic_and_order_independent() -> None:
    first = challenges.select_panel(ZERO_HASH, PROOF_ID, CANDIDATES)
    again = challenges.select_panel(ZERO_HASH, PROOF_ID, reversed(CANDIDATES))

    assert first == again
    assert len(first) == challenges.PANEL_SIZE
    assert len(set(first)) == len(first)
    assert set(first) <= set(CANDIDATES)


def test_select_panel_commits_to_the_beacon_and_the_proof() -> None:
    size = len(CANDIDATES) - 1
    base = challenges.select_panel(ZERO_HASH, PROOF_ID, CANDIDATES, size=size)

    assert base != challenges.select_panel("ff" * 32, PROOF_ID, CANDIDATES, size=size)
    assert base != challenges.select_panel(ZERO_HASH, "ef" * 32, CANDIDATES, size=size)


def test_select_panel_excludes_parties() -> None:
    bob, miner = CANDIDATES[0], CANDIDATES[1]
    panel = challenges.select_panel(ZERO_HASH, PROOF_ID, CANDIDATES, size=5, exclude=(bob, miner))

    assert len(panel) == 5
    assert bob not in panel and miner not in panel


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"size": 0}, "odd, positive"),
        ({"size": 2}, "odd, positive"),
        ({"size": 4}, "odd, positive"),
        ({"beacon_hash": "zz" * 32}, "must be hex"),
        ({"beacon_hash": "00" * 31}, "32-byte digest"),
        ({"proof_id": "00" * 31}, "32-byte digest"),
        ({"size": 13}, "eligible adjudicator"),
    ],
)
def test_select_panel_rejects_impossible_panels(kwargs: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {
        "beacon_hash": ZERO_HASH,
        "proof_id": PROOF_ID,
        "candidates": CANDIDATES,
    }
    arguments.update(kwargs)
    with pytest.raises(ChallengeError, match=message):
        challenges.select_panel(**arguments)


def test_panel_verdict_needs_a_majority() -> None:
    panel = ("a", "b", "c")
    assert challenges.panel_verdict(panel, {"a": True, "b": False, "c": True}) is True
    assert challenges.panel_verdict(panel, {"a": False, "b": True, "c": False}) is False
    assert challenges.panel_verdict(("a",), {"a": True}) is True


@pytest.mark.parametrize(
    ("panel", "votes", "message"),
    [
        ((), {}, "odd number"),
        (("a", "b"), {"a": True, "b": True}, "odd number"),
        (("a", "a", "b"), {"a": True, "b": True}, "duplicate"),
        (("a", "b", "c"), {"a": True, "b": True}, "missing"),
        (("a", "b", "c"), {"a": True, "b": True, "c": True, "d": True}, "unknown"),
        (("a", "b", "c"), {"a": True, "b": True, "c": 1}, "boolean"),
        (("a", "b", "c"), {"a": True, "b": True, "c": "false"}, "boolean"),
    ],
)
def test_panel_verdict_rejects_incomplete_ballots(
    panel: tuple[str, ...], votes: Mapping[str, bool], message: str
) -> None:
    with pytest.raises(ChallengeError, match=message):
        challenges.panel_verdict(panel, votes)


def test_challenge_panel_derives_from_the_bonded_set(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    carol = protocol_accounts["carol"].address
    _bond(ledger, carol, "ee" * 20, "fd" * 20)

    panel = challenges.challenge_panel(ledger, record.challenge_id)

    assert len(panel) == challenges.PANEL_SIZE
    assert carol in panel
    assert protocol_accounts["bob"].address not in panel
    assert protocol_accounts["alice"].address not in panel
    assert panel == challenges.challenge_panel(ledger, record.challenge_id)

    # A resolved challenge keeps the panel it was decided by.
    challenges.adjudicate_with_panel(
        ledger,
        record.challenge_id,
        {member: True for member in panel},
        height=OPEN_HEIGHT + 1,
    )
    ledger.ensure(carol).bonded = 0
    assert challenges.challenge_panel(ledger, record.challenge_id) == panel


def test_challenge_panel_needs_enough_eligible_adjudicators(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    _bond(ledger, protocol_accounts["carol"].address)

    with pytest.raises(ChallengeError, match="eligible adjudicator"):
        challenges.challenge_panel(ledger, record.challenge_id)
    with pytest.raises(ChallengeError, match="no challenge"):
        challenges.challenge_panel(ledger, "00" * 32)


def test_adjudicate_with_panel_settles_an_upheld_majority(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    challenger = protocol_accounts["bob"].address
    miner = protocol_accounts["alice"].address
    _bond(
        ledger,
        protocol_accounts["carol"].address,
        "ee" * 20,
        "fd" * 20,
        amount=2 * BOND,
    )
    locked = ledger.ensure(challenger).balance
    panel = challenges.challenge_panel(ledger, record.challenge_id)
    votes = dict.fromkeys(panel, True)
    votes[panel[0]] = False

    resolved = challenges.adjudicate_with_panel(
        ledger, record.challenge_id, votes, height=OPEN_HEIGHT + 1
    )

    assert resolved.status == "upheld"
    assert resolved.panel == panel
    assert resolved.resolved_height == OPEN_HEIGHT + 1
    assert ledger.ensure(challenger).balance == locked + BOND
    assert ledger.ensure(miner).bonded == 0


def test_adjudicate_with_panel_burns_the_bond_on_a_rejection(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    challenger = protocol_accounts["bob"].address
    miner = protocol_accounts["alice"].address
    _bond(ledger, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)
    ledger.ensure(miner).bonded = BOND
    locked = ledger.ensure(challenger).balance
    panel = challenges.challenge_panel(ledger, record.challenge_id)
    votes = dict.fromkeys(panel, False)
    votes[panel[0]] = True

    resolved = challenges.adjudicate_with_panel(
        ledger, record.challenge_id, votes, height=OPEN_HEIGHT + 1
    )

    assert resolved.status == "rejected"
    assert ledger.ensure(challenger).balance == locked
    assert ledger.ensure(miner).bonded == BOND


def test_adjudicate_with_panel_rejects_off_panel_voters(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    _bond(ledger, protocol_accounts["carol"].address, "ee" * 20, "fd" * 20)
    panel = challenges.challenge_panel(ledger, record.challenge_id)
    stuffed = {**dict.fromkeys(panel, True), protocol_accounts["bob"].address: True}

    with pytest.raises(ChallengeError, match="unknown"):
        challenges.adjudicate_with_panel(
            ledger, record.challenge_id, stuffed, height=OPEN_HEIGHT + 1
        )
    assert ledger.challenges[record.challenge_id].status == "open"


def test_parse_challenge_enforces_the_field_limits(protocol_accounts: Mapping[str, Any]) -> None:
    sender = protocol_accounts["bob"].address

    def parse(**payload: Any) -> challenges.ChallengeRequest:
        tx = Transaction(
            network="devnet",
            type=TxType.CHALLENGE,
            sender=sender,
            nonce=0,
            fee=0,
            payload={
                "proof_id": PROOF_ID,
                "reason": "bad",
                "evidence": "e",
                "bond": BOND,
                **payload,
            },
        )
        return challenges.parse_challenge(tx)

    assert parse().proof_id == PROOF_ID
    with pytest.raises(ChallengeError, match="non-empty"):
        parse(proof_id="")
    with pytest.raises(ChallengeError, match="reason is longer"):
        parse(reason="x" * (challenges.MAX_REASON_LENGTH + 1))
    with pytest.raises(ChallengeError, match="evidence is longer"):
        parse(evidence="x" * (challenges.MAX_EVIDENCE_LENGTH + 1))
    with pytest.raises(ChallengeError, match="positive bond"):
        parse(bond=0)


def test_open_rejects_oversized_requests(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    with pytest.raises(ChallengeError, match="reason is longer"):
        _open(ledger, protocol_accounts, reason="x" * (challenges.MAX_REASON_LENGTH + 1))
    with pytest.raises(ChallengeError, match="evidence is longer"):
        challenges.open_challenge(
            ledger,
            challenger=protocol_accounts["bob"].address,
            proof_id=PROOF_ID,
            reason="bad trace",
            evidence="x" * (challenges.MAX_EVIDENCE_LENGTH + 1),
            bond=BOND,
            height=OPEN_HEIGHT,
        )


def test_open_rejects_unknown_proofs(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    with pytest.raises(ChallengeError, match="no accepted proof"):
        challenges.open_challenge(
            ledger,
            challenger=protocol_accounts["bob"].address,
            proof_id="00" * 32,
            reason="bad trace",
            evidence="replay diverges",
            bond=BOND,
            height=OPEN_HEIGHT,
        )


def test_open_is_disabled_with_a_zero_window(protocol_accounts: Mapping[str, Any]) -> None:
    ledger = _state(_params(challenge_window=0), protocol_accounts)
    with pytest.raises(ChallengeError, match="disabled on this network"):
        _open(ledger, protocol_accounts)


def test_open_needs_an_affordable_bond(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    ledger.ensure(protocol_accounts["bob"].address).balance = BOND - 1
    with pytest.raises(ChallengeError, match="cannot cover the challenge bond"):
        _open(ledger, protocol_accounts)


def test_resolve_rejects_unknown_and_premature_challenges(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    with pytest.raises(ChallengeError, match="no challenge"):
        challenges.resolve_challenge(ledger, "00" * 32, upheld=True, height=OPEN_HEIGHT)

    record = _open(ledger, protocol_accounts)
    with pytest.raises(ChallengeError, match="before the block that opened it"):
        challenges.resolve_challenge(
            ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT - 1
        )
    assert ledger.challenges[record.challenge_id].status == "open"


def test_resolution_tolerates_a_missing_proof_record(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    record = _open(ledger, protocol_accounts)
    ledger.proofs.pop(GAME_HASH)

    resolved = challenges.resolve_challenge(
        ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1
    )

    assert resolved.status == "upheld"
    assert ledger.ensure(protocol_accounts["alice"].address).bonded == 0


def test_transaction_validation_enforces_the_challenge_window_and_registries(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    tx = _challenge_tx(ledger, protocol_accounts)
    with pytest.raises(TxValidationError, match="closed at height"):
        validate_transaction(tx, ledger.params, ledger, height=PROOF_HEIGHT + 21)

    record = _open(ledger, protocol_accounts)
    with pytest.raises(TxValidationError, match="already has an open challenge"):
        validate_transaction(tx, ledger.params, ledger, height=OPEN_HEIGHT)

    challenges.resolve_challenge(ledger, record.challenge_id, upheld=True, height=OPEN_HEIGHT + 1)
    with pytest.raises(TxValidationError, match="already been invalidated"):
        validate_transaction(tx, ledger.params, ledger, height=OPEN_HEIGHT + 2)


def test_transaction_validation_rejects_a_miner_challenging_its_own_proof(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    transfer = WalletService(ledger.params, ledger).build_transfer(
        protocol_accounts["alice"].keypair,
        protocol_accounts["carol"].address,
        1,
        fee=ledger.params.min_fee,
    )
    tx = sign_transaction(
        protocol_accounts["alice"].keypair,
        transfer.model_copy(
            update={
                "type": TxType.CHALLENGE,
                "payload": {
                    "proof_id": PROOF_ID,
                    "reason": "bad trace",
                    "evidence": "replay diverges at ply 8",
                    "bond": BOND,
                },
            }
        ),
    )

    with pytest.raises(TxValidationError, match="cannot challenge its own proof"):
        validate_transaction(tx, ledger.params, ledger, height=OPEN_HEIGHT)


def test_transaction_validation_is_disabled_with_a_zero_window(
    protocol_accounts: Mapping[str, Any],
) -> None:
    ledger = _state(_params(challenge_window=0), protocol_accounts)
    tx = _challenge_tx(ledger, protocol_accounts)

    with pytest.raises(TxValidationError, match="disabled on this network"):
        validate_transaction(tx, ledger.params, ledger, height=OPEN_HEIGHT)


# -- adjudication: the panel, its votes, and the resolution they trigger ---------


def _voters(params: NetworkParams) -> dict[str, Any]:
    """Two extra keyed, funded accounts: a three-member panel needs more than three."""
    voters: dict[str, Any] = {}
    for index, name in enumerate(("dave", "erin"), start=7):
        keypair = hybrid_keypair_from_seed(bytes([index]) * 32)
        voters[name] = SimpleNamespace(
            keypair=keypair,
            address=encode_address(keypair.public_key_bytes, params),
        )
    return voters


@pytest.fixture()
def wide_accounts(params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> dict[str, Any]:
    return {**protocol_accounts, **_voters(params)}


@pytest.fixture()
def wide_ledger(params: NetworkParams, wide_accounts: Mapping[str, Any]) -> State:
    return _state(params, wide_accounts)


def _bond_named(
    ledger: State, accounts: Mapping[str, Any], *names: str, amount: int = BOND
) -> None:
    for name in names:
        ledger.ensure(accounts[name].address).bonded = amount


def _vote_tx(
    ledger: State,
    account: Any,
    challenge_id: str,
    *,
    upheld: bool,
    nonce: int = 0,
) -> Transaction:
    tx = Transaction(
        network=ledger.network,
        type=TxType.PANEL_VOTE,
        sender=account.address,
        nonce=nonce,
        fee=ledger.params.min_fee,
        payload={
            "challenge_id": challenge_id,
            "upheld": upheld,
            "public_key": account.keypair.public_key_hex,
        },
    )
    return sign_transaction(account.keypair, tx)


def _cast(ledger: State, account: Any, challenge_id: str, *, upheld: bool, height: int) -> None:
    """Validate and apply one vote through the transaction path, as a block does."""
    tx = _vote_tx(ledger, account, challenge_id, upheld=upheld)
    validate_transaction(tx, ledger.params, ledger, height=height)
    apply_transaction(ledger, tx, height=height)


def test_the_panel_is_bonded_odd_and_excludes_the_parties(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    assert challenges.adjudication_panel(wide_ledger, record) == ()  # nobody is bonded

    _bond_named(wide_ledger, wide_accounts, "carol")
    assert challenges.adjudication_panel(wide_ledger, record) == (wide_accounts["carol"].address,)

    # An even candidate set still yields an odd panel: the largest odd subset.
    _bond_named(wide_ledger, wide_accounts, "dave")
    pair = challenges.adjudication_panel(wide_ledger, record)
    assert len(pair) == 1
    assert pair[0] in (wide_accounts["carol"].address, wide_accounts["dave"].address)

    _bond_named(wide_ledger, wide_accounts, "erin")
    panel = challenges.adjudication_panel(wide_ledger, record)
    assert len(panel) == challenges.PANEL_SIZE
    parties = {record.challenger, record.miner}
    assert not parties & set(panel)


def test_a_vote_only_counts_from_a_member_of_the_panel(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    _bond_named(wide_ledger, wide_accounts, "carol")

    stranger = _vote_tx(wide_ledger, wide_accounts["dave"], record.challenge_id, upheld=True)
    with pytest.raises(TxValidationError, match="is not on the panel"):
        validate_transaction(stranger, wide_ledger.params, wide_ledger, height=OPEN_HEIGHT + 1)

    challenger = _vote_tx(wide_ledger, wide_accounts["bob"], record.challenge_id, upheld=True)
    with pytest.raises(TxValidationError, match="is not on the panel"):
        validate_transaction(challenger, wide_ledger.params, wide_ledger, height=OPEN_HEIGHT + 1)


def test_a_vote_needs_a_boolean_verdict_and_a_known_challenge(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    _bond_named(wide_ledger, wide_accounts, "carol")

    unknown = _vote_tx(wide_ledger, wide_accounts["carol"], "00" * 32, upheld=True)
    with pytest.raises(TxValidationError, match="no challenge"):
        validate_transaction(unknown, wide_ledger.params, wide_ledger, height=OPEN_HEIGHT + 1)

    text = _vote_tx(wide_ledger, wide_accounts["carol"], record.challenge_id, upheld=True)
    stringly = sign_transaction(
        wide_accounts["carol"].keypair,
        text.model_copy(update={"payload": {**text.payload, "upheld": "true"}}),
    )
    with pytest.raises(TxValidationError, match="must be a boolean"):
        validate_transaction(stringly, wide_ledger.params, wide_ledger, height=OPEN_HEIGHT + 1)


def test_the_ballot_resolves_the_challenge_when_it_is_complete(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    before = wide_ledger.ensure(wide_accounts["bob"].address).balance
    record = _open(wide_ledger, wide_accounts)
    miner = wide_ledger.ensure(record.miner)
    miner.bonded = 3 * BOND
    miner.coinbase_queue = [(PROOF_HEIGHT, 700), (1, 300)]
    miner.immature = 1_000

    _bond_named(wide_ledger, wide_accounts, "carol", "dave", "erin")
    panel = challenges.adjudication_panel(wide_ledger, record)
    assert len(panel) == challenges.PANEL_SIZE
    by_address = {account.address: account for account in wide_accounts.values()}

    _cast(wide_ledger, by_address[panel[0]], record.challenge_id, upheld=True, height=6)
    assert wide_ledger.challenges[record.challenge_id].status == "open"
    _cast(wide_ledger, by_address[panel[1]], record.challenge_id, upheld=True, height=7)
    assert wide_ledger.challenges[record.challenge_id].status == "open"
    _cast(wide_ledger, by_address[panel[2]], record.challenge_id, upheld=False, height=8)

    resolved = wide_ledger.challenges[record.challenge_id]
    assert resolved.status == "upheld"
    assert resolved.panel == panel
    assert resolved.resolved_height == 8
    assert resolved.votes == {panel[0]: True, panel[1]: True, panel[2]: False}
    # Upheld: the challenger is made whole, the miner is slashed, and the immature
    # coinbase credited at the proof's height is burned.
    assert wide_ledger.ensure(record.challenger).balance == before
    assert wide_ledger.ensure(record.miner).bonded == 0
    assert wide_ledger.ensure(record.miner).coinbase_queue == [(1, 300)]
    assert wide_ledger.ensure(record.miner).immature == 300


def test_a_rejected_ballot_burns_the_challengers_bond(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    wide_ledger.ensure(record.miner).bonded = 3 * BOND
    _bond_named(wide_ledger, wide_accounts, "carol")

    _cast(wide_ledger, wide_accounts["carol"], record.challenge_id, upheld=False, height=6)

    resolved = wide_ledger.challenges[record.challenge_id]
    assert resolved.status == "rejected"
    assert resolved.panel == (wide_accounts["carol"].address,)
    # The bond was subtracted when the challenge opened; not returning it is the burn.
    assert wide_ledger.ensure(record.challenger).balance == 1_000_000 - BOND
    assert wide_ledger.ensure(record.miner).bonded == 3 * BOND


def test_a_member_votes_once_and_a_resolved_challenge_takes_no_more(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    _bond_named(wide_ledger, wide_accounts, "carol")
    _cast(wide_ledger, wide_accounts["carol"], record.challenge_id, upheld=False, height=6)

    again = _vote_tx(
        wide_ledger,
        wide_accounts["carol"],
        record.challenge_id,
        upheld=True,
        nonce=wide_ledger.ensure(wide_accounts["carol"].address).nonce,
    )
    with pytest.raises(TxValidationError, match="already rejected"):
        validate_transaction(again, wide_ledger.params, wide_ledger, height=7)


def test_a_vote_finds_no_panel_without_a_bonded_adjudicator(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    _bond_named(wide_ledger, wide_accounts, "carol")
    _cast(wide_ledger, wide_accounts["carol"], record.challenge_id, upheld=True, height=6)

    # The challenge is already resolved by the only member; bonding somebody else
    # cannot reopen it.
    _bond_named(wide_ledger, wide_accounts, "dave")
    _bond_named(wide_ledger, wide_accounts, "erin")
    assert wide_ledger.challenges[record.challenge_id].status == "upheld"


def test_votes_survive_a_snapshot_round_trip(
    wide_ledger: State, wide_accounts: Mapping[str, Any]
) -> None:
    record = _open(wide_ledger, wide_accounts)
    _bond_named(wide_ledger, wide_accounts, "carol")
    _cast(wide_ledger, wide_accounts["carol"], record.challenge_id, upheld=False, height=6)

    restored = State.from_snapshot(wide_ledger.params, wide_ledger.snapshot())
    assert restored.challenges[record.challenge_id] == wide_ledger.challenges[record.challenge_id]
    assert restored.challenges[record.challenge_id].votes == {wide_accounts["carol"].address: False}
