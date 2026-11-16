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


def reveal_deadline_tip(epoch: int, params: NetworkParams) -> int:
    """The last tip at which a reveal for ``epoch`` can still be built.

    Consensus accepts a reveal only in a block of the epoch before the one it
    targets (``epoch(block) + 1``), and a block built on tip ``t`` has height
    ``t + 1``. The last usable tip is therefore one below the last height of
    ``epoch - 1``. Missing it voids the commitment for that epoch: there is no
    later window, because the following epoch is already outside the range.
    """
    if epoch < 1:
        raise StateError(f"epoch must be at least 1, got {epoch}")
    return epoch_end_height(epoch - 1, params) - 1


@dataclass(slots=True)
class Account:
    """One account's balances, bond, nonce and registration metadata.

    ``balance`` is the native SCCP coin. ``assets`` holds non-native variant
    tokens (Chess960 first) keyed by their derived asset id; both have a matching
    ``immature`` counter for coinbase rewards that have not matured yet.
    """

    balance: int = 0
    """Spendable native balance in base units."""
    immature: int = 0
    """Native coinbase value that has not reached ``coinbase_maturity`` yet."""
    assets: dict[str, int] = field(default_factory=dict)
    """Spendable variant-token balances keyed by asset id."""
    immature_assets: dict[str, int] = field(default_factory=dict)
    """Variant-token coinbase value still waiting for maturity."""
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
    variant: str = "standard"
    """Chess variant this miner registered for; standard is native SCCP."""
    public_key: str | None = None
    """Ed25519 public key (hex), set by genesis, ``register`` or first reveal."""
    banned_until: int = 0
    """Height until which the account is excluded from the roster."""
    coinbase_queue: list[tuple[int, int]] = field(default_factory=list)
    """Native ``(credit_height, amount)`` pairs waiting for maturity, oldest first."""
    asset_coinbase_queue: list[tuple[int, int, str]] = field(default_factory=list)
    """Variant ``(credit_height, amount, asset_id)`` rewards waiting for maturity."""

    def __post_init__(self) -> None:
        for name in ("balance", "immature", "bonded", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StateError(f"account {name} must be a non-negative integer")
        for label, mapping in (("assets", self.assets), ("immature_assets", self.immature_assets)):
            for asset, amount in mapping.items():
                if not isinstance(asset, str) or not asset:
                    raise StateError(f"account {label} keys must be asset ids")
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise StateError(f"account {label} amounts must be non-negative integers")

    @property
    def total(self) -> int:
        """Everything the account owns: spendable, immature and bonded."""
        return self.balance + self.immature + self.bonded

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping used by ``state_root`` and snapshots.

        Empty asset fields and the default variant are omitted, so a
        standard-only account has exactly the protocol-v1 leaf and existing
        state roots stay stable.
        """
        document: dict[str, object] = {
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
        if self.variant != "standard":
            document["variant"] = self.variant
        if self.assets:
            document["assets"] = dict(sorted(self.assets.items()))
        if self.immature_assets:
            document["immature_assets"] = dict(sorted(self.immature_assets.items()))
        if self.asset_coinbase_queue:
            document["asset_coinbase_queue"] = [
                [height, amount, asset] for height, amount, asset in self.asset_coinbase_queue
            ]
        return document

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

        def _assets(key: str) -> dict[str, int]:
            raw = document.get(key, {})
            if raw is None:
                return {}
            if not isinstance(raw, Mapping):
                raise StateError(f"account field {key!r} must be an object")
            balances: dict[str, int] = {}
            for asset, amount in raw.items():
                if not isinstance(asset, str) or not asset:
                    raise StateError(f"account field {key!r} keys must be asset ids")
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise StateError(f"account field {key!r} amounts must be non-negative")
                balances[asset] = amount
            return balances

        asset_queue_raw = document.get("asset_coinbase_queue", [])
        if not isinstance(asset_queue_raw, list):
            raise StateError("account field 'asset_coinbase_queue' must be a list")
        asset_queue: list[tuple[int, int, str]] = []
        for item in asset_queue_raw:
            if (
                not isinstance(item, list)
                or len(item) != 3
                or not isinstance(item[0], int)
                or isinstance(item[0], bool)
                or not isinstance(item[1], int)
                or isinstance(item[1], bool)
                or not isinstance(item[2], str)
            ):
                raise StateError("asset_coinbase_queue entries must be [height, amount, asset]")
            asset_queue.append((item[0], item[1], item[2]))

        return cls(
            balance=_int("balance"),
            immature=_int("immature"),
            assets=_assets("assets"),
            immature_assets=_assets("immature_assets"),
            bonded=_int("bonded"),
            unbonding_height=_optional_int("unbonding_height"),
            nonce=_int("nonce"),
            registered_until_epoch=_optional_int("registered_until_epoch"),
            endpoint=_optional_str("endpoint"),
            engine=_optional_str("engine"),
            variant=_optional_str("variant") or "standard",
            public_key=_optional_str("public_key"),
            banned_until=_int("banned_until"),
            coinbase_queue=queue,
            asset_coinbase_queue=asset_queue,
        )


@dataclass(slots=True)
class Pool:
    """One constant-product liquidity pool, including its LP share ledger.

    ``asset_a`` is the empty string for native SCCP and a 64-hex asset id
    otherwise. The pair is ordered, so a pool has exactly one representation and
    :func:`sccp.chain.dex.pool_id` can be derived from either side order.
    """

    pool_id: str
    asset_a: str
    asset_b: str
    reserve_a: int
    reserve_b: int
    total_shares: int
    shares: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.pool_id) != 64 or self.pool_id != self.pool_id.lower():
            raise StateError("pool id must be a 32-byte lowercase hex digest")
        try:
            bytes.fromhex(self.pool_id)
        except ValueError:
            raise StateError("pool id must be hex") from None
        if not self.asset_b or self.asset_a == self.asset_b:
            raise StateError("a pool needs two different assets")
        if self.asset_a > self.asset_b:
            raise StateError("pool assets must be ordered")
        for label, asset in (("asset_a", self.asset_a), ("asset_b", self.asset_b)):
            if asset and (len(asset) != 64 or asset != asset.lower()):
                raise StateError(f"pool {label} must be an asset id")
        for label in ("reserve_a", "reserve_b", "total_shares"):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StateError(f"pool {label} must be a non-negative integer")
        for address, amount in self.shares.items():
            if not isinstance(address, str) or not address:
                raise StateError("pool share addresses must be non-empty strings")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise StateError("pool share amounts must be positive integers")
        if sum(self.shares.values()) != self.total_shares:
            raise StateError("pool total_shares must equal the sum of LP shares")

    def as_dict(self) -> dict[str, object]:
        """Canonical mapping committed by ``state_root`` and written to snapshots."""
        return {
            "asset_a": self.asset_a,
            "asset_b": self.asset_b,
            "reserve_a": self.reserve_a,
            "reserve_b": self.reserve_b,
            "total_shares": self.total_shares,
            "shares": dict(sorted(self.shares.items())),
        }

    @classmethod
    def from_dict(cls, pool_id: str, document: Mapping[str, object]) -> Pool:
        """Rebuild a pool from :meth:`as_dict` output."""

        def _int(key: str) -> int:
            value = document.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateError(f"pool field {key!r} must be an integer")
            return value

        raw_shares = document.get("shares", {})
        if not isinstance(raw_shares, Mapping):
            raise StateError("pool field 'shares' must be a mapping")
        asset_a = document.get("asset_a", "")
        asset_b = document.get("asset_b", "")
        if not isinstance(asset_a, str) or not isinstance(asset_b, str):
            raise StateError("pool assets must be strings")
        shares: dict[str, int] = {}
        for address, amount in raw_shares.items():
            if not isinstance(address, str) or not address:
                raise StateError("pool share addresses must be non-empty strings")
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise StateError("pool share amounts must be integers")
            shares[address] = amount
        return cls(
            pool_id=pool_id,
            asset_a=asset_a,
            asset_b=asset_b,
            reserve_a=_int("reserve_a"),
            reserve_b=_int("reserve_b"),
            total_shares=_int("total_shares"),
            shares=shares,
        )


@dataclass(slots=True)
class ProofRecord:
    """A proof accepted into the chain, keyed by its game hash."""

    proof_id: str
    game_hash: str
    height: int
    winner: str
    miner: str = ""
    """Who announced the proof, recorded for audit."""
    variant: str = "standard"
    """The variant the game was played under."""


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


def _roster_member_document(member: RosterMember) -> dict[str, object]:
    """Serialize a roster entry, omitting the standard variant's default.

    Existing standard-only snapshots therefore keep the exact document they had
    before variant support; only non-standard members gain a ``variant`` key.
    """
    document = member.model_dump(mode="json")
    if member.variant == "standard":
        document.pop("variant", None)
    return document


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
                address: _roster_member_document(member)
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
    difficulty: Difficulty | None = None
    last_k_change_height: int | None = None
    timestamps: list[int] = field(default_factory=list)
    """The last :data:`MAX_RECENT_TIMESTAMPS` block timestamps, oldest first."""
    minted: int = 0
    """Total native subsidy ever created, for the ``max_supply`` clamp."""
    minted_assets: dict[str, int] = field(default_factory=dict)
    """Total variant-token supply ever created, keyed by asset id."""
    pools: dict[str, Pool] = field(default_factory=dict)
    """``pool_id -> Pool`` for every DEX pair the chain has created."""

    def __post_init__(self) -> None:
        if self.difficulty is None:
            self.difficulty = initial_difficulty(self.params)
        if self.height < 0:
            raise StateError("state height must be non-negative")
        if self.minted < 0:
            raise StateError("minted supply must be non-negative")
        for asset, amount in self.minted_assets.items():
            if not isinstance(asset, str) or not asset:
                raise StateError("minted_assets keys must be asset ids")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise StateError("minted_assets amounts must be non-negative")
        for pool_id, pool in self.pools.items():
            if not isinstance(pool_id, str) or pool_id != pool.pool_id:
                raise StateError("pools must be keyed by their pool id")

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
        """Merkle root over account and DEX-pool leaves (PROTOCOL.md section 11).

        Account keys are bech32 addresses; pool keys are prefixed with ``pool:``
        so the two namespaces cannot collide. A chain with no pools produces the
        same all-account root as before pools existed.
        """
        leaves: dict[str, object] = {
            address: account.as_dict() for address, account in self.accounts.items()
        }
        for pool_id, pool in self.pools.items():
            leaves[f"pool:{pool_id}"] = pool.as_dict()
        return merkle_root_hex(leaves)

    @staticmethod
    def roster_root_of(roster: Mapping[str, RosterMember]) -> str:
        """Merkle root over the epoch's active roster.

        Only the consensus-relevant fields are committed: the registered public
        key and the miner's committed game quota.
        """
        leaves: dict[str, dict[str, object]] = {}
        for address, member in roster.items():
            leaf: dict[str, object] = {
                "public_key": member.public_key,
                "max_games": member.max_games,
            }
            if member.variant != "standard":
                leaf["variant"] = member.variant
            leaves[address] = leaf
        return merkle_root_hex(leaves)

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

    def credit_asset_coinbase(self, miner: str, asset: str, amount: int, height: int) -> None:
        """Credit a variant-token block reward that matures like native coinbase."""
        if amount < 0:
            raise StateError("asset coinbase amount must be non-negative")
        if amount == 0:
            return
        account = self.ensure(miner)
        account.immature_assets[asset] = account.immature_assets.get(asset, 0) + amount
        account.asset_coinbase_queue.append((height, amount, asset))
        self.minted_assets[asset] = self.minted_assets.get(asset, 0) + amount

    def release_matured(self, height: int) -> int:
        """Move matured coinbase value into spendable balances; return the native total."""
        maturity = self.params.coinbase_maturity
        total = 0
        for account in self.accounts.values():
            if account.coinbase_queue:
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
            if account.asset_coinbase_queue:
                asset_pending: list[tuple[int, int, str]] = []
                for credit_height, amount, asset in account.asset_coinbase_queue:
                    if credit_height + maturity <= height:
                        account.assets[asset] = account.assets.get(asset, 0) + amount
                        account.immature_assets[asset] = max(
                            0, account.immature_assets.get(asset, 0) - amount
                        )
                    else:
                        asset_pending.append((credit_height, amount, asset))
                account.asset_coinbase_queue = asset_pending
        return total

    def spendable(self, address: str) -> int:
        """The native balance a transaction may draw on."""
        return self.account(address).balance

    def asset_balance(self, address: str, asset: str) -> int:
        """The spendable balance of ``asset`` owned by ``address``."""
        return self.account(address).assets.get(asset, 0)

    def credit_asset(self, address: str, asset: str, amount: int) -> None:
        """Add ``amount`` of ``asset`` to a spendable balance."""
        if amount < 0:
            raise StateError("asset credit must be non-negative")
        account = self.ensure(address)
        account.assets[asset] = account.assets.get(asset, 0) + amount

    def debit_asset(self, address: str, asset: str, amount: int) -> None:
        """Subtract ``amount`` of ``asset``, refusing an overdraft."""
        if amount < 0:
            raise StateError("asset debit must be non-negative")
        account = self.ensure(address)
        current = account.assets.get(asset, 0)
        if current < amount:
            raise StateError(f"{address} has {current} of {asset}, cannot debit {amount}")
        remaining = current - amount
        if remaining:
            account.assets[asset] = remaining
        else:
            account.assets.pop(asset, None)

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
                    variant=account.variant,
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
                    **({"variant": record.variant} if record.variant != "standard" else {}),
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
            "difficulty": self.difficulty.as_dict() if self.difficulty else None,
            "last_k_change_height": self.last_k_change_height,
            "timestamps": list(self.timestamps),
            "minted": self.minted,
            **(
                {"minted_assets": dict(sorted(self.minted_assets.items()))}
                if self.minted_assets
                else {}
            ),
            **(
                {"pools": {pool_id: pool.as_dict() for pool_id, pool in sorted(self.pools.items())}}
                if self.pools
                else {}
            ),
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
        pools_raw = _as_mapping(document.get("pools", {}), "pools")

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
                variant=str(item.get("variant") or "standard"),
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
        pools = {
            str(pool_id): Pool.from_dict(str(pool_id), _as_mapping(entry, "pool"))
            for pool_id, entry in pools_raw.items()
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
            difficulty=difficulty,
            last_k_change_height=(
                None
                if document.get("last_k_change_height") is None
                else _as_int(document.get("last_k_change_height"), "last_k_change_height")
            ),
            timestamps=[_as_int(value, "timestamp") for value in timestamps_raw],
            minted=_as_int(document.get("minted"), "minted"),
            minted_assets=_asset_totals(document.get("minted_assets")),
            pools=pools,
        )

    def copy(self) -> State:
        """A deep, independent copy via the snapshot round-trip."""
        return State.from_snapshot(self.params, self.snapshot())


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    """Require a mapping field in a snapshot."""
    if not isinstance(value, Mapping):
        raise StateError(f"{name} must be a mapping in a state snapshot")
    return value


def _asset_totals(value: object) -> dict[str, int]:
    """Parse an optional ``asset id -> total`` map from a snapshot."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StateError("minted_assets must be a mapping in a state snapshot")
    totals: dict[str, int] = {}
    for asset, amount in value.items():
        if not isinstance(asset, str) or not asset:
            raise StateError("minted_assets keys must be asset ids")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise StateError("minted_assets amounts must be non-negative integers")
        totals[asset] = amount
    return totals


__all__ = [
    "MAX_RECENT_TIMESTAMPS",
    "ZERO_HASH",
    "Account",
    "EpochRecord",
    "MatchRecord",
    "MinerStats",
    "Pool",
    "ProofRecord",
    "State",
    "TimeoutClaimRecord",
    "WitnessRecord",
    "epoch_end_height",
    "epoch_of",
    "epoch_start_height",
    "first_matchable_epoch",
    "reveal_deadline_tip",
]
