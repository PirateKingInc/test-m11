"""One game's spread quoted in both conventions must give one stored home line and one pick.

nflverse `spread_line` is the expected home margin (home favoured = positive); sportsbooks
(SharpAPI, The Odds API) quote the home team's line (home favoured = negative). Every parser must
store the sportsbook convention, and every consumer (grading, the alert signal, the engine's
value layer) must pick the same side from it. Audit of the live 0-8 m13w run, 2026-10-28.
"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from betdata.alerts import rules as R
from betdata.nfl.engine.value import side_bets
from betdata.nfl.grade import bets_for, result_of
from betdata.sources import nflverse, sharpapi, the_odds_api

AT = datetime(2026, 10, 25, 12, tzinfo=UTC)
KICK = "2026-10-25T17:00:00Z"
HOME, AWAY = "Kansas City Chiefs", "Denver Broncos"


def _nflverse(spread_line: float) -> bytes:
    cols = sorted(nflverse.REQUIRED_COLUMNS)
    row = {c: "" for c in cols} | {
        "game_id": "2026_07_DEN_KC", "season": "2026", "game_type": "REG", "week": "7",
        "gameday": "2026-10-25", "gametime": "13:00", "away_team": "DEN", "home_team": "KC",
        "location": "Home", "spread_line": str(spread_line), "home_spread_odds": "-110",
        "away_spread_odds": "-110"}  # fmt: skip
    return (",".join(cols) + "\n" + ",".join(row[c] for c in cols) + "\n").encode()


def _sharpapi(home_line: float) -> bytes:
    def line(side, team, pt):
        return {"sportsbook": "draftkings", "event_id": "e1", "home_team": HOME,
                "away_team": AWAY, "market_type": "point_spread", "team_side": side,
                "selection": team, "odds_american": -110, "line": pt,
                "event_start_time": KICK, "is_main_line": True}  # fmt: skip
    return json.dumps({"data": [line("home", HOME, home_line),
                                line("away", AWAY, -home_line)]}).encode()  # fmt: skip


def _odds_api(home_line: float) -> bytes:
    return json.dumps([{"id": "x1", "sport_key": "americanfootball_nfl", "commence_time": KICK,
                        "home_team": HOME, "away_team": AWAY, "bookmakers": [{
                            "key": "fanduel", "title": "FanDuel", "markets": [{
                                "key": "spreads", "outcomes": [
                                    {"name": AWAY, "price": -110, "point": -home_line},
                                    {"name": HOME, "price": -110, "point": home_line}]}]}]}]
                      ).encode()  # fmt: skip


def _home_line(parsed) -> float:
    (p,) = [x for x in parsed.prices if x.market_key == "spreads" and x.outcome == "home"]
    return p.line


@pytest.mark.parametrize("favoured_by", [3.0, -3.0, 7.5, 0.5])
def test_every_parser_stores_the_home_line_in_the_sportsbook_convention(favoured_by):
    """`favoured_by`: points the home team is expected to win by (negative: home is the dog)."""
    book_line = -favoured_by
    from_nflverse = _home_line(nflverse.parse_schedules(
        _nflverse(favoured_by), fetched_at=AT, seasons=None))  # nflverse: home-favoured +
    from_sharp, _ = sharpapi.parse_odds(_sharpapi(book_line), sport="nfl", fetched_at=AT,
                                        complete=True)  # fmt: skip
    from_odds_api = the_odds_api.parse_odds(_odds_api(book_line), sport="nfl", fetched_at=AT)
    assert from_nflverse == _home_line(from_sharp) == _home_line(from_odds_api) == book_line


@pytest.mark.parametrize("margin, side", [(9.0, "home"), (-1.0, "away")])
def test_every_consumer_picks_the_same_side_from_either_convention(margin, side):
    """KC favoured by 3: nflverse spread_line +3 and a book's KC -3 are the same market. A model
    margin of KC by 9 is a KC -3 pick; KC by -1 (DEN by 1) is a DEN +3 pick; everywhere."""
    lines = {
        "nflverse": _home_line(nflverse.parse_schedules(_nflverse(3.0), fetched_at=AT,
                                                        seasons=None)),  # fmt: skip
        "sharpapi": _home_line(sharpapi.parse_odds(_sharpapi(-3.0), sport="nfl", fetched_at=AT,
                                                   complete=True)[0]),  # fmt: skip
        "the_odds_api": _home_line(the_odds_api.parse_odds(_odds_api(-3.0), sport="nfl",
                                                           fetched_at=AT)),  # fmt: skip
    }
    want_line = -3.0 if side == "home" else 3.0
    for src, home_line in lines.items():
        (bet,) = bets_for(margin, None, home_line, None, {"spread": 3.0, "total": None})
        assert (bet.side, bet.line) == (side, want_line), src
        # KC wins 27-20 (by 7): the KC -3 pick wins, the DEN +3 pick loses
        assert result_of(bet, 27, 20) == ("win" if side == "home" else "loss"), src

        pick = R.side_signal("nfl", margin - 2.0 if side == "home" else margin + 2.0, margin,
                             [R.Spread("draftkings", home_line, -110, -110)])  # fmt: skip
        if abs(margin + home_line) >= R.SIDE_EDGE["nfl"]:
            assert (pick.side, pick.line) == (side, want_line), src

        eng = side_bets(pd.DataFrame({"margin_mean": [margin], "close_spread_home": [home_line],
                                      "act_margin": [7.0]}), 3.0)  # fmt: skip
        assert ("home" if eng.gap.iloc[0] > 0 else "away") == side, src
        assert eng.result.iloc[0] == (1.0 if side == "home" else 0.0), src
