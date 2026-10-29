"""M18 verification: the value rule replayed on a stored NBA news event (a copy of the poller
database; the original is untouched)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select

from betdata.alerts.db import alerts, engine_for, quotes, repredictions, status_events
from betdata.alerts.replay import replay
from tests.nba.synth import synth

STATED = datetime(2021, 10, 20, 17, 45, tzinfo=UTC)


def test_replay_reproduces_a_value_alert(engine, tmp_path):
    gid = synth(engine, n_days=2)[0]
    src = tmp_path / "poller.sqlite"
    pdb = engine_for(src)
    with pdb.begin() as p:
        ev = p.execute(insert(status_events).values(
            sport="nba", game_id=gid, player_key="s", player_name="Star, A", team_id=1,
            old_status="Questionable", new_status="Out", prob_before=0.55, prob_after=0.0,
            weight=34.0, qualifies=True, report_at=STATED,
            detected_at=STATED + timedelta(minutes=12))).inserted_primary_key[0]  # fmt: skip
        p.execute(insert(repredictions).values(
            event_id=ev, game_id=gid, model_version="t", margin_before=3.0, margin_after=-1.0,
            total_before=220.0, total_after=216.0, props=[],
            created_at=STATED + timedelta(minutes=12)))  # fmt: skip
        rows = []
        for t, e in ((STATED - timedelta(minutes=30), None), (STATED + timedelta(minutes=13), ev)):
            for book in ("draftkings", "fanduel"):
                for side, line in (("home", -1.5), ("away", 1.5)):
                    rows.append(dict(polled_at=t, sport="nba", game_id=gid, event_id=e,
                                     source="sharpapi", book=book, market="spreads",
                                     outcome=side, line=line, price=-110))
        p.execute(insert(quotes), rows)
    res = replay(engine, src, tmp_path / "work.sqlite", STATED + timedelta(days=2))
    assert res["events"] == 1 and res["replayed"][0]["alerts"] == 1
    [a] = res["alerts"]
    assert (a["market"], a["side"], a["line"]) == ("spreads", "away", 1.5)
    with pdb.connect() as p:  # the original database is untouched
        assert p.execute(select(func.count()).select_from(alerts)).scalar() == 0
