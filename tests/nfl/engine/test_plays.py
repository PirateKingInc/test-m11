import numpy as np
import pandas as pd

from betdata.nfl.engine import plays as P
from betdata.nfl.engine import stability as S


def test_yardline_from_the_offence():
    spot = pd.Series(["KC 25", "BUF 40", "MID 50", None])
    pos = pd.Series(["KC", "KC", "KC", "KC"])
    assert P.yardline(spot, pos).tolist()[:3] == [75.0, 40.0, 50.0]


def _pbp():
    rows = []
    base = dict(game_id="2020_01_BUF_KC", season=2020, week=1, season_type="REG",
                home_team="KC", away_team="BUF", posteam="KC", defteam="BUF", fixed_drive=1,
                fixed_drive_result="Touchdown", drive_start_yard_line="KC 25",
                drive_end_yard_line="BUF 5", drive_time_of_possession="2:30", qtr=1,
                half_seconds_remaining=1700, game_seconds_remaining=3500, score_differential=0,
                down=1, ydstogo=10, yardline_100=75, qb_dropback=0, qb_scramble=0, sack=0,
                pass_attempt=0, rush_attempt=0, complete_pass=0, play_type="run")  # fmt: skip
    rows.append({**base, "play_id": 1, "rush_attempt": 1})  # designed run
    rows.append({**base, "play_id": 2, "play_type": "pass", "qb_dropback": 1, "pass_attempt": 1,
                 "complete_pass": 1, "yardline_100": 15})  # attempt, red zone
    rows.append({**base, "play_id": 3, "play_type": "pass", "qb_dropback": 1, "sack": 1,
                 "pass_attempt": 1})  # sack
    rows.append({**base, "play_id": 4, "play_type": "run", "qb_dropback": 1, "qb_scramble": 1,
                 "rush_attempt": 1, "score_differential": 14})  # scramble, not neutral
    rows.append({**base, "play_id": 5, "play_type": "no_play"})
    return pd.DataFrame(rows)


def test_play_flags():
    d = P.flag_plays(_pbp())
    assert d.scrim.tolist() == [True, True, True, True, False]
    assert d.run.tolist() == [True, False, False, False, False]
    assert d.attempt.tolist() == [False, True, False, False, False]
    assert d.is_sack.tolist() == [False, False, True, False, False]
    assert d.scramble.tolist() == [False, False, False, True, False]
    assert d.dropback.sum() == 3 and d.rz.sum() == 1
    assert d.neutral.tolist() == [True, True, True, False, False]


def test_stability_recovers_a_known_reliability():
    """Units with true rates spread around 0.5 (sd 0.1), 200 trials a game over 16 games: the
    split-half r and the padding match the binomial theory (k = p(1-p)/var = 25)."""
    rng = np.random.default_rng(0)
    units, games = 400, 16
    p = np.clip(rng.normal(0.5, 0.1, units), 0.01, 0.99)
    n = 200
    rows = [{"unit": u, "season": 2020, "order": g, "num": rng.binomial(n, p[u]), "den": n}
            for u in range(units) for g in range(games)]  # fmt: skip
    df = pd.DataFrame(rows)
    r = S.measure(S.Rate("x", "team_off", "num", "den", 1, 1), df)
    theory = 1600 / (1600 + 25)  # half = 8 games x 200 trials
    assert abs(r["r_split_half"] - theory) < 0.02
    assert 15 < r["padding"] < 40
