"""M15 lineup stints from play-by-play, on small synthetic games (no database)."""

import pytest

from betdata.nba.stints import Play, build_stints, clock_seconds, possessions

HT, AT = "10", "20"  # ESPN team ids: home, away
H = ["h1", "h2", "h3", "h4", "h5"]
A = ["a1", "a2", "a3", "a4", "a5"]
ROSTER = {p: "home" for p in [*H, "h6", "h7"]} | {p: "away" for p in [*A, "a6", "a7"]}


class Game:
    def __init__(self):
        self.plays, self.score = [], {HT: 0, AT: 0}

    def add(self, period, clock, kind, team, *athletes, pts=0, shooting=False):
        if pts:
            self.score[team] += pts
        self.plays.append(Play(len(self.plays) + 1, period, clock, kind, team, athletes,
                               self.score[HT], self.score[AT], shooting))  # fmt: skip
        return self

    def shot(self, period, clock, team, who, pts=0):
        return self.add(period, clock, "Jump Shot", team, who, pts=pts, shooting=True)

    def sub(self, period, clock, team, enters, leaves):
        return self.add(period, clock, "Substitution", team, enters, leaves)

    def end(self, period):
        return self.add(period, 0.0, "End Period", None)

    def build(self, roster=ROSTER, starters=frozenset(H + A), espn_home=HT):
        return build_stints(self.plays, roster, set(starters), espn_home)


def players(*ids):
    return ",".join(sorted(ids))


def test_substitution_mid_period_starts_a_stint():
    g = (Game().shot(1, 700, HT, "h1", 2).shot(1, 690, AT, "a1")
         .add(1, 688, "Defensive Rebound", HT, "h2").sub(1, 600, HT, "h6", "h1")
         .shot(1, 500, AT, "a2", 3).end(1))  # fmt: skip
    r = g.build()
    assert not r.unresolved
    first, second = r.stints
    assert (first["start_sec"], first["end_sec"], second["start_sec"], second["end_sec"]) == (
        0, 120, 120, 720)
    assert first["home_players"] == players(*H) and first["away_players"] == players(*A)
    assert second["home_players"] == players("h2", "h3", "h4", "h5", "h6")
    assert (first["home_pts"], first["away_pts"], second["home_pts"], second["away_pts"]) == (
        2, 0, 0, 3)
    assert (first["home_poss"], first["away_poss"]) == (1, 1)


def test_later_period_lineup_inferred_and_filled_from_previous_period():
    g = Game().sub(1, 600, HT, "h6", "h1").end(1)
    # Period 2: h2 and h3 act, h4 leaves before entering; h5 and h6 never show up: they come
    # from period 1's closing lineup. h7 entered during the period, so he wasn't on at its start.
    g.shot(2, 700, HT, "h2", 2).add(2, 690, "Defensive Rebound", HT, "h3")
    for a in A:
        g.shot(2, 680, AT, a)
    g.sub(2, 650, HT, "h7", "h4").end(2)
    r = g.build()
    assert not r.unresolved
    p2 = [s for s in r.stints if s["period"] == 2]
    assert p2[0]["start_sec"] == 720 and p2[0]["home_players"] == players(
        "h2", "h3", "h4", "h5", "h6")
    assert p2[1]["start_sec"] == 720 + 70 and "h7" in p2[1]["home_players"]
    assert p2[-1]["end_sec"] == 1440


def test_overtime_period_length():
    g = Game().end(1)
    for period in (2, 3, 4, 5):
        g.end(period)
    r = g.build()
    assert r.stints[-1]["period"] == 5 and r.stints[-1]["start_sec"] == 2880
    assert r.stints[-1]["end_sec"] == 2880 + 300


def test_unresolved_period_is_skipped_and_counted():
    g = Game().end(1)
    # Period 2: three home players act (h7 from the bench), so two of h1, h4, h5 must have
    # started it: ambiguous.
    g.shot(2, 700, HT, "h2").shot(2, 690, HT, "h3").shot(2, 680, HT, "h7").end(2)
    g.shot(3, 700, HT, "h1").end(3)  # no closing lineup from period 2 to fill from
    r = g.build()
    assert [p for p, _ in r.unresolved] == [2, 3]
    assert {s["period"] for s in r.stints} == {1}


