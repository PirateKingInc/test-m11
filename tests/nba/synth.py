"""A small synthetic NBA league written through the real tables (M14 tests)."""

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    markets,
    nba_injury_reports,
    nba_player_games,
    odds_snapshots,
    results,
    teams,
)

ET = ZoneInfo("America/New_York")
TEAMS = ["Boston Celtics", "Denver Nuggets", "Miami Heat", "Utah Jazz"]


def synth(engine, n_days=40, seed=1):
    """Four teams, a game a day, eight players a team with box lines, one injury report and
    an opening line per game."""
    rnd = random.Random(seed)
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        book = conn.execute(select(books.c.book_id).where(books.c.key == "draftkings")).scalar()
        mkt = dict(conn.execute(select(markets.c.key, markets.c.market_id)).all())
        start = datetime(2021, 10, 20, 23, 30, tzinfo=UTC)
        ids = []
        for d in range(n_days):
            tip = start + timedelta(days=d)
            h, a = rnd.sample(TEAMS, 2)
            gid = conn.execute(insert(games).values(
                sport_id=sid, season=2021, season_type="REG", start_time_utc=tip,
                home_team_id=tid[h], away_team_id=tid[a], status="final")).inserted_primary_key[0]
            conn.execute(insert(game_source_ids).values(source="espn", source_game_id=str(gid),
                                                        game_id=gid))  # fmt: skip
            hs, as_ = rnd.randint(90, 125), rnd.randint(90, 125)
            conn.execute(insert(results).values(game_id=gid, home_score=hs, away_score=as_,
                                                source="espn_nba", final=True))  # fmt: skip
            for team in (h, a):
                k = TEAMS.index(team)
                for j in range(8):
                    if j == 0 and rnd.random() < 0.3:
                        continue  # the star misses some games
                    mins = rnd.uniform(14, 38)
                    conn.execute(insert(nba_player_games).values(
                        game_id=gid, team_id=tid[team], player_id=f"{k}{j}",
                        player_name=f"Pl{'abcdefgh'[j]} Tm{'abcd'[k]}", starter=j < 5,
                        did_not_play=False,
                        minutes=mins, points=rnd.randint(2, 30), plus_minus=rnd.randint(-15, 15),
                        fgm=5, fga=11, fg3m=1, fg3a=4, ftm=2, fta=3, oreb=1, dreb=3, ast=2,
                        stl=1, blk=0, tov=2, pf=2, source="sportsdataverse",
                        known_at=tip + timedelta(hours=3), fetched_at=tip))  # fmt: skip
            k = TEAMS.index(h)
            conn.execute(insert(nba_injury_reports).values(
                game_date=tip.astimezone(ET).date().isoformat(), matchup="X@Y", game_id=gid,
                team_id=tid[h], team_name=h, player_name=f"Tm{'abcd'[k]}, Pla",
                status=rnd.choice(["Out", "Questionable", "Probable"]), reason="Knee",
                report_at=tip - timedelta(hours=8), known_at=tip - timedelta(hours=7, minutes=45),
                source="nba_injury", fetched_at=tip))  # fmt: skip
            line = rnd.choice([-6.5, -2.5, 1.5, 4.0])
            for kind, at in (("source_opening", tip + timedelta(days=300)),
                             ("poll", tip - timedelta(hours=2))):  # fmt: skip
                conn.execute(insert(odds_snapshots).values(
                    source="espn_nba", source_game_id=str(gid), game_id=gid, book_id=book,
                    market_id=mkt["spreads"], outcome="home", line=line, price_american=-110,
                    snapshot_kind=kind, fetched_at=at))  # fmt: skip
            ids.append(gid)
    return ids
