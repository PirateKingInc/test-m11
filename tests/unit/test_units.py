"""Unit model: missing-starter logic, unit ratings, regression, totals grading, no look-ahead."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from betdata.analysis.backtest import choose_threshold, grade_total, total_picks
from betdata.analysis.context import (
    Context,
    Starter,
    StarterTracker,
    absences,
    missing_by_group,
    norm_name,
)
from betdata.analysis.models import Prediction
from betdata.analysis.units import (
    UnitRatings,
    build_features,
    estimate_out_prob,
    ridge,
)
from tests.unit.test_backtest import game


def test_norm_name():
    assert norm_name("Michael Pittman Jr.") == "michael pittman"
    assert norm_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert norm_name("D.J. Moore") == norm_name("DJ Moore")


# --- Starters and absences -----------------------------------------------------------------


def snaps(*players):
    """(key, group, share) -> snap rows; offense groups use the offense share."""
    out = []
    for key, group, share in players:
        off = group in ("QB", "OL", "WR", "TE", "RB")
        out.append((key, key.upper(), group, share if off else 0.0, 0.0 if off else share))
    return out


def tracker_with(games_snaps, season=2024, team=1):
    ctx = Context()
    for gid, rows in enumerate(games_snaps, start=1):
        ctx.snaps[gid] = {team: rows}
    t = StarterTracker(ctx)
    for gid in range(1, len(games_snaps) + 1):
        t.record(gid, season, team)
    return ctx, t


def test_starters_need_half_the_snaps_in_games_played():
    ctx, t = tracker_with([
        snaps(("qb", "QB", 1.0), ("cb1", "CB", 0.95), ("cb3", "CB", 0.3)),
        snaps(("qb", "QB", 1.0), ("cb1", "CB", 0.9), ("cb3", "CB", 0.4)),
        snaps(("qb", "QB", 1.0), ("cb3", "CB", 0.6)),  # cb1 missed this one
    ])  # fmt: skip
    starters = {s.key: s for s in t.starters(2024, 1)}
    assert set(starters) == {"qb", "cb1"}  # cb3 averages 0.43: a rotation player
    assert starters["cb1"].share == pytest.approx(0.925)  # average over games he played


def test_starters_fall_back_to_last_season_then_none():
    ctx, t = tracker_with([snaps(("qb", "QB", 1.0))] * 3, season=2023)
    assert [s.key for s in t.starters(2024, 1)] == ["qb"]
    assert t.starters(2024, 2) == []


def test_absences_from_reports_and_rosters():
    ctx = Context()
    keys = [("qb", "QB"), ("cb", "CB"), ("s", "S"), ("lt", "OL"), ("wr", "WR"), ("rb", "RB")]
    s = {k: Starter(k, k.upper(), g, 1.0) for k, g in keys}
    ctx.reports[(2024, 5, 1)] = {"qb": "Questionable", "cb": "Out", "s": "Doubtful"}
    # Week 4's roster (week 3 was a bye) is what is known at week 5's final injury report.
    ctx.rosters[(2024, 4, 1)] = {"qb": "ACT", "cb": "ACT", "s": "ACT", "lt": "RES", "rb": "INA"}
    ctx.rostered[(2024, 1)] = {"qb": 1, "cb": 1, "s": 1, "lt": 1, "wr": 2, "rb": 1}
    out = {a.starter.key: a for a in absences(ctx, list(s.values()), 2024, 5, 1)}
    assert out["qb"].out_prob == 0.25 and out["qb"].reason == "Questionable"
    assert out["cb"].out_prob == 1.0 and out["s"].out_prob == 0.9
    assert out["lt"].reason == "IR/reserve"
    assert out["wr"].reason == "not on roster"  # was on the team in week 2, gone by week 4
    assert "rb" not in out  # inactive for last week's game: not an absence this week
    groups = missing_by_group(list(out.values()))
    assert groups["CB"] == 1.0 and groups["S"] == 0.9 and groups["QB"] == 0.25


def test_game_week_roster_is_not_known_at_the_final_report():
    """REVIEW.md #1: the week's own roster is the game-day snapshot (Saturday IR moves,
    inactives), so it must not count at the final-report point."""
    ctx = Context()
    lt = Starter("lt", "LT", "OL", 1.0)
    ctx.rostered[(2024, 1)] = {"lt": 1}
    ctx.rosters[(2024, 4, 1)] = {"lt": "ACT"}
    ctx.rosters[(2024, 5, 1)] = {"lt": "RES"}  # placed on IR the day before the game
    assert absences(ctx, [lt], 2024, 5, 1) == []
    assert [a.reason for a in absences(ctx, [lt], 2024, 6, 1)] == ["IR/reserve"]


def test_injury_report_overrides_an_earlier_reserve_status():
    ctx = Context()
    wr = Starter("wr", "WR", "WR", 1.0)
    ctx.rostered[(2024, 1)] = {"wr": 1}
    ctx.rosters[(2024, 4, 1)] = {"wr": "RES"}
    ctx.reports[(2024, 5, 1)] = {"wr": "Questionable"}  # designated to return
    [a] = absences(ctx, [wr], 2024, 5, 1)
    assert a.reason == "Questionable"


def test_injury_known_at():
    from betdata.analysis.context import injury_known_at
    from betdata.nfl.points import final_report_time

    kickoff = datetime(2024, 10, 6, 17, 0, tzinfo=UTC)  # Sunday 1 pm ET
    live = datetime(2024, 10, 3, 20, 0, tzinfo=UTC)
    published = datetime(2024, 10, 4, 18, 0, tzinfo=UTC)
    backfill = datetime(2026, 10, 1, tzinfo=UTC)
    assert injury_known_at(live, published, kickoff) == live  # fetched before the game
    assert injury_known_at(backfill, published, kickoff) == published
    assert injury_known_at(backfill, None, kickoff) == final_report_time(kickoff)


def test_offseason_departures_are_not_missing_starters():
    ctx = Context()
    ctx.rosters[(2024, 1, 1)] = {"new_qb": "ACT"}
    ctx.rostered[(2024, 1)] = {"new_qb": 1}
    old = Starter("old_qb", "OLD", "QB", 1.0)  # last season's starter, gone in the offseason
    assert absences(ctx, [old], 2024, 1, 1) == []


def test_without_a_roster_only_the_injury_report_counts():
    ctx = Context()
    ctx.reports[(2024, 3, 1)] = {"wr": "Out"}
    starters = [Starter("wr", "WR", "WR", 0.8), Starter("te", "TE", "TE", 0.9)]
    [a] = absences(ctx, starters, 2024, 3, 1)
    assert a.starter.key == "wr"
    assert missing_by_group([a])["WR"] == pytest.approx(0.8)


# --- Ratings and regression ------------------------------------------------------------------


def test_unit_ratings_move_offense_and_defense_together():
    r = UnitRatings(alpha=0.1)
    r.update(1, 2, (36, 36 * 0.5, 26, 26 * -0.05))  # big passing day, average running day
    assert r.po[1] > 0 and r.pd[2] > 0  # offense rated up, defense rated worse (allows more)
    assert r.pass_edge(1, 2) > r.pass_edge(1, 3) > 0
    assert abs(r.ro[1]) < 1e-9
    r.roll(2024)
    r.roll(2025)
    assert r.po[1] == pytest.approx(0.1 * 0.5 * (1 - r.revert))


def test_ridge_recovers_a_linear_relationship():
    rng = random.Random(4)
    rows = [{"a": rng.gauss(0, 1), "b": rng.gauss(0, 2)} for _ in range(500)]
    y = [2 + 3 * r["a"] - 1 * r["b"] + rng.gauss(0, 0.1) for r in rows]
    f = ridge(rows, y, ("a", "b"), lam=0.0)
    assert f.raw_coef("a") == pytest.approx(3, abs=0.02)
    assert f.raw_coef("b") == pytest.approx(-1, abs=0.02)
    assert f.predict({"a": 0, "b": 0}) == pytest.approx(2, abs=0.02)


def test_estimate_out_prob_uses_only_the_given_seasons():
    log = [("Questionable", True, 2015)] * 70 + [("Questionable", False, 2015)] * 30
    log += [("Questionable", False, 2022)] * 500
    probs = estimate_out_prob(log, {2015})
    assert probs["Questionable"] == pytest.approx(0.3)
    assert probs["Doubtful"] == 0.9  # too few examples: default kept


def test_features_never_see_later_games():
    teams = range(1, 5)
    ctx = Context()
    games, rng = [], random.Random(2)
    start = datetime(2020, 9, 13, 17, tzinfo=UTC)
    for wk in range(8):
        for i, (h, a) in enumerate([(1, 2), (3, 4)] if wk % 2 else [(2, 3), (4, 1)]):
            gid = wk * 10 + i
            games.append(game(gid, h, a, at=start + timedelta(weeks=wk), hs=rng.randint(10, 30),
                              as_=rng.randint(10, 30), spread=-3.0))  # fmt: skip
            games[-1] = replace(games[-1], week=wk + 1)
            ctx.stats[gid] = {t: (35, rng.gauss(0, 5), 25, rng.gauss(-1, 3)) for t in (h, a)}
            ctx.snaps[gid] = {t: snaps((f"qb{t}", "QB", 1.0), (f"cb{t}", "CB", 0.9)) for t in teams}
    base = build_features(games, ctx)
    cut = games[len(games) // 2].start
    later = [g.game_id for g in games if g.start >= cut]
    for gid in later:
        ctx.stats[gid] = {t: (35, 99.0, 25, 99.0) for t in ctx.stats[gid]}
        ctx.snaps[gid] = {}
    for a, b in zip(base, build_features(games, ctx), strict=True):
        if a.game.start <= cut:
            assert (a.margin, a.total) == (b.margin, b.total)


# --- Totals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hs, as_, side, result",
    [(24, 21, "over", "win"), (20, 21, "over", "loss"), (21, 23.5, "over", "push"),
     (20, 21, "under", "win")],
)  # fmt: skip
def test_grade_total(hs, as_, side, result):
    g = replace(game(1, 1, 2, hs=hs, as_=0, spread=-3.0), away_score=as_, total=44.5)
    assert grade_total(g, side) == result


def test_total_picks_and_threshold_choice():
    g = replace(game(1, 1, 2, hs=30, as_=20, spread=-3.0), total=44.5, over_price=-105)
    [p] = total_picks([(g, Prediction(0, total=49.0))], 3.0)
    assert (p.side, p.line, p.price, p.result) == ("over", 44.5, -105, "win")
    assert total_picks([(g, Prediction(0, total=46.0))], 3.0) == []
    assert total_picks([(g, Prediction(0))], 1.0) == []  # model without totals
    preds = [
        (replace(g, game_id=i), Prediction(0, total=40.0 if i % 2 else 50.0)) for i in range(300)
    ]
    t, by = choose_threshold(total_picks, preds, [2020], thresholds=(1.0, 10.0))
    assert t == 1.0 and by[0][1].bets == 300 and by[1][1].bets == 0
