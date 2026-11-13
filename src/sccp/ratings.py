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

Floating point is intentional: this is a read-only view, never part of
``state_root``, so two nodes that disagree in the last bit of a ``float`` disagree
about a label, not about a block. The arithmetic below is the paper's, with the
paper's own constants, and reproduces the paper's worked example in the tests.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

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

CONVERGENCE: Final = 1e-6
"""Tolerance for the Illinois root search on volatility."""

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


def _g(deviation: float) -> float:
    """``g(phi)``: how much a game against this deviation can move a rating."""
    return 1.0 / math.sqrt(1.0 + 3.0 * deviation * deviation / (math.pi * math.pi))


def _expected(mu: float, opponent_mu: float, g: float) -> float:
    """``E``: the expected score, ``1 / (1 + 10^(-g * (mu - mu_j) / 400))``.

    ``mu`` is already in the paper's internal scale, where ``400 / ln(10)``
    cancels, so the exponent is simply ``-g * (mu - mu_j)``.
    """
    return 1.0 / (1.0 + math.exp(-g * (mu - opponent_mu)))


def _solve_volatility(
    phi: float,
    sigma: float,
    v: float,
    delta: float,
    *,
    tau: float,
    convergence: float,
) -> float:
    """Step 5 of the paper: the new volatility from the Illinois algorithm."""

    def f(x: float) -> float:
        ex = math.exp(x)
        numerator = ex * (delta * delta - phi * phi - v - ex)
        denominator = 2.0 * (phi * phi + v + ex) ** 2
        return numerator / denominator - (x - a) / tau

    a = math.log(sigma * sigma)
    if delta * delta > phi * phi + v:
        b = math.log(delta * delta - phi * phi - v)
    else:
        k = 1.0
        while f(a - k * tau) < 0.0:
            k += 1.0
        b = a - k * tau

    f_a = f(a)
    f_b = f(b)
    while abs(b - a) > convergence:
        c = a + (a - b) * f_a / (f_b - f_a)
        f_c = f(c)
        if f_c * f_b <= 0.0:
            a, f_a = b, f_b
        else:
            f_a /= 2.0
        b, f_b = c, f_c
    return math.exp(a / 2.0)


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
    convergence: float = CONVERGENCE,
) -> Rating:
    """Apply one rating period to ``current``.

    ``games`` pairs each opponent's pre-period rating with the score against it
    (1.0 win, 0.5 draw, 0.0 loss). Averaging duplicate opponents is the caller's
    job; this is the paper's single-pass update. With no games the rating is
    unchanged and only its deviation grows.
    """
    if not games:
        return inflate(current, 1)
    if tau <= 0.0:
        raise ValueError("tau must be positive")

    mu = (current.rating - DEFAULT_RATING) / RATING_SCALE
    phi = current.deviation / RATING_SCALE
    information = 0.0
    improvement = 0.0
    for opponent, score in games:
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"a game score must be between 0 and 1, got {score}")
        opponent_mu = (opponent.rating - DEFAULT_RATING) / RATING_SCALE
        g = _g(opponent.deviation / RATING_SCALE)
        expected = _expected(mu, opponent_mu, g)
        information += g * g * expected * (1.0 - expected)
        improvement += g * (score - expected)
    if information <= 0.0:  # pragma: no cover - E is strictly inside (0, 1)
        raise ValueError("a rating period needs at least one informative game")

    v = 1.0 / information
    delta = v * improvement
    volatility = _solve_volatility(
        phi, current.volatility, v, delta, tau=tau, convergence=convergence
    )
    phi_star = math.sqrt(phi * phi + volatility * volatility)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * improvement
    return Rating(
        new_mu * RATING_SCALE + DEFAULT_RATING,
        new_phi * RATING_SCALE,
        volatility,
    )


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
    "CONVERGENCE",
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
