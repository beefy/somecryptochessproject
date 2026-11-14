"""Derived Glicko-2 ratings (PROTOCOL.md section 14.4).

Two layers are covered here: the one-period update — delegated to the pinned
`glicko2` package and checked against the paper's worked example — and the ledger
that replays settled proofs off a real chain. The ledger tests use the same
fixtures as the block tests, so a proof that verifies also rates.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from sccp.chain.mempool import Mempool
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import AddressError
from sccp.node.service import NodeService
from sccp.ratings import (
    DEFAULT_DEVIATION,
    DEFAULT_RATING,
    RATING_SCALE,
    Rating,
    SettledGame,
    build_ledger,
    inflate,
    rating_buckets,
    rating_mean,
    rating_median,
    settled_games,
    update_rating,
)
from sccp.wallet.service import WalletService


def _service(chain: Any) -> NodeService:
    """A node over an existing chain, without storage or a listening socket."""
    return NodeService(
        params=chain.params,
        chain=chain,
        mempool=Mempool(params=chain.params, tip_state=chain.state, book=chain.book),
    )


def _unrated_address(chain: Any) -> str:
    """A valid address for this network that has never settled a game."""
    keypair = hybrid_keypair_from_seed(bytes([9]) * 32)
    return encode_address(keypair.public_key_bytes, chain.params)


def test_glicko2_reproduces_the_papers_worked_example() -> None:
    """Glickman's example: three games from 1500/200/0.06 land near 1464/151.5."""
    updated = update_rating(
        Rating(1500.0, 200.0, 0.06),
        [
            (Rating(1400.0, 30.0), 1.0),
            (Rating(1550.0, 100.0), 0.0),
            (Rating(1700.0, 300.0), 0.0),
        ],
    )
    # The paper prints rounded intermediates (1464.06); full precision is 1464.05.
    assert updated.rating == pytest.approx(1464.06, abs=0.02)
    assert updated.deviation == pytest.approx(151.52, abs=0.02)
    assert updated.volatility == pytest.approx(0.05999, abs=1e-5)


def test_a_heavy_upset_stays_on_the_scale() -> None:
    """A lone loss to a far weaker opponent must not blow the update up.

    Glicko-2's volatility search gets flat when the surprise is extreme (a 2200
    player losing to an 840 one). A hand-rolled bracket once returned a
    volatility near 1139 and a rating near -195676 for this input; the pinned
    reference package keeps it bounded.
    """
    updated = update_rating(
        Rating(2197.2591089542266, 339.59450792396785, 0.048287426485310675),
        [(Rating(836.1669930, 168.1135337), 0.0)],
    )
    assert 1000.0 < updated.rating < 2197.2591089542266
    assert 0.0 < updated.deviation < 700.0
    assert 0.0 < updated.volatility < 0.2


def test_tau_reaches_the_reference_update() -> None:
    """The system constant is passed through, not silently left at the default."""
    games = [(Rating(1600.0, 60.0), 0.0)]
    calm = update_rating(Rating(2100.0, 60.0, 0.05), games, tau=0.3)
    jumpy = update_rating(Rating(2100.0, 60.0, 0.05), games, tau=1.2)
    assert jumpy.volatility > calm.volatility


def test_a_win_lifts_the_winner_and_lowers_the_loser() -> None:
    winner = update_rating(Rating(), [(Rating(), 1.0)])
    loser = update_rating(Rating(), [(Rating(), 0.0)])
    assert winner.rating > DEFAULT_RATING > loser.rating
    assert winner.deviation < DEFAULT_DEVIATION
    assert loser.deviation < DEFAULT_DEVIATION


def test_equal_ratings_gain_and_lose_the_same_amount() -> None:
    equal = Rating(1600.0, 60.0, 0.06)
    winner = update_rating(equal, [(equal, 1.0)])
    loser = update_rating(equal, [(equal, 0.0)])
    assert winner.rating - 1600.0 == pytest.approx(1600.0 - loser.rating)


def test_a_draw_moves_a_stronger_player_down() -> None:
    stronger = Rating(1700.0, 60.0, 0.06)
    drawn = update_rating(stronger, [(Rating(1400.0, 60.0, 0.06), 0.5)])
    assert drawn.rating < stronger.rating


def test_inactivity_inflates_deviation_without_moving_the_rating() -> None:
    rating = Rating(1630.0, 80.0, 0.06)
    after = inflate(rating, 3)
    phi = 80.0 / RATING_SCALE
    expected = math.sqrt(phi * phi + 3 * 0.06 * 0.06) * RATING_SCALE
    assert after.rating == rating.rating
    assert after.deviation == pytest.approx(expected)
    assert inflate(rating, 0) is rating
    with pytest.raises(ValueError, match="negative"):
        inflate(rating, -1)


def test_an_empty_period_is_one_idle_period() -> None:
    rating = Rating(1630.0, 80.0, 0.06)
    assert update_rating(rating, []) == inflate(rating, 1)


def test_impossible_scores_and_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        update_rating(Rating(), [(Rating(), 1.5)])
    with pytest.raises(ValueError, match="tau"):
        update_rating(Rating(), [(Rating(), 1.0)], tau=0.0)
    with pytest.raises(ValueError, match="deviation"):
        Rating(1500.0, 0.0)


