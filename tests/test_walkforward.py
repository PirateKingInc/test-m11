"""The walk-forward tool (owner 2026-11-12, betdata/walkforward.py): no look-ahead."""

from datetime import UTC, date, datetime, timedelta

from betdata import walkforward as WF

T = datetime(2026, 11, 13, 11, 15, tzinfo=UTC)  # Nov 13's selection, 6:15 AM ET
TIP = "2026-11-14T00:30:00+00:00"  # a Nov 13 (ET) game


def _p(pid, *, made, p=0.6, odds="-110", side="over", line="4.5", median="5.5", res="win",
       graded=None, player=None, game="7", market="assists", tip=TIP):
    return {"pick_id": pid, "sport": "nba", "signal": "snipe", "signal_name": "F",
            "game_id": game, "game": "NYK @ BOS", "tip": tip, "player": player or pid,
            "market": market, "side": side, "line": line, "odds": odds, "fair_prob": str(p),
            "created_at": made.isoformat(), "result": res,
            "profit": {"win": "0.9091", "loss": "-1.0"}.get(res, "0"),
            "graded_at": (graded or made + timedelta(days=1)).isoformat(),
            "proj_median": median, "clv_consensus": "", "filter": ""}


def _data(picks, extra_days=()):
    days = {"2026-11-13": T, **dict(extra_days)}
    return WF.Data(picks, days, {}, [r for r in picks if r["result"] in ("win", "loss")])


def test_a_pick_created_after_selection_is_ignored():
    ok = _p("ok", made=T - timedelta(hours=1), p=0.55)
    future = _p("future", made=T + timedelta(minutes=1), p=0.95)  # planted: better, but late
    at_t = _p("at_t", made=T, p=0.9)  # created exactly at selection: not "before"
    res = WF.run(_data([ok, future, at_t]), "v2top5")
    assert [r["pick_id"] for r in res["days"]["2026-11-13"]] == ["ok"]


def test_fits_never_see_a_grade_from_that_day_or_later():
    """A pick graded at or after the day's selection time never enters the fit used that day."""
    old = [_p(f"o{i}", made=T - timedelta(days=5), p=0.6, res="win" if i % 2 else "loss",
              graded=T - timedelta(days=4), player=f"o{i}", game=str(100 + i),
              tip="2026-11-09T00:00:00+00:00")
           for i in range(250)]
    late = [_p(f"l{i}", made=T - timedelta(hours=3), p=0.6, res="loss",
               graded=T + timedelta(hours=i % 30), player=f"l{i}", game=str(500 + i),
               tip="2026-11-09T00:00:00+00:00")
            for i in range(300)]  # graded from T on: invisible on Nov 13
    late.append(_p("lat", made=T - timedelta(days=1), res="loss", graded=T, player="lat",
                   game="999", tip="2026-11-09T00:00:00+00:00"))
    d = _data(old + late)
    f = WF.global_fit(d, T)
    assert f is not None and f.n == 250  # only the 250 graded before T


def test_one_pick_per_player_and_rules():
    a = _p("a", made=T - timedelta(hours=2), p=0.6, player="X")
    b = _p("b", made=T - timedelta(hours=2), p=0.7, player="X", line="3.5")  # same player: kept
    long = _p("long", made=T - timedelta(hours=2), p=0.9, odds="200")  # price rule
    wrong = _p("wrong", made=T - timedelta(hours=2), p=0.9, median="4.0")  # median rule
    reb = _p("reb", made=T - timedelta(hours=2), p=0.9, market="rebounds")  # Rules v2
    res = WF.run(_data([a, b, long, wrong, reb]), "v2top5")
    assert [r["pick_id"] for r in res["days"]["2026-11-13"]] == ["b"]
    assert res["counts"]["2026-11-13"] == (5, 2, 1)  # raw, after rules, after one per player


def test_calibrated_rule_is_identical_and_split_mode_scores_after_cutoff():
    picks = [_p(f"k{i}", made=T - timedelta(hours=2), p=0.5 + i / 100, player=f"k{i}")
             for i in range(8)]
    old = [_p(f"o{i}", made=T - timedelta(days=5), p=0.6, res="win" if i % 2 else "loss",
              graded=T - timedelta(days=4), player=f"o{i}", game=str(100 + i),
              tip="2026-11-09T00:00:00+00:00") for i in range(250)]
    t2 = T - timedelta(days=2)
    d = _data(picks + old, extra_days=[("2026-11-11", t2)])
    a, b = WF.run(d, "v2top5"), WF.run(d, "v2cal")
    assert WF.same_picks(a, b) == (2, 2)
    split = WF.run(d, "v2top5", cutoff=date(2026, 11, 12))
    assert list(split["days"]) == ["2026-11-13"]  # only days from the cutoff are scored


def test_report_marks_in_and_out_of_sample(tmp_path):
    import inspect

    src = inspect.getsource(WF.report)
    assert "in-sample" in src and "out-of-sample" in src and "raw" in src
