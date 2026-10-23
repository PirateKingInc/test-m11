"""M16 rules (MODEL.md "M16", fixed before live data): qualifying changes, Signal 1 (side and
props), Signal 2, quiet hours and the lag study's move sizes."""

from datetime import UTC, datetime

from betdata.alerts import rules as R


def test_qualifying_status_changes():
    assert R.qualifies_nba(24.0, 0.50, 0.01)  # Questionable -> Out, a 24-minute starter
    assert not R.qualifies_nba(15.0, 0.50, 0.01)  # bench player
    assert not R.qualifies_nba(30.0, 0.97, 0.80)  # small move
    assert R.qualifies_nfl("QB", 0.55, 0.9, 0.0)
    assert not R.qualifies_nfl("WR", 0.55, 0.9, 0.0)  # WR needs 60%
    assert R.qualifies_nfl("T", 0.85, 0.9, 0.0, starter=True)
    assert not R.qualifies_nfl("T", 0.85, 0.9, 0.0)  # a lineman who is not a starter
    assert not R.qualifies_nfl("RB", None, 0.9, 0.0)


def test_side_signal_direction_threshold_and_best_book():
    dk = R.Spread("draftkings", -2.5, -110, -110)
    fd = R.Spread("fanduel", -2.0, -105, -115)
    # Star out on the away team: home margin 3.0 -> 6.0. DK edge 6 - 2.5 = 3.5, FD 4.0: FD.
    p = R.side_signal("nba", 3.0, 6.0, [dk, fd])
    assert (p.side, p.book, p.line, p.price, p.edge) == ("home", "fanduel", -2.0, -105, 4.0)
    assert R.side_signal("nba", 5.4, 6.0, [dk, fd]) is None  # moved < 1 point
    assert R.side_signal("nba", 3.0, 5.0, [dk]) is None  # edge 2.5 < 3.0 (NBA)
    assert R.side_signal("nfl", 3.0, 5.0, [dk]).edge == 2.5  # NFL threshold 2.5
    # Edge against the direction of the move does not alert: margin fell, home still covers.
    assert R.side_signal("nba", 12.0, 10.0, [R.Spread("draftkings", -6.0, -110, -110)]) is None
    # Margin fell to -1 against home -3: away covers by 4.
    p = R.side_signal("nba", 1.0, -1.0, [R.Spread("draftkings", -3.0, -110, -110)])
    assert (p.side, p.line) == ("away", 3.0)


def test_prop_signal():
    p = R.prop_signal(20.0, 23.0, 0.62, 21.5, -110, -110, "draftkings")
    assert p.side == "over" and abs(p.edge - 0.12) < 1e-9
    assert R.prop_signal(20.0, 21.0, 0.70, 21.5, -110, -110, "draftkings") is None  # +5% only
    assert R.prop_signal(20.0, 23.0, 0.55, 21.5, -110, -110, "draftkings") is None  # 5 pp
    assert R.prop_signal(20.0, 23.0, 0.70, 21.5, None, None, "draftkings") is None  # no prices
    u = R.prop_signal(20.0, 17.0, 0.38, 19.5, -110, -110, "draftkings")
    assert u.side == "under" and u.price == -110 and abs(u.fair_prob - 0.62) < 1e-9


def test_sharp_signal_same_line_gap_ev_and_age():
    pin = R.TwoWay("pinnacle", -3.5, -125, 115, age_min=2)  # no-vig home ~0.546
    soft = R.TwoWay("draftkings", -3.5, 105, -125, age_min=1)  # no-vig home ~0.475
    picks = R.sharp_signal(pin, soft)
    assert [(p.side, p.line, p.price) for p in picks] == [("home", -3.5, 105)]
    assert R.sharp_signal(pin, R.TwoWay("draftkings", -4.0, 105, -125, 1)) == []  # other line
    assert R.sharp_signal(R.TwoWay("pinnacle", -3.5, -125, 115, 11), soft) == []  # stale sharp
    assert R.sharp_signal(pin, R.TwoWay("draftkings", -3.5, 105, -125, 6)) == []  # stale soft
    tot = R.sharp_signal(R.TwoWay("pinnacle", 221.5, -125, 115, 1),
                         R.TwoWay("fanduel", 221.5, 105, -125, 1), sides=("over", "under"))
    assert [(p.side, p.line) for p in tot] == [("over", 221.5)]
    # A 4.5-pp gap that is not +EV at the soft price (+100 at a fair 50%: EV 0) does not alert.
    assert R.sharp_signal(R.TwoWay("pinnacle", None, -110, -110, 1),
                          R.TwoWay("fanduel", None, -150, 100, 1)) == []


def test_quiet_hours_eastern():
    assert R.quiet(datetime(2026, 10, 23, 3, 30, tzinfo=UTC))  # 23:30 EDT
    assert R.quiet(datetime(2026, 10, 23, 10, 59, tzinfo=UTC))  # 06:59 EDT
    assert not R.quiet(datetime(2026, 10, 23, 11, 0, tzinfo=UTC))  # 07:00 EDT
    assert not R.quiet(datetime(2026, 10, 23, 2, 59, tzinfo=UTC))  # 22:59 EDT


def test_lag_moves():
    assert R.moved("spreads", -3.5, 0.5, -4.0, 0.5)
    assert not R.moved("spreads", -3.5, 0.50, -3.5, 0.56)  # price only: not a spread move
    assert R.moved("h2h", None, 0.50, None, 0.516)
    assert not R.moved("points", 21.5, 0.50, 21.5, 0.51)
