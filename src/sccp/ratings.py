"""Derived miner ratings: Glicko-2 over settled games (PROTOCOL.md section 14.4).

A rating is **not** consensus state and **not** a miner-submitted number. It is a
pure function of the games the chain has already accepted: every proof names two
seats and a result, and replaying the main chain in order produces the same
series on every node. Nothing in a block, a transaction or the API lets a miner
write its own rating, so the only way to move it is to win verified games.

The algorithm is Mark Glickman's Glicko-2 with one rating period per epoch:

* the prior is 1500 with a deviation of 350 and volatility 0.06, a deliberately
  wide starting range;
* a period's games are evaluated against opponents' *pre-period* ratings, so the
  update is simultaneous and order-independent apart from stable tie-breaks;
* two games against the same opponent in one period are averaged into a single
  score, as the paper prescribes;
* an inactive miner's deviation grows (``phi^2 += sigma^2`` per period) while the
  rating itself does not move.

The single-period arithmetic is **not** reimplemented here: ``update_rating``
calls the ``glicko2`` package (the pinned reference implementation declared in
``pyproject.toml``), which reproduces the paper's worked example in the tests.
The replay, the epoch boundary, the prior, the duplicate-opponent averaging and
the validation are ours. Ratings are a view, so a library upgrade can move a
number on the explorer, never a block.

Floating point is intentional: this is a read-only view, never part of
``state_root``, so two nodes that disagree in the last bit of a ``float`` disagree
about a label, not about a block.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from glicko2 import Player  # type: ignore[import-untyped]

if TYPE_CHECKING:  # pragma: no cover - import-cycle hygiene, not runtime behaviour
    from sccp.chain.chain import Chain

RATING_SCALE: Final = 173.7178
"""``400 / ln(10)``: the Glicko-2 internal scale, rounded exactly as the paper does."""

DEFAULT_RATING: Final = 1500.0
"""A miner with no settled game yet starts at the centre of the scale."""

DEFAULT_DEVIATION: Final = 350.0
"""Starting rating deviation: wide enough to be honest about knowing nothing."""

DEFAULT_VOLATILITY: Final = 0.06
"""Starting volatility, the paper's value for an average player."""

TAU: Final = 0.5
"""System constant: how much volatility may move in one period (0.3-1.2 is sane)."""

PROVISIONAL_GAMES: Final = 10
"""Below this many settled games a rating is flagged provisional."""

DRAW_RESULT: Final = "1/2-1/2"

DISTRIBUTION_MIN: Final = 1000
DISTRIBUTION_MAX: Final = 2000
DISTRIBUTION_STEP: Final = 100


@dataclass(frozen=True, slots=True)
class Rating:
    """One Glicko-2 rating: the estimate, its deviation and its volatility."""

    rating: float = DEFAULT_RATING
    deviation: float = DEFAULT_DEVIATION
    volatility: float = DEFAULT_VOLATILITY

    def __post_init__(self) -> None:
        for name in ("rating", "deviation", "volatility"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"rating {name} must be finite")
        if self.deviation <= 0.0:
            raise ValueError("rating deviation must be positive")
        if self.volatility <= 0.0:
            raise ValueError("rating volatility must be positive")


def inflate(rating: Rating, periods: int) -> Rating:
    """Grow the deviation over ``periods`` of inactivity (step 1, repeated)."""
    if periods < 0:
        raise ValueError("periods must not be negative")
    if periods == 0:
        return rating
    phi = rating.deviation / RATING_SCALE
    squared = phi * phi + periods * rating.volatility * rating.volatility
    return Rating(rating.rating, math.sqrt(squared) * RATING_SCALE, rating.volatility)


def update_rating(
    current: Rating,
    games: Sequence[tuple[Rating, float]],
    *,
    tau: float = TAU,
) -> Rating:
    """Apply one rating period to ``current``.

    ``games`` pairs each opponent's pre-period rating with the score against it
    (1.0 win, 0.5 draw, 0.0 loss). Averaging duplicate opponents is the caller's
    job; this is the paper's single-pass update, delegated to the pinned
    ``glicko2`` package. With no games the rating is unchanged and only its
    deviation grows (``inflate``), which is the rule the package calls
    ``did_not_compete``.

    One quirk comes with the package: its volatility objective squares the
    pre-period rating (``mu^2``) where the paper squares the pre-period deviation
    (``phi^2``). It still reproduces the paper's worked example, but results for
    ratings far from 1500 differ slightly from a literal transcription. The
    package is the reference this node reproduces; it is a display value either
    way.
    """
    if not games:
        return inflate(current, 1)
    if tau <= 0.0:
        raise ValueError("tau must be positive")
    for _, score in games:
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"a game score must be between 0 and 1, got {score}")

    player = Player(current.rating, current.deviation, current.volatility)
    # The package keeps tau as a class attribute; there is no constructor argument.
    player._tau = tau
    player.update_player(
        [opponent.rating for opponent, _ in games],
        [opponent.deviation for opponent, _ in games],
        [score for _, score in games],
    )
    return Rating(float(player.rating), float(player.rd), float(player.vol))


