"""Ledger state: accounts, bonds, registries, epochs and snapshots (plan 5.11).

The state is a plain Python object with ``snapshot``/``restore`` so it can be
persisted, cloned for speculative validation and rebuilt on a reorg. Every field
is deterministic and integer-valued: nothing here reads a clock, a random source
or the network.

Public keys
-----------
An address is ``bech32(0x00 || H("sccp/v1/address", {network, public_key})[:20])``
and Ed25519 is not key-recoverable, so a spend from an address needs that
address's public key from somewhere. Two places supply it: a genesis allocation
declares the key, and a miner's ``register`` transaction stores it. An account
that has neither (a plain recipient) must include a ``public_key`` in its first
transaction payload; consensus then checks that the key re-encodes to the sender
address before storing it. That is the standard first-use key reveal and it keeps
transfers verifiable without a separate key registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from sccp.chain.difficulty import Difficulty, initial_difficulty
from sccp.config import NetworkParams
from sccp.errors import StateError
from sccp.protocol.models import (
    Availability,
    Beacon,
    ClockEvidence,
    MatchCertificate,
    RosterMember,
    SignedMove,
)
from sccp.serialization import merkle_root_hex

ZERO_HASH: Final = "00" * 32
"""Parent of the genesis block: a digest of 32 zero bytes."""

MAX_RECENT_TIMESTAMPS: Final = 11
"""Median-time-past window (PROTOCOL.md sections 12-13)."""


def epoch_of(height: int, params: NetworkParams) -> int:
    """The epoch that contains ``height`` (``height // epoch_length``)."""
    if height < 0:
        raise StateError(f"height must be non-negative, got {height}")
    return height // params.epoch_length


def epoch_start_height(epoch: int, params: NetworkParams) -> int:
    """First height of ``epoch``."""
    if epoch < 0:
        raise StateError(f"epoch must be non-negative, got {epoch}")
    return epoch * params.epoch_length


def epoch_end_height(epoch: int, params: NetworkParams) -> int:
    """Last height of ``epoch`` (``(epoch + 1) * epoch_length - 1``)."""
    return epoch_start_height(epoch, params) + params.epoch_length - 1


def first_matchable_epoch(params: NetworkParams) -> int:
    """Availability is committed ``availability_lead`` epochs ahead, so 2 is first."""
    return params.availability_lead


@dataclass(slots=True)
class Account:
    """One account's balances, bond, nonce and registration metadata."""

    balance: int = 0
    """Spendable balance in base units."""
    immature: int = 0
    """Coinbase value that has not reached ``coinbase_maturity`` yet."""
    bonded: int = 0
    """Bonded balance: owned by the account, unspendable until unbonding."""
    unbonding_height: int | None = None
    """Height at which the bond becomes withdrawable, once unbonding has started."""
    nonce: int = 0
    """Next valid transaction nonce."""
    registered_until_epoch: int | None = None
    """Last epoch for which the account is a registered miner, if any."""
    endpoint: str | None = None
    """Informational miner endpoint; consensus never fetches it."""
    engine: str | None = None
    """Engine fingerprint declared at registration."""
    public_key: str | None = None
    """Ed25519 public key (hex), set by genesis, ``register`` or first reveal."""
    banned_until: int = 0
    """Height until which the account is excluded from the roster."""
    coinbase_queue: list[tuple[int, int]] = field(default_factory=list)
    """``(credit_height, amount)`` pairs waiting for maturity, oldest first."""

    def __post_init__(self) -> None:
        for name in ("balance", "immature", "bonded", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StateError(f"account {name} must be a non-negative integer")

    @property
    def total(self) -> int:
        """Everything the account owns: spendable, immature and bonded."""
        return self.balance + self.immature + self.bonded

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping used by ``state_root`` and snapshots."""
        return {
            "balance": self.balance,
            "immature": self.immature,
            "bonded": self.bonded,
            "unbonding_height": self.unbonding_height,
            "nonce": self.nonce,
            "registered_until_epoch": self.registered_until_epoch,
            "endpoint": self.endpoint,
            "engine": self.engine,
            "public_key": self.public_key,
            "banned_until": self.banned_until,
            "coinbase_queue": [[height, amount] for height, amount in self.coinbase_queue],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> Account:
        """Rebuild an account from :meth:`as_dict` output."""

        def _int(key: str, default: int = 0) -> int:
            value = document.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateError(f"account field {key!r} must be an integer")
            return value

        def _optional_int(key: str) -> int | None:
            value = document.get(key)
            if value is None:
                return None
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateError(f"account field {key!r} must be an integer or null")
            return value

        def _optional_str(key: str) -> str | None:
            value = document.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise StateError(f"account field {key!r} must be a string or null")
            return value

        queue_raw = document.get("coinbase_queue", [])
        if not isinstance(queue_raw, list):
            raise StateError("account field 'coinbase_queue' must be a list")
        queue: list[tuple[int, int]] = []
        for item in queue_raw:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(part, int) and not isinstance(part, bool) for part in item)
            ):
                raise StateError("coinbase_queue entries must be [height, amount] pairs")
            queue.append((item[0], item[1]))

        return cls(
            balance=_int("balance"),
            immature=_int("immature"),
            bonded=_int("bonded"),
            unbonding_height=_optional_int("unbonding_height"),
            nonce=_int("nonce"),
            registered_until_epoch=_optional_int("registered_until_epoch"),
            endpoint=_optional_str("endpoint"),
            engine=_optional_str("engine"),
            public_key=_optional_str("public_key"),
            banned_until=_int("banned_until"),
            coinbase_queue=queue,
        )


@dataclass(slots=True)
class ProofRecord:
    """A proof accepted into the chain, keyed by its game hash."""

    proof_id: str
    game_hash: str
    height: int
    winner: str
    miner: str = ""
    """Who announced the proof, used when a challenge slashes its coinbase."""


@dataclass(slots=True)
class MatchRecord:
    """A match the chain has seen a proof for, used for duplicate detection."""

    epoch: int
    round: int
    seats: tuple[str, str]


@dataclass(slots=True)
class MinerStats:
    """Per-miner counters behind the ``opponent_cooldown`` rule."""

    wins: int = 0
    games: int = 0
    last_opponent_epoch: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class WitnessRecord:
    """A registered clock witness and its slashable bond."""

    public_key: str
    endpoint: str
    bond: int
    registered_at_height: int
    until_epoch: int


def _as_int(value: object, name: str) -> int:
    """Coerce a snapshot field to ``int`` or raise :class:`StateError`.

    Snapshots round-trip through JSON, whose object keys are always strings, so a
    decimal ``str`` is accepted where the in-memory form holds an ``int``.
    """
    if isinstance(value, bool):
        raise StateError(f"{name} must be an integer in a state snapshot")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    raise StateError(f"{name} must be an integer in a state snapshot")


@dataclass(slots=True)
class EpochRecord:
    """Everything the chain knows about one epoch (PROTOCOL.md section 6).

    Created when the epoch's first block is applied, from the reveal set
    collected during the previous epoch and the roster registered at ``H_e``.
    Proofs that reference the epoch are verified against this record, which is
    why it is part of the state root rather than a cache.
    """

    epoch: int
    beacon: Beacon | None = None
    roster: dict[str, RosterMember] = field(default_factory=dict)
    availability: dict[str, str] = field(default_factory=dict)
    quotas: dict[str, int] = field(default_factory=dict)
    roster_root: str = ""
    ended_at_height: int | None = None
    matchable: bool = False

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping for snapshots."""
        return {
            "epoch": self.epoch,
            "beacon": self.beacon.model_dump(mode="json") if self.beacon is not None else None,
            "roster": {
                address: member.model_dump(mode="json")
                for address, member in sorted(self.roster.items())
            },
            "availability": dict(sorted(self.availability.items())),
            "quotas": dict(sorted(self.quotas.items())),
            "roster_root": self.roster_root,
            "ended_at_height": self.ended_at_height,
            "matchable": self.matchable,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> EpochRecord:
        """Rebuild an epoch record from :meth:`as_dict` output."""
        beacon_raw = document.get("beacon")
        roster_raw = document.get("roster") or {}
        availability_raw = document.get("availability") or {}
        quotas_raw = document.get("quotas") or {}
        if not isinstance(roster_raw, Mapping) or not isinstance(availability_raw, Mapping):
            raise StateError("epoch record rosters must be mappings")
        if not isinstance(quotas_raw, Mapping):
            raise StateError("epoch record quotas must be a mapping")
        return cls(
            epoch=_as_int(document.get("epoch"), "epoch"),
            beacon=Beacon.model_validate(beacon_raw) if beacon_raw is not None else None,
            roster={
                str(address): RosterMember.model_validate(member)
                for address, member in roster_raw.items()
            },
            availability={str(key): str(value) for key, value in availability_raw.items()},
            quotas={str(key): _as_int(value, "quota") for key, value in quotas_raw.items()},
            roster_root=str(document.get("roster_root") or ""),
            ended_at_height=(
                None
                if document.get("ended_at_height") is None
                else _as_int(document.get("ended_at_height"), "ended_at_height")
            ),
            matchable=bool(document.get("matchable", False)),
        )


@dataclass(slots=True)
class TimeoutClaimRecord:
    """A bonded timeout claim and its dispute/finalisation lifecycle."""

    claim_id: str
    claimant: str
    accused: str
    match_id: str
    bond: int
    opened_height: int
    deadline_height: int
    certificate: MatchCertificate
    clock_evidence: ClockEvidence
    last_move: SignedMove
    disputed_height: int | None = None
    finalised_height: int | None = None
    slashed: bool = False

    @property
    def open(self) -> bool:
        """Whether the claim is neither disputed, finalised nor slashed."""
        return self.disputed_height is None and self.finalised_height is None and not self.slashed

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping for snapshots."""
        return {
            "claim_id": self.claim_id,
            "claimant": self.claimant,
            "accused": self.accused,
            "match_id": self.match_id,
            "bond": self.bond,
            "opened_height": self.opened_height,
            "deadline_height": self.deadline_height,
            "certificate": self.certificate.model_dump(mode="json"),
            "clock_evidence": self.clock_evidence.model_dump(mode="json"),
            "last_move": self.last_move.model_dump(mode="json"),
            "disputed_height": self.disputed_height,
            "finalised_height": self.finalised_height,
            "slashed": self.slashed,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> TimeoutClaimRecord:
        """Rebuild a claim record from :meth:`as_dict` output."""
        return cls(
            claim_id=str(document["claim_id"]),
            claimant=str(document["claimant"]),
            accused=str(document["accused"]),
            match_id=str(document["match_id"]),
            bond=_as_int(document.get("bond"), "bond"),
            opened_height=_as_int(document.get("opened_height"), "opened_height"),
            deadline_height=_as_int(document.get("deadline_height"), "deadline_height"),
            certificate=MatchCertificate.model_validate(document["certificate"]),
            clock_evidence=ClockEvidence.model_validate(document["clock_evidence"]),
            last_move=SignedMove.model_validate(document["last_move"]),
            disputed_height=(
                None
                if document.get("disputed_height") is None
                else _as_int(document.get("disputed_height"), "disputed_height")
            ),
            finalised_height=(
                None
                if document.get("finalised_height") is None
                else _as_int(document.get("finalised_height"), "finalised_height")
            ),
            slashed=bool(document.get("slashed", False)),
        )


@dataclass(slots=True)
class ChallengeRecord:
    """A bonded quality challenge against an accepted proof.

    The bond is locked when the challenge opens. Upholding the challenge returns
    the bond and slashes the proof miner; rejecting it burns the bond. A record is
    immutable once resolved so an adjudication cannot be replayed under a
    different verdict.
    """

    challenge_id: str
    proof_id: str
    game_hash: str
    challenger: str
    miner: str
    reason: str
    evidence: str
    bond: int
    opened_height: int
    deadline_height: int
    status: str = "open"
    resolved_height: int | None = None
    panel: tuple[str, ...] = ()

    @property
    def open(self) -> bool:
        """Whether the challenge is still awaiting adjudication."""
        return self.status == "open"

    @property
    def upheld(self) -> bool:
        """Whether the challenge invalidated its proof."""
        return self.status == "upheld"

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping for snapshots."""
        return {
            "challenge_id": self.challenge_id,
            "proof_id": self.proof_id,
            "game_hash": self.game_hash,
            "challenger": self.challenger,
            "miner": self.miner,
            "reason": self.reason,
            "evidence": self.evidence,
            "bond": self.bond,
            "opened_height": self.opened_height,
            "deadline_height": self.deadline_height,
            "status": self.status,
            "resolved_height": self.resolved_height,
            "panel": list(self.panel),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> ChallengeRecord:
        """Rebuild a challenge from :meth:`as_dict` output."""
        panel_raw = document.get("panel") or []
        if not isinstance(panel_raw, list):
            raise StateError("challenge panel must be a list")
        status = str(document.get("status") or "open")
        if status not in {"open", "upheld", "rejected"}:
            raise StateError(f"unknown challenge status: {status!r}")
        return cls(
            challenge_id=str(document["challenge_id"]),
            proof_id=str(document["proof_id"]),
            game_hash=str(document["game_hash"]),
            challenger=str(document["challenger"]),
            miner=str(document.get("miner") or ""),
            reason=str(document.get("reason") or ""),
            evidence=str(document.get("evidence") or ""),
            bond=_as_int(document.get("bond"), "challenge bond"),
            opened_height=_as_int(document.get("opened_height"), "opened_height"),
            deadline_height=_as_int(document.get("deadline_height"), "deadline_height"),
            status=status,
            resolved_height=(
                None
                if document.get("resolved_height") is None
                else _as_int(document.get("resolved_height"), "resolved_height")
            ),
            panel=tuple(str(member) for member in panel_raw),
        )


@dataclass(slots=True)
class State:
    """The complete ledger state after applying a block.

    ``snapshot`` produces a canonical JSON tree and ``restore`` rebuilds it, so
    persistence and reorgs use exactly the same code path as tests.
    """

    params: NetworkParams
    height: int = 0
    tip_hash: str = ZERO_HASH
    accounts: dict[str, Account] = field(default_factory=dict)
    proofs: dict[str, ProofRecord] = field(default_factory=dict)
    """``game_hash -> ProofRecord`` for every proof accepted into the chain."""
    proof_ids: dict[str, str] = field(default_factory=dict)
    """``proof_id -> game_hash``, so a proof id cannot be reused."""
    matches: dict[str, MatchRecord] = field(default_factory=dict)
    miner_stats: dict[str, MinerStats] = field(default_factory=dict)
    epochs: dict[int, EpochRecord] = field(default_factory=dict)
    availability: dict[int, dict[str, Availability]] = field(default_factory=dict)
    """``epoch -> {miner: availability tx}`` committed ``availability_lead`` ahead."""
    reveals: dict[int, dict[str, str]] = field(default_factory=dict)
    """``epoch -> {miner: nonce}`` revealed during the previous epoch."""
    witnesses: dict[str, WitnessRecord] = field(default_factory=dict)
    timeout_claims: dict[str, TimeoutClaimRecord] = field(default_factory=dict)
    challenges: dict[str, ChallengeRecord] = field(default_factory=dict)
    """``challenge_id -> ChallengeRecord`` for every opened quality challenge."""
    difficulty: Difficulty | None = None
    last_k_change_height: int | None = None
    timestamps: list[int] = field(default_factory=list)
    """The last :data:`MAX_RECENT_TIMESTAMPS` block timestamps, oldest first."""
    minted: int = 0
    """Total subsidy ever created, for the ``max_supply`` clamp."""

    def __post_init__(self) -> None:
        if self.difficulty is None:
            self.difficulty = initial_difficulty(self.params)
        if self.height < 0:
            raise StateError("state height must be non-negative")
        if self.minted < 0:
            raise StateError("minted supply must be non-negative")

    # -- accessors ----------------------------------------------------------

    @property
    def network(self) -> str:
        """The network id this state belongs to."""
        return self.params.network_id

    def account(self, address: str) -> Account:
        """The account at ``address``, or a zeroed one that is not yet stored."""
        return self.accounts.get(address, Account())

    def ensure(self, address: str) -> Account:
        """The stored account at ``address``, created on first use."""
        existing = self.accounts.get(address)
        if existing is None:
            existing = Account()
            self.accounts[address] = existing
        return existing

    def stats(self, address: str) -> MinerStats:
        """The miner counters for ``address``, created on first use."""
        existing = self.miner_stats.get(address)
        if existing is None:
            existing = MinerStats()
            self.miner_stats[address] = existing
        return existing

    def current_epoch(self) -> int:
        """The epoch of the current tip."""
        return epoch_of(self.height, self.params)

    def epoch_record(self, epoch: int) -> EpochRecord | None:
        """The stored record for ``epoch``, if the epoch has opened."""
        return self.epochs.get(epoch)

    def roster_for(self, epoch: int) -> dict[str, RosterMember]:
        """The matchable roster of ``epoch`` (empty when unmatched or unknown)."""
        record = self.epochs.get(epoch)
        if record is None or not record.matchable:
            return {}
        return dict(record.roster)

    def public_keys(self) -> dict[str, str]:
        """``address -> hybrid public key hex`` for every address the chain trusts.

        The map holds the full 1985-byte blob exactly as it was registered; the
        per-stream verifiers project the Ed25519 half when they check a move or a
        clock receipt. Keeping one authoritative representation is what removes
        the "which scheme is this key?" question from the ledger.
        """
        keys: dict[str, str] = {}
        for address, account in self.accounts.items():
            if account.public_key:
                keys[address] = account.public_key
        for address, witness in self.witnesses.items():
            keys.setdefault(address, witness.public_key)
        return keys

    def known_game_hashes(self) -> frozenset[str]:
        """Every game hash already accepted into the chain."""
        return frozenset(self.proofs)

    def known_proof_ids(self) -> frozenset[str]:
        """Every proof id already accepted into the chain."""
        return frozenset(self.proof_ids)

    def matchable(self, address: str, epoch: int) -> bool:
        """Whether ``address`` is a registered, bonded, unbonded, unexpired miner."""
        account = self.accounts.get(address)
        if account is None or account.public_key is None:
            return False
        if account.registered_until_epoch is None:
            return False
        if account.registered_until_epoch < epoch:
            return False
        if account.bonded < self.params.min_bond:
            return False
        return self.height >= account.banned_until

    # -- commitments --------------------------------------------------------

    def state_root(self) -> str:
        """Merkle root over ``address -> account`` leaves (PROTOCOL.md sections 12-13)."""
        return merkle_root_hex(
            {address: account.as_dict() for address, account in self.accounts.items()}
        )

    @staticmethod
    def roster_root_of(roster: Mapping[str, RosterMember]) -> str:
        """Merkle root over the epoch's active roster.

        Only the consensus-relevant fields are committed: the registered public
        key and the miner's committed game quota.
        """
        return merkle_root_hex(
            {
                address: {"public_key": member.public_key, "max_games": member.max_games}
                for address, member in roster.items()
            }
        )

    # -- balances -----------------------------------------------------------

    def credit_coinbase(self, miner: str, amount: int, height: int) -> None:
        """Credit a block reward that matures ``coinbase_maturity`` blocks later."""
        if amount < 0:
            raise StateError("coinbase amount must be non-negative")
        if amount == 0:
            return
        account = self.ensure(miner)
        account.immature += amount
        account.coinbase_queue.append((height, amount))
        self.minted += amount

    def release_matured(self, height: int) -> int:
        """Move matured coinbase value into spendable balances; return the total."""
        maturity = self.params.coinbase_maturity
        total = 0
        for account in self.accounts.values():
            if not account.coinbase_queue:
                continue
            released = 0
            pending: list[tuple[int, int]] = []
            for credit_height, amount in account.coinbase_queue:
                if credit_height + maturity <= height:
                    released += amount
                else:
                    pending.append((credit_height, amount))
            if released:
                account.coinbase_queue = pending
                account.immature -= released
                account.balance += released
                total += released
        return total

    def spendable(self, address: str) -> int:
        """The balance a transaction may draw on: spendable plus matured coinbase."""
        return self.account(address).balance

    # -- epochs -------------------------------------------------------------

    def open_epoch(self, epoch: int, *, beacon: Beacon | None, height: int) -> EpochRecord:
        """Build and store the record for ``epoch`` at its first block.

        The roster is every registered, bonded, unexpired, unbanned miner at
        ``height`` that revealed a valid nonce for ``epoch``; a miner's quota is
        the ``max_games`` it committed to, capped by ``rounds_per_epoch``.
        Epochs before :func:`first_matchable_epoch` are recorded but not
        matchable, because availability is committed two epochs ahead.
        """
        params = self.params
        roster: dict[str, RosterMember] = {}
        commitments: dict[str, str] = {}
        quotas: dict[str, int] = {}
        if beacon is not None and epoch >= first_matchable_epoch(params):
            availability = self.availability.get(epoch, {})
            reveals = self.reveals.get(epoch, {})
            for address in sorted(availability):
                if address not in reveals:
                    continue
                if not self.matchable(address, epoch):
                    continue
                account = self.account(address)
                quota = min(availability[address].max_games, params.rounds_per_epoch)
                roster[address] = RosterMember(
                    public_key=account.public_key or "",
                    engine=account.engine,
                    max_games=quota,
                )
                commitments[address] = availability[address].commitment
                quotas[address] = quota
        record = EpochRecord(
            epoch=epoch,
            beacon=beacon,
            roster=roster,
            availability=commitments,
            quotas=quotas,
            roster_root=self.roster_root_of(roster),
            matchable=bool(roster) and epoch >= first_matchable_epoch(params),
        )
        self.epochs[epoch] = record
        if epoch > 0:
            previous = self.epochs.get(epoch - 1)
            if previous is not None and previous.ended_at_height is None:
                previous.ended_at_height = height - 1
        return record

    def close_tip_epoch(self) -> None:
        """Mark the epoch of the current tip as ended at the current height."""
        record = self.epochs.get(self.current_epoch())
        if record is not None and record.ended_at_height is None:
            record.ended_at_height = self.height

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """A canonical JSON tree that :meth:`from_snapshot` can rebuild."""
        return {
            "network": self.network,
            "height": self.height,
            "tip_hash": self.tip_hash,
            "accounts": {
                address: account.as_dict() for address, account in sorted(self.accounts.items())
            },
            "proofs": {
                game_hash: {
                    "proof_id": record.proof_id,
                    "game_hash": record.game_hash,
                    "height": record.height,
                    "winner": record.winner,
                    "miner": record.miner,
                }
                for game_hash, record in sorted(self.proofs.items())
            },
            "proof_ids": dict(sorted(self.proof_ids.items())),
            "matches": {
                match_id: {
                    "epoch": record.epoch,
                    "round": record.round,
                    "seats": list(record.seats),
                }
                for match_id, record in sorted(self.matches.items())
            },
            "miner_stats": {
                address: {
                    "wins": stats.wins,
                    "games": stats.games,
                    "last_opponent_epoch": dict(sorted(stats.last_opponent_epoch.items())),
                }
                for address, stats in sorted(self.miner_stats.items())
            },
            "epochs": {
                str(epoch): record.as_dict() for epoch, record in sorted(self.epochs.items())
            },
            "availability": {
                str(epoch): {
                    miner: value.model_dump(mode="json") for miner, value in sorted(entries.items())
                }
                for epoch, entries in sorted(self.availability.items())
            },
            "reveals": {
                str(epoch): dict(sorted(entries.items()))
                for epoch, entries in sorted(self.reveals.items())
            },
            "witnesses": {
                address: {
                    "public_key": witness.public_key,
                    "endpoint": witness.endpoint,
                    "bond": witness.bond,
                    "registered_at_height": witness.registered_at_height,
                    "until_epoch": witness.until_epoch,
                }
                for address, witness in sorted(self.witnesses.items())
            },
            "timeout_claims": {
                claim_id: record.as_dict()
                for claim_id, record in sorted(self.timeout_claims.items())
            },
            "challenges": {
                challenge_id: record.as_dict()
                for challenge_id, record in sorted(self.challenges.items())
            },
            "difficulty": self.difficulty.as_dict() if self.difficulty else None,
            "last_k_change_height": self.last_k_change_height,
            "timestamps": list(self.timestamps),
            "minted": self.minted,
        }

    @classmethod
    def from_snapshot(cls, params: NetworkParams, document: Mapping[str, object]) -> State:
        """Rebuild a state from :meth:`snapshot` output."""
        network = document.get("network")
        if network != params.network_id:
            raise StateError(
                f"snapshot is for {network!r}, but the parameters are {params.network_id!r}"
            )
        accounts_raw = _as_mapping(document.get("accounts"), "accounts")
        proofs_raw = _as_mapping(document.get("proofs"), "proofs")
        proof_ids_raw = _as_mapping(document.get("proof_ids"), "proof_ids")
        matches_raw = _as_mapping(document.get("matches"), "matches")
        stats_raw = _as_mapping(document.get("miner_stats"), "miner_stats")
        epochs_raw = _as_mapping(document.get("epochs"), "epochs")
        availability_raw = _as_mapping(document.get("availability"), "availability")
        reveals_raw = _as_mapping(document.get("reveals"), "reveals")
        witnesses_raw = _as_mapping(document.get("witnesses"), "witnesses")
        claims_raw = _as_mapping(document.get("timeout_claims"), "timeout_claims")
        challenges_raw = _as_mapping(document.get("challenges"), "challenges")

        accounts = {
            str(address): Account.from_dict(_as_mapping(entry, "account"))
            for address, entry in accounts_raw.items()
        }
        proofs: dict[str, ProofRecord] = {}
        for game_hash, entry in proofs_raw.items():
            item = _as_mapping(entry, "proof record")
            proofs[str(game_hash)] = ProofRecord(
                proof_id=str(item["proof_id"]),
                game_hash=str(item["game_hash"]),
                height=_as_int(item.get("height"), "proof height"),
                winner=str(item["winner"]),
                miner=str(item.get("miner") or item["winner"]),
            )
        matches: dict[str, MatchRecord] = {}
        for match, entry in matches_raw.items():
            item = _as_mapping(entry, "match record")
            seats = item.get("seats")
            if not isinstance(seats, list) or len(seats) != 2:
                raise StateError("match record seats must be a two-element list")
            matches[str(match)] = MatchRecord(
                epoch=_as_int(item.get("epoch"), "match epoch"),
                round=_as_int(item.get("round"), "match round"),
                seats=(str(seats[0]), str(seats[1])),
            )
        stats: dict[str, MinerStats] = {}
        for address, entry in stats_raw.items():
            item = _as_mapping(entry, "miner stats")
            opponents = _as_mapping(item.get("last_opponent_epoch"), "opponents")
            stats[str(address)] = MinerStats(
                wins=_as_int(item.get("wins"), "wins"),
                games=_as_int(item.get("games"), "games"),
                last_opponent_epoch={
                    str(key): _as_int(value, "opponent epoch") for key, value in opponents.items()
                },
            )
        epochs = {
            _as_int(key, "epoch"): EpochRecord.from_dict(_as_mapping(entry, "epoch record"))
            for key, entry in epochs_raw.items()
        }
        availability = {
            _as_int(epoch, "epoch"): {
                str(miner): Availability.model_validate(miner_entry)
                for miner, miner_entry in _as_mapping(entries, "availability").items()
            }
            for epoch, entries in availability_raw.items()
        }
        reveals = {
            _as_int(epoch, "epoch"): {
                str(miner): str(nonce) for miner, nonce in _as_mapping(entries, "reveals").items()
            }
            for epoch, entries in reveals_raw.items()
        }

        witnesses: dict[str, WitnessRecord] = {}
        for address, entry in witnesses_raw.items():
            item = _as_mapping(entry, "witness")
            witnesses[str(address)] = WitnessRecord(
                public_key=str(item["public_key"]),
                endpoint=str(item.get("endpoint") or ""),
                bond=_as_int(item.get("bond"), "witness bond"),
                registered_at_height=_as_int(
                    item.get("registered_at_height"), "registered_at_height"
                ),
                until_epoch=_as_int(item.get("until_epoch"), "until_epoch"),
            )
        claims = {
            str(claim_id): TimeoutClaimRecord.from_dict(_as_mapping(entry, "timeout claim"))
            for claim_id, entry in claims_raw.items()
        }
        challenges = {
            str(challenge_id): ChallengeRecord.from_dict(_as_mapping(entry, "challenge record"))
            for challenge_id, entry in challenges_raw.items()
        }
        difficulty = None
        if document.get("difficulty") is not None:
            item = _as_mapping(document.get("difficulty"), "difficulty")
            difficulty = Difficulty(
                wins_required=_as_int(item.get("wins_required"), "wins_required"),
                seal_bits=_as_int(item.get("seal_bits"), "seal_bits"),
            )
        timestamps_raw = document.get("timestamps") or []
        if not isinstance(timestamps_raw, list):
            raise StateError("snapshot timestamps must be a list")
        return cls(
            params=params,
            height=_as_int(document.get("height"), "height"),
            tip_hash=str(document.get("tip_hash") or ZERO_HASH),
            accounts=accounts,
            proofs=proofs,
            proof_ids={str(key): str(value) for key, value in proof_ids_raw.items()},
            matches=matches,
            miner_stats=stats,
            epochs=epochs,
            availability=availability,
            reveals=reveals,
            witnesses=witnesses,
            timeout_claims=claims,
            challenges=challenges,
            difficulty=difficulty,
            last_k_change_height=(
                None
                if document.get("last_k_change_height") is None
                else _as_int(document.get("last_k_change_height"), "last_k_change_height")
            ),
            timestamps=[_as_int(value, "timestamp") for value in timestamps_raw],
            minted=_as_int(document.get("minted"), "minted"),
        )

    def copy(self) -> State:
        """A deep, independent copy via the snapshot round-trip."""
        return State.from_snapshot(self.params, self.snapshot())


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    """Require a mapping field in a snapshot."""
    if not isinstance(value, Mapping):
        raise StateError(f"{name} must be a mapping in a state snapshot")
    return value


__all__ = [
    "MAX_RECENT_TIMESTAMPS",
    "ZERO_HASH",
    "Account",
    "EpochRecord",
    "MatchRecord",
    "MinerStats",
    "ProofRecord",
    "State",
    "TimeoutClaimRecord",
    "WitnessRecord",
    "epoch_end_height",
    "epoch_of",
    "epoch_start_height",
    "first_matchable_epoch",
]