def test_substitution_of_player_not_on_floor_is_unresolved():
    r = Game().sub(1, 600, HT, "h6", "h7").end(1).build()
    assert r.unresolved and "substitution" in r.unresolved[0][1] and not r.stints


def test_missing_starters_fall_back_to_inference_in_period_one():
    g = Game()
    for p in H:
        g.shot(1, 700, HT, p)
    for p in A:
        g.shot(1, 690, AT, p)
    r = g.end(1).build(starters=set(H[:3] + A))  # the box score names only 3 home starters
    assert not r.unresolved and r.stints[0]["home_players"] == players(*H)


def test_subbed_in_and_out_in_the_same_dead_ball_never_appears():
    g = (Game().add(1, 400, "Shooting Foul", AT, "a1")
         .add(1, 400, "Free Throw - 1 of 2", HT, "h2", pts=1)
         .sub(1, 400, HT, "h6", "h3").sub(1, 400, HT, "h3", "h6")
         .add(1, 400, "Free Throw - 2 of 2", HT, "h2", pts=1).end(1))  # fmt: skip
    r = g.build()
    assert len(r.stints) == 1 and "h6" not in r.stints[0]["home_players"]
    assert r.stints[0]["home_pts"] == 2


def test_free_throws_around_a_substitution_belong_to_the_lineup_of_the_foul():
    g = (Game().add(1, 400, "Shooting Foul", AT, "a1")
         .add(1, 400, "Free Throw - 1 of 2", HT, "h2", pts=1).sub(1, 400, HT, "h6", "h3")
         .add(1, 400, "Free Throw - 2 of 2", HT, "h2", pts=1).end(1))  # fmt: skip
    first, second = g.build().stints
    assert first["home_pts"] == 2 and first["end_sec"] == 320 == second["start_sec"]
    assert first["home_poss"] == pytest.approx(0.88) and second["home_pts"] == 0


def test_substitutions_at_period_start_replace_the_inferred_five():
    g = Game().end(1)
    g.sub(2, 720, HT, "h6", "h1").add(2, 720, "Jumpball", HT, "h6", "a1")
    for p in ["h2", "h3", "h4", "h5", *A]:
        g.shot(2, 700, HT if p.startswith("h") else AT, p)
    r = g.end(2).build()
    p2 = [s for s in r.stints if s["period"] == 2]
    assert len(p2) == 1 and p2[0]["start_sec"] == 720
    assert p2[0]["home_players"] == players("h2", "h3", "h4", "h5", "h6")


def test_score_follows_our_orientation_when_espn_home_is_our_away():
    g = Game().shot(1, 700, HT, "h1", 3).shot(1, 690, AT, "a1", 2).end(1)
    flipped = {p: "away" if s == "home" else "home" for p, s in ROSTER.items()}
    s = g.build(roster=flipped).stints[0]
    assert (s["home_pts"], s["away_pts"]) == (2, 3)
    assert s["home_players"] == players(*A)


def test_possession_estimate_per_play():
    def play(kind, athletes=("x",), shooting=False):
        return Play(1, 1, 0.0, kind, HT, athletes, 0, 0, shooting)

    assert possessions(play("Jump Shot", shooting=True)) == 1
    assert possessions(play("Free Throw - 1 of 2", shooting=True)) == 0.44
    assert possessions(play("Free Throw - Technical", shooting=True)) == 0
    assert possessions(play("Offensive Rebound")) == -1
    assert possessions(play("Offensive Rebound", ())) == 0  # team rebound
    assert possessions(play("Bad Pass\nTurnover")) == 1
    assert possessions(play("Traveling")) == 1
    assert possessions(play("No Turnover")) == 0
    assert possessions(play("Defensive Rebound")) == 0


def test_clock_formats():
    assert clock_seconds("7:21") == 441
    assert clock_seconds("0:32.1") == pytest.approx(32.1)
    assert clock_seconds("32.1") == pytest.approx(32.1)  # ESPN's live clock under a minute
    assert clock_seconds("1:38:59") == pytest.approx(98.59)
