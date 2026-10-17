"""Backtest harness: Elo math, grading, statistics, no look-ahead, parameter choice."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from betdata.analysis.backtest import (
    BREAK_EVEN,
    binomial_tail,
    grade,
    mae,
    picks,
    profit,
    summarize,
    tune,
    verdict,
    walk_forward,
    wilson,
)
from betdata.analysis.dataset import GameRow
from betdata.analysis.models import EloModel, MarketModel, Prediction, version

T0 = datetime(2020, 9, 13, 17, 0, tzinfo=UTC)  # a Sunday, 1 pm Eastern


def game(
    gid, home, away, *, at=T0, season=2020, hs=None, as_=None, spread=None, neutral=False,
    home_rest=None, away_rest=None, home_price=None, away_price=None, season_type="REG",
):  # fmt: skip
    return GameRow(
        game_id=gid, season=season, season_type=season_type, week=1, start=at,
        home_team_id=home, away_team_id=away, home=f"T{home}", away=f"T{away}", neutral=neutral,
        home_score=hs, away_score=as_, spread_home=spread, spread_home_price=home_price,
        spread_away_price=away_price, total=None, over_price=None, under_price=None,
        home_rest_days=home_rest, away_rest_days=away_rest,
    )  # fmt: skip


# --- Elo ---------------------------------------------------------------------


def test_elo_even_teams_neutral_site_is_a_coin_flip():
    p = EloModel().predict(game(1, 1, 2, neutral=True))
    assert p.home_margin == 0 and p.home_win_prob == pytest.approx(0.5)


def test_elo_home_field_is_hfa_over_25_points():
    p = EloModel(hfa=50).predict(game(1, 1, 2))
    assert p.home_margin == pytest.approx(2.0)
    assert p.home_win_prob > 0.5


def test_elo_bye_bonus_only_after_13_days_rest():
    m = EloModel(hfa=0, bye=25)
    assert m.predict(game(1, 1, 2, home_rest=14)).home_margin == pytest.approx(1.0)
    assert m.predict(game(1, 1, 2, home_rest=7, away_rest=13)).home_margin == pytest.approx(-1.0)
    assert m.predict(game(1, 1, 2)).home_margin == 0  # first game of the season: no rest known


def test_elo_update_is_zero_sum_and_rewards_the_winner():
    m = EloModel()
    m.update(game(1, 1, 2, hs=24, as_=10))
    assert m.ratings[1] > 1505 > m.ratings[2]
    assert m.ratings[1] + m.ratings[2] == pytest.approx(2 * 1505)


def test_elo_bigger_wins_move_ratings_more():
    small, big = EloModel(), EloModel()
    small.update(game(1, 1, 2, hs=17, as_=14))
    big.update(game(1, 1, 2, hs=38, as_=3))
    assert big.ratings[1] - 1505 > small.ratings[1] - 1505 > 0


def test_elo_ignores_unfinished_games():
    m = EloModel()
    m.update(game(1, 1, 2))
    assert m.ratings == {}


def test_elo_reverts_toward_the_mean_between_seasons():
    m = EloModel(revert=0.5, hfa=0)
    m.ratings = {1: 1605.0, 2: 1405.0}
    m.season = 2020
    p = m.predict(game(1, 1, 2, season=2021))
    assert m.ratings == {1: 1555.0, 2: 1455.0}
    assert p.home_margin == pytest.approx(100 / 25)


def test_version_names_model_and_parameters():
    assert (
        version(EloModel(k=20, hfa=48, revert=0.33, bye=0)) == "elo:bye=0,hfa=48,k=20,revert=0.33"
    )
    assert version(MarketModel()) == "market"


# --- Grading and money ---------------------------------------------------------


@pytest.mark.parametrize(
    "spread, hs, as_, side, expected",
    [
        (-3.0, 24, 21, "home", (-3.0, "push")),
        (-3.0, 24, 20, "home", (-3.0, "win")),
        (-3.0, 24, 22, "home", (-3.0, "loss")),
        (-3.0, 24, 22, "away", (3.0, "win")),
        (2.5, 20, 21, "home", (2.5, "win")),  # home dog loses by 1, covers +2.5
        (2.5, 17, 21, "away", (-2.5, "win")),
    ],
)
def test_grade(spread, hs, as_, side, expected):
    assert grade(game(1, 1, 2, hs=hs, as_=as_, spread=spread), side) == expected


def test_profit_per_unit():
    assert profit("win", -110) == pytest.approx(100 / 110, abs=1e-6)
    assert profit("win", 120) == pytest.approx(1.2)
    assert profit("loss", -110) == -1
    assert profit("push", -110) == 0


def test_picks_bet_the_model_side_beyond_the_threshold():
    g = game(1, 1, 2, hs=30, as_=10, spread=-3.0, home_price=-105)
    assert picks([(g, Prediction(4.0))], 2.0) == []  # edge 1.0: no bet
    [p] = picks([(g, Prediction(6.0))], 2.0)  # model: home by 6, market: home by 3
    assert (p.side, p.edge, p.line, p.price, p.result) == ("home", 3.0, -3.0, -105, "win")
    [p] = picks([(g, Prediction(0.0))], 2.0)
    assert (p.side, p.line, p.price, p.result) == ("away", 3.0, -110, "loss")  # default price


def test_picks_skip_unfinished_unlined_and_filtered_games():
    lined = game(1, 1, 2, hs=30, as_=10, spread=-3.0)
    rows = [
        (game(2, 1, 2, spread=-3.0), Prediction(10)),  # not played
        (game(3, 1, 2, hs=30, as_=10), Prediction(10)),  # no line
        (lined, None),  # model had no view
        (replace(lined, game_id=4, season_type="POST"), Prediction(10)),
        (replace(lined, game_id=5, season=2019), Prediction(10)),
    ]
    assert [p.game.game_id for p in picks(rows, 1.0)] == [4, 5]
    assert [p.game.game_id for p in picks(rows, 1.0, regular_only=True)] == [5]
    assert [p.game.game_id for p in picks(rows, 1.0, seasons=[2020])] == [4]


def test_cheating_model_wins_every_decided_bet():
    """Harness check: a model told the final margin must never lose."""
    rng = random.Random(7)
    spreads = [-7.0, -3.0, 1.5, 6.5]
    games = [
        game(i, 1, 2, hs=rng.randint(0, 40), as_=rng.randint(0, 40), spread=rng.choice(spreads))
        for i in range(400)
    ]
    s = summarize(picks([(g, Prediction(g.home_margin)) for g in games], 0.5))
    assert s.losses == 0 and s.wins > 300


# --- Statistics --------------------------------------------------------------


def test_summary_rates_and_roi():
    g = game(1, 1, 2, hs=30, as_=10, spread=-3.0)
    win = picks([(g, Prediction(10))], 1)[0]
    loss = picks([(g, Prediction(-10))], 1)[0]
    s = summarize([win, win, loss])
    assert (s.bets, s.wins, s.losses, s.pushes) == (3, 2, 1, 0)
    assert s.win_rate == pytest.approx(2 / 3)
    assert s.roi == pytest.approx((2 * 100 / 110 - 1) / 3, abs=1e-6)


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)


def test_binomial_tail():
    assert binomial_tail(0, 10, 0.5) == 1.0
    assert binomial_tail(10, 10, 0.5) == pytest.approx(1 / 1024)
    assert binomial_tail(6, 10, 0.5) == pytest.approx(386 / 1024)
    assert binomial_tail(550, 1000, BREAK_EVEN) == pytest.approx(0.05, abs=0.01)  # ~1.6 sd


def test_verdict_thresholds():
    from betdata.analysis.backtest import Summary

    assert verdict(Summary(100, 70, 30, 0, 0)).startswith("INCONCLUSIVE")  # too few bets
    assert verdict(Summary(400, 220, 180, 0, 0)).startswith("EDGE")  # 55%
    assert verdict(Summary(400, 205, 195, 0, 0)).startswith("NO EDGE")  # 51.3%
    assert verdict(Summary(400, 213, 187, 0, 0)).startswith("INCONCLUSIVE")  # 53.3%


# --- No look-ahead -------------------------------------------------------------


def _season(seed=1, seasons=(2018, 2019, 2020), teams=8):
    rng = random.Random(seed)
    out, gid = [], 0
    for season in seasons:
        start = datetime(season, 9, 10, 17, tzinfo=UTC)
        for week in range(10):
            order = rng.sample(range(1, teams + 1), teams)
            for i in range(0, teams, 2):
                gid += 1
                at = start + timedelta(weeks=week, hours=3 * (i // 4))
                hs, as_ = rng.randint(3, 38), rng.randint(3, 38)
                spread = rng.choice([-7.5, -6.5, -3.0, -2.5, 1.0, 3.0, 4.5])
                out.append(game(gid, order[i], order[i + 1], at=at, season=season, hs=hs, as_=as_,
                                spread=spread))  # fmt: skip
    return out


def test_changing_a_later_result_never_changes_an_earlier_prediction():
    games = _season()
    base = walk_forward(games, EloModel())
    cut = games[len(games) // 2].start
    altered = [replace(g, home_score=0, away_score=50) if g.start >= cut else g for g in games]
    for (g, p), (_, q) in zip(base, walk_forward(altered, EloModel()), strict=True):
        if g.start <= cut:
            assert p == q, g


def test_games_on_the_same_day_do_not_see_each_others_results():
    early = game(1, 1, 2, at=T0, hs=50, as_=0)
    late = game(2, 1, 3, at=T0 + timedelta(hours=3), hs=10, as_=10)  # same Eastern day
    next_day = game(3, 1, 4, at=T0 + timedelta(days=1), hs=10, as_=10)
    preds = dict((g.game_id, p) for g, p in walk_forward([late, next_day, early], EloModel(hfa=0)))
    assert preds[2].home_margin == 0  # team 1's blowout earlier that day is not known yet
    assert preds[3].home_margin > 0


def test_market_model_never_bets_and_matches_market_error():
    games = _season()
    preds = walk_forward(games, MarketModel())
    assert picks(preds, 0.5) == []
    model_err, market_err, n = mae(preds, [2019])
    assert n and model_err == market_err


def test_tune_reads_only_training_seasons():
    games = _season()
    grid = {"k": (10.0, 30.0), "hfa": (0.0, 50.0), "revert": (0.33,), "bye": (0.0,)}
    a = tune(games, [2019], grid=grid, thresholds=(1.0, 3.0))
    scrambled = [replace(g, home_score=g.away_score, away_score=g.home_score) if g.season == 2020
                 else g for g in games]  # fmt: skip
    b = tune(scrambled, [2019], grid=grid, thresholds=(1.0, 3.0))
    assert (a.params, a.threshold, a.train_mae) == (b.params, b.threshold, b.train_mae)
    assert set(a.params) == {"k", "hfa", "revert", "bye"}