@dataclass(frozen=True, slots=True)
class SettledGame:
    """One verified game, as the chain settled it."""

    game_hash: str
    epoch: int
    """The epoch of the block that settled the proof (the rating period)."""
    height: int
    winner: str
    loser: str
    result: str

    @property
    def scores(self) -> tuple[tuple[str, float], tuple[str, float]]:
        """``(address, score)`` for both seats; a draw splits the point."""
        if self.result == DRAW_RESULT:
            return ((self.winner, 0.5), (self.loser, 0.5))
        return ((self.winner, 1.0), (self.loser, 0.0))


def settled_games(chain: Chain) -> list[SettledGame]:
    """Every proof on the adopted branch, oldest block first."""
    games: list[SettledGame] = []
    for stored in chain.main_chain():
        epoch = stored.header.epoch
        for proof in stored.block.proofs:
            games.append(
                SettledGame(
                    game_hash=proof.game_hash,
                    epoch=epoch,
                    height=stored.height,
                    winner=proof.winner,
                    loser=proof.loser,
                    result=proof.result,
                )
            )
    return games


@dataclass(frozen=True, slots=True)
class RatingPoint:
    """A miner's rating after one period, plus its cumulative record."""

    epoch: int
    height: int
    rating: float
    deviation: float
    volatility: float
    games: int
    wins: int
    draws: int
    losses: int


@dataclass(frozen=True, slots=True)
class MinerRating:
    """A miner's current rating and its whole history."""

    address: str
    rating: float
    deviation: float
    volatility: float
    games: int
    wins: int
    draws: int
    losses: int
    first_epoch: int
    last_epoch: int
    last_height: int
    history: tuple[RatingPoint, ...]
    rank: int

    @property
    def provisional(self) -> bool:
        """Whether the record is too short for the rating to mean much yet."""
        return self.games < PROVISIONAL_GAMES


@dataclass(frozen=True, slots=True)
class RatingLedger:
    """Every rated miner at one tip, sorted strongest first."""

    miners: tuple[MinerRating, ...]
    games: int
    rated_epochs: int
    tip_epoch: int
    tip_height: int

    def find(self, address: str) -> MinerRating | None:
        """The row for ``address``, if it has ever settled a game."""
        for miner in self.miners:
            if miner.address == address:
                return miner
        return None

    @property
    def ratings(self) -> tuple[float, ...]:
        """Current ratings in rank order, for the histogram and summary."""
        return tuple(miner.rating for miner in self.miners)


@dataclass(slots=True)
class _MinerState:
    """Mutable bookkeeping while the ledger is replayed."""

    rating: Rating = field(default_factory=Rating)
    games: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    first_epoch: int = 0
    last_epoch: int = 0
    last_height: int = 0
    applied_epoch: int = 0
    """The last period this rating already accounts for."""
    history: list[RatingPoint] = field(default_factory=list)

    @classmethod
    def start(cls, epoch: int) -> _MinerState:
        """A fresh prior entering its first period at ``epoch``."""
        return cls(first_epoch=epoch, last_epoch=epoch, applied_epoch=epoch - 1)

    def record(self, score: float, height: int) -> None:
        """Count one result and remember where it settled."""
        self.games += 1
        if score == 1.0:
            self.wins += 1
        elif score == 0.5:
            self.draws += 1
        else:
            self.losses += 1
        self.last_height = max(self.last_height, height)

    def point(self, epoch: int, height: int) -> RatingPoint:
        """Snapshot the current values as one history point."""
        return RatingPoint(
            epoch=epoch,
            height=height,
            rating=self.rating.rating,
            deviation=self.rating.deviation,
            volatility=self.rating.volatility,
            games=self.games,
            wins=self.wins,
            draws=self.draws,
            losses=self.losses,
        )


