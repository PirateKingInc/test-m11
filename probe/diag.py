"""Ledger probe: why the M16 news alerts of Oct 25-26 have no result. Counts and fields only."""

import sqlite3

p = sqlite3.connect("data/poller.sqlite")
m = sqlite3.connect("data/betdata.sqlite")
rows = p.execute("select alert_id, game_id, game_label, tip, market, side, line from alerts "
                 "where signal='news' order by alert_id").fetchall()
print("news alerts", rows)
for aid, gid, *_ in rows:
    print("grade", aid, p.execute("select * from alert_grades where alert_id=?", (aid,)).fetchall())
    print("game", gid, m.execute("select game_id, sport, start_time_utc, status, home_team_id, "
                                 "away_team_id from games where game_id=?", (gid,)).fetchall())
    print("results", gid, m.execute("select * from results where game_id=?", (gid,)).fetchall())
print("alert_grades by signal", p.execute(
    "select a.signal, count(*), sum(g.result is not null), sum(g.close_at is not null) "
    "from alerts a left join alert_grades g on g.alert_id=a.alert_id group by 1").fetchall())
print("last runs", p.execute("select run_id, started_at, status, message from poller_runs "
                             "order by run_id desc limit 3").fetchall())