def test_buckets_are_fixed_width_and_account_for_every_value() -> None:
    buckets = rating_buckets([999.0, 1000.0, 1099.9, 1100.0, 1500.0, 2000.0, 2500.0])
    assert [bucket.label for bucket in buckets[:3]] == ["<1000", "1000-1099", "1100-1199"]
    assert buckets[0].count == 1
    assert buckets[1].count == 2
    assert buckets[-1].count == 2
    assert sum(bucket.count for bucket in buckets) == 7
    assert buckets[0].low is None and buckets[0].high == 1000
    assert buckets[-1].low == 2000 and buckets[-1].high is None


def test_mean_and_median_handle_empty_lists_and_even_counts() -> None:
    assert rating_mean([]) == 0.0
    assert rating_median([]) == 0.0
    assert rating_mean([1500.0, 1600.0]) == pytest.approx(1550.0)
    assert rating_median([1700.0, 1500.0, 1600.0, 1400.0]) == pytest.approx(1550.0)


def test_scores_split_only_a_drawn_game() -> None:
    decided = SettledGame(
        game_hash="ab" * 32, epoch=3, height=4, winner="white", loser="black", result="1-0"
    )
    drawn = SettledGame(
        game_hash="cd" * 32,
        epoch=3,
        height=5,
        winner="white",
        loser="black",
        result="1/2-1/2",
    )
    assert decided.scores == (("white", 1.0), ("black", 0.0))
    assert drawn.scores == (("white", 0.5), ("black", 0.5))


def test_an_empty_chain_has_no_rated_miners(devnet_chain: Any) -> None:
    ledger = build_ledger(devnet_chain)
    assert ledger.miners == ()
    assert ledger.games == 0
    assert ledger.rated_epochs == 0


def test_a_settled_proof_rates_both_seats(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    block = mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))

    games = settled_games(chain)
    assert [(game.height, game.winner) for game in games] == [
        (block.header.height, match.proof.winner)
    ]

    ledger = build_ledger(chain)
    assert ledger.games == 1
    assert ledger.rated_epochs == 1
    assert ledger.tip_epoch == block.header.epoch
    winner = ledger.find(match.proof.winner)
    loser = ledger.find(match.proof.loser)
    assert winner is not None and loser is not None
    assert (winner.rank, loser.rank) == (1, 2)
    assert winner.rating > DEFAULT_RATING > loser.rating
    assert (winner.wins, winner.draws, winner.losses) == (1, 0, 0)
    assert (loser.wins, loser.draws, loser.losses) == (0, 0, 1)
    assert winner.provisional and loser.provisional
    assert winner.last_height == block.header.height
    assert len(winner.history) == 1
    assert winner.history[0].epoch == block.header.epoch
    assert winner.history[0].games == 1
    assert winner.rating == pytest.approx(winner.history[-1].rating)


def test_an_idle_miner_keeps_its_rating_and_widens_its_deviation(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    """Devnet epochs are one block long, so a heartbeat is a new period."""
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    before = build_ledger(chain).find(match.proof.winner)
    assert before is not None

    transfer = WalletService(chain.params, chain.state).build_transfer(
        live_chain.faucet, live_chain.accounts["alice"].address, 1_000
    )
    block = mine_block(chain, miner_keys[match.proof.winner], txs=(transfer,))
    after = build_ledger(chain).find(match.proof.winner)
    assert after is not None
    assert after.rating == pytest.approx(before.rating)
    assert after.deviation > before.deviation
    assert after.games == before.games
    assert after.history[-1].epoch == block.header.epoch
    assert after.history[-1].rating == pytest.approx(after.rating)


def test_ratings_are_a_pure_function_of_the_chain(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    assert build_ledger(chain) == build_ledger(chain)


def test_the_service_serves_the_ledger_and_the_prior(
    live_chain: Any, chain_match: Any, mine_block: Any, miner_keys: dict[str, Any]
) -> None:
    chain = live_chain.chain
    match = chain_match(chain, epoch=3)
    mine_block(chain, miner_keys[match.proof.winner], proofs=(match.proof,))
    service = _service(chain)
    try:
        view = service.ratings()
        assert view.network == chain.params.network_id
        assert view.algorithm == "glicko-2"
        assert view.rating_period == "epoch"
        assert view.games == 1
        assert [miner.rank for miner in view.miners] == [1, 2]
        assert view.distribution.rated == 2
        assert sum(bucket.count for bucket in view.distribution.buckets) == 2

        detail = service.miner_rating(match.proof.winner)
        assert detail.rank == 1
        assert detail.games == 1
        assert len(detail.history) == 1

        prior = service.miner_rating(_unrated_address(chain))
        assert prior.games == 0
        assert prior.rating == DEFAULT_RATING
        assert prior.rank is None
        assert prior.history == ()

        # The cache is keyed by tip: an unchanged chain returns the same numbers.
        assert service.ratings() == view
        assert service.ratings(limit=1).miners == view.miners[:1]
    finally:
        service.close()


def test_an_invalid_address_is_refused(
    live_chain: Any,
) -> None:
    service = _service(live_chain.chain)
    try:
        with pytest.raises(AddressError, match="not a devnet address"):
            service.miner_rating("sccpt1nope")
    finally:
        service.close()