def build_ledger(chain: Chain) -> RatingLedger:
    """Replay every settled game on ``chain`` and rate its miners.

    The sweep visits only epochs that contain games, so its cost is linear in the
    number of proofs. Skipped periods are folded into the next update with
    :func:`inflate`, which is exactly the paper's per-period ``phi^2 += sigma^2``
    applied ``n`` times in closed form.
    """
    games = settled_games(chain)
    tip_epoch = chain.state.current_epoch()
    tip_height = chain.height
    if not games:
        return RatingLedger(
            miners=(),
            games=0,
            rated_epochs=0,
            tip_epoch=tip_epoch,
            tip_height=tip_height,
        )

    by_epoch: dict[int, list[SettledGame]] = {}
    for game in games:
        if game.winner == game.loser:
            raise ValueError(f"game {game.game_hash!r} has one seat on both sides")
        by_epoch.setdefault(game.epoch, []).append(game)

    states: dict[str, _MinerState] = {}
    for epoch in sorted(by_epoch):
        opponents: dict[str, dict[str, list[float]]] = {}
        for game in by_epoch[epoch]:
            for address, score in game.scores:
                opponent = game.loser if address == game.winner else game.winner
                state = states.setdefault(address, _MinerState.start(epoch))
                state.record(score, game.height)
                opponents.setdefault(address, {}).setdefault(opponent, []).append(score)

        # Every update in a period sees the same pre-period ratings.
        before = {
            address: inflate(states[address].rating, epoch - states[address].applied_epoch - 1)
            for address in opponents
        }
        for address, by_opponent in opponents.items():
            state = states[address]
            period_games = [
                (before[opponent], sum(scores) / len(scores))
                for opponent, scores in sorted(by_opponent.items())
            ]
            state.rating = update_rating(before[address], period_games)
            state.applied_epoch = epoch
            state.last_epoch = epoch
            state.history.append(state.point(epoch, state.last_height))

    miners: list[MinerRating] = []
    for address in sorted(states):
        state = states[address]
        current = inflate(state.rating, tip_epoch - state.applied_epoch)
        history = list(state.history)
        if tip_epoch > state.applied_epoch:
            history.append(
                RatingPoint(
                    epoch=tip_epoch,
                    height=tip_height,
                    rating=current.rating,
                    deviation=current.deviation,
                    volatility=current.volatility,
                    games=state.games,
                    wins=state.wins,
                    draws=state.draws,
                    losses=state.losses,
                )
            )
        miners.append(
            MinerRating(
                address=address,
                rating=current.rating,
                deviation=current.deviation,
                volatility=current.volatility,
                games=state.games,
                wins=state.wins,
                draws=state.draws,
                losses=state.losses,
                first_epoch=state.first_epoch,
                last_epoch=state.last_epoch,
                last_height=state.last_height,
                history=tuple(history),
                rank=0,
            )
        )

    miners.sort(key=lambda miner: (-miner.rating, miner.address))
    ranked = tuple(
        MinerRating(
            address=miner.address,
            rating=miner.rating,
            deviation=miner.deviation,
            volatility=miner.volatility,
            games=miner.games,
            wins=miner.wins,
            draws=miner.draws,
            losses=miner.losses,
            first_epoch=miner.first_epoch,
            last_epoch=miner.last_epoch,
            last_height=miner.last_height,
            history=miner.history,
            rank=index,
        )
        for index, miner in enumerate(miners, start=1)
    )
    return RatingLedger(
        miners=ranked,
        games=len(games),
        rated_epochs=len(by_epoch),
        tip_epoch=tip_epoch,
        tip_height=tip_height,
    )


@dataclass(frozen=True, slots=True)
class RatingBucket:
    """One bar of the rating histogram."""

    label: str
    low: int | None
    high: int | None
    count: int


def rating_buckets(values: Iterable[float]) -> tuple[RatingBucket, ...]:
    """A fixed-width histogram, so two nodes draw the same axis.

    Buckets are ``<1000``, then 100-wide bands from 1000 to 1999, then ``>=2000``.
    Fixed bounds (rather than data-driven bins) keep the shape comparable between
    polls and between networks.
    """
    labels = [f"<{DISTRIBUTION_MIN}"]
    counts = [0]
    for start in range(DISTRIBUTION_MIN, DISTRIBUTION_MAX, DISTRIBUTION_STEP):
        labels.append(f"{start}-{start + DISTRIBUTION_STEP - 1}")
        counts.append(0)
    labels.append(f">={DISTRIBUTION_MAX}")
    counts.append(0)

    for value in values:
        if value < DISTRIBUTION_MIN:
            counts[0] += 1
        elif value >= DISTRIBUTION_MAX:
            counts[-1] += 1
        else:
            counts[1 + int((value - DISTRIBUTION_MIN) // DISTRIBUTION_STEP)] += 1

    buckets: list[RatingBucket] = []
    for index, (label, count) in enumerate(zip(labels, counts, strict=True)):
        if index == 0:
            low, high = None, DISTRIBUTION_MIN
        elif index == len(labels) - 1:
            low, high = DISTRIBUTION_MAX, None
        else:
            low = DISTRIBUTION_MIN + (index - 1) * DISTRIBUTION_STEP
            high = low + DISTRIBUTION_STEP
        buckets.append(RatingBucket(label=label, low=low, high=high, count=count))
    return tuple(buckets)


def rating_mean(values: Sequence[float]) -> float:
    """The arithmetic mean, or 0.0 when nobody is rated."""
    if not values:
        return 0.0
    return math.fsum(values) / len(values)


def rating_median(values: Sequence[float]) -> float:
    """The middle rating, or 0.0 when nobody is rated."""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


__all__ = [
    "DEFAULT_DEVIATION",
    "DEFAULT_RATING",
    "DEFAULT_VOLATILITY",
    "DISTRIBUTION_MAX",
    "DISTRIBUTION_MIN",
    "DISTRIBUTION_STEP",
    "PROVISIONAL_GAMES",
    "RATING_SCALE",
    "TAU",
    "MinerRating",
    "Rating",
    "RatingBucket",
    "RatingLedger",
    "RatingPoint",
    "SettledGame",
    "build_ledger",
    "inflate",
    "rating_buckets",
    "rating_mean",
    "rating_median",
    "settled_games",
    "update_rating",
]
