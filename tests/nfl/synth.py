"""A small synthetic league with every as-of frame filled, for harness and model tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from betdata.nfl.data import _rest_days
from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.points import final_report_time, prekick_time
from betdata.nfl.store import Frame, Store

TEAMS = list(range(1, 9))
POS = [("QB", 1), ("OL", 5), ("WR", 3), ("TE", 1), ("RB", 1), ("DL", 4), ("LB", 3), ("CB", 3),
       ("S", 2)]  # fmt: skip
VENUES = {  # team -> (lat, lon, elevation, tz)
    1: (42.1, -71.3, 50, "America/New_York"), 2: (40.8, -74.1, 10, "America/New_York"),
    3: (39.7, -105.0, 1600, "America/Denver"), 4: (37.4, -121.9, 10, "America/Los_Angeles"),
    5: (41.9, -87.6, 180, "America/Chicago"), 6: (29.7, -95.4, 15, "America/Chicago"),
    7: (47.6, -122.3, 5, "America/Los_Angeles"), 8: (25.9, -80.2, 3, "America/New_York"),
}  # fmt: skip


def make_store(seasons=(2018, 2019, 2020), weeks=10, seed=1) -> Store:
    rng = np.random.default_rng(seed)
    strength = {t: rng.normal(0, 4) for t in TEAMS}
    games, gid = [], 1
    for season in seasons:
        start = datetime(season, 9, 9, 17, 0, tzinfo=UTC)  # a Sunday-ish 1 pm ET
        for week in range(1, weeks + 1):
            order = list(rng.permutation(TEAMS))
            for i in range(0, len(order), 2):
                h, a = int(order[i]), int(order[i + 1])
                k = start + timedelta(days=7 * (week - 1), hours=int(rng.choice([0, 3, 7])))
                if week == 3 and i == 0:
                    k -= timedelta(days=3)  # a Thursday game
                lat, lon, elev, tz = VENUES[h]
                games.append(dict(game_id=gid, season=season, week=week, season_type="REG",
                                  kickoff=pd.Timestamp(k), home=h, away=a, neutral=False,
                                  status="final", venue_id=h, lat=lat, lon=lon, elevation_m=elev,
                                  tz=tz, country="US", venue_roof="open",
                                  div_game=bool((h + a) % 3 == 0),
                                  surface="grass" if h % 2 else "fieldturf",
                                  roof_type="outdoors" if h % 4 else "dome"))  # fmt: skip
                gid += 1
    g = _rest_days(pd.DataFrame(games))
    g.index = g["game_id"]

    res, tg, pg, snaps, inj, ros, coaches, gameday, odds, weather = ([] for _ in range(10))
    players = {(t, grp, j): f"P{t}-{grp}{j}" for t in TEAMS for grp, n in POS
               for j in range(n + 1)}  # fmt: skip
    for r in g.itertuples(index=False):
        known = r.kickoff + RESULT_DELAY
        exp = strength[r.home] - strength[r.away] + 2
        hs = max(0, int(round(22 + exp / 2 + rng.normal(0, 7))))
        as_ = max(0, int(round(22 - exp / 2 + rng.normal(0, 7))))
        res.append(dict(game_id=r.game_id, home_score=hs, away_score=as_, known_at=known))
        for team, opp, home in ((r.home, r.away, True), (r.away, r.home, False)):
            pp, rp = int(rng.integers(28, 42)), int(rng.integers(20, 32))
            stats = dict(plays=pp + rp, pass_plays=pp, pass_epa=rng.normal(0.05, 0.2) * pp,
                         pass_succ=pp * 0.45, rush_plays=rp, rush_epa=rng.normal(-0.05, 0.15) * rp,
                         rush_succ=rp * 0.4, early_plays=40.0, early_epa=rng.normal(0, 4),
                         explosive=float(rng.integers(3, 10)), neutral_plays=40.0,
                         neutral_pass=22.0, neutral_secs=40 * 27.0, neutral_secs_n=40.0,
                         early_neutral_n=30.0, early_neutral_pass_oe=rng.normal(0, 30),
                         pass_att=float(pp - 2), ints=float(rng.integers(0, 3)),
                         fumbles=float(rng.integers(0, 3)), fumbles_lost=1.0,
                         fourth_opps=2.0, fourth_goes=float(rng.integers(0, 3)),
                         penalties=float(rng.integers(3, 10)), penalty_yards=50.0,
                         fumble_recoveries=1.0, game_fumbles=2.0)  # fmt: skip
            tg.append(dict(game_id=r.game_id, season=r.season, week=r.week, team=team, opp=opp,
                           home=home, neutral=False, kickoff=r.kickoff, known_at=known, **stats))
            qb = players[(team, "QB", 0 if rng.random() > 0.15 else 1)]
            pg.append(dict(game_id=r.game_id, season=r.season, week=r.week, team=team, key=qb,
                           kickoff=r.kickoff, known_at=known, dropbacks=float(pp),
                           dropback_epa=rng.normal(0.05, 0.2) * pp, carries=2.0, carry_epa=0.1,
                           targets=0.0, target_epa=0.0))  # fmt: skip
            for grp, n in POS:
                for j in range(n + 1):
                    key = players[(team, grp, j)]
                    if grp != "QB" and grp in ("WR", "TE", "RB"):
                        pg.append(dict(game_id=r.game_id, season=r.season, week=r.week,
                                       team=team, key=key, kickoff=r.kickoff, known_at=known,
                                       dropbacks=0.0, dropback_epa=0.0, carries=3.0 * (j == 0),
                                       carry_epa=rng.normal(0, 1), targets=5.0 - j,
                                       target_epa=rng.normal(0, 2)))  # fmt: skip
                    share = 1.0 if j < n else 0.2
                    if grp == "QB":
                        share = 1.0 if key == qb else 0.0
                    if share and rng.random() > 0.05:
                        snaps.append(dict(game_id=r.game_id, season=r.season, week=r.week,
                                          team=team, key=key, group=grp, share=share,
                                          kickoff=r.kickoff, known_at=known))  # fmt: skip
                    status = "ACT" if rng.random() > 0.05 else rng.choice(["INA", "RES"])
                    ros.append(dict(season=r.season, week=r.week, team=team, key=key,
                                    status=status, group=grp, position=grp,
                                    known_at=prekick_time(r.kickoff)))  # fmt: skip
                    if rng.random() < 0.08:
                        inj.append(dict(season=r.season, week=r.week, team=team, key=key,
                                        group=grp, injury_id=len(inj) + 1,
                                        report_status=rng.choice(["Out", "Questionable",
                                                                  "Doubtful"]),
                                        practice_status=rng.choice([
                                            "Did Not Participate In Practice",
                                            "Limited Participation in Practice",
                                            "Full Participation in Practice"]),
                                        known_at=final_report_time(r.kickoff)
                                        - timedelta(hours=int(rng.integers(0, 30)))))  # fmt: skip
        coaches.append(dict(game_id=r.game_id, home_coach=f"C{r.home}", away_coach=f"C{r.away}",
                            known_at=final_report_time(r.kickoff)))  # fmt: skip
        gameday.append(dict(game_id=r.game_id, referee=f"Ref{r.game_id % 5}",
                            roof_state=None, known_at=prekick_time(r.kickoff)))  # fmt: skip
        for days in (6, 3, 0.1):
            odds.append(dict(game_id=r.game_id, market="spreads", line=-exp + rng.normal(0, 1),
                             source="nflverse", known_at=r.kickoff - timedelta(days=days)))
            odds.append(dict(game_id=r.game_id, market="totals", line=44 + rng.normal(0, 2),
                             source="nflverse", known_at=r.kickoff - timedelta(days=days)))
            weather.append(dict(game_id=r.game_id, temp_c=rng.normal(10, 8),
                                wind_kph=abs(rng.normal(12, 8)), gust_kph=20.0,
                                precip_mm=max(0.0, rng.normal(0, 1)),
                                known_at=r.kickoff - timedelta(days=days)))  # fmt: skip
    frames = dict(results=res, team_games=tg, player_games=pg, snaps=snaps, injuries=inj,
                  rosters=ros, coaches=coaches, gameday=gameday, odds=odds, weather=weather)
    return Store(g, {k: Frame(pd.DataFrame(v)) for k, v in frames.items()})
