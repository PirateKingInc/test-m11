"""Short / empty card reasons (owner 2026-11-12, betdata/card_reasons.py)."""



from betdata import card_reasons as CR
from betdata import morning as M
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, set_state
from tests.test_morning import NOW, _alert


def _og(picks, filtered=None, rejected=None, **kw):
    return {"sport": "nba", "day": "2026-11-06", "selected_at": NOW.isoformat(),
            "source": CD.SHADOW, "ranking": "v2", "picks": [{"dedup_key": k} for k in picks],
            "filtered": filtered or {}, "og_rejected": rejected or {}, **kw}


def test_og_reason_line_from_stored_reasons():
    card = _og([], {"a": "filtered: rebounds", "b": "filtered: rebounds",
                    "c": "filtered: rebounds", "d": "filtered: rebounds",
                    "e": "filtered: under 5.5+ assists", "f": "filtered: under 5.5+ assists"},
               {"g": "og: price outside -150..+130"})
    assert CR.og(card, 2) == ("no picks — 2-game slate; 7 candidates: 4 rebounds, 2 under 5.5+ "
                              "assists, 1 outside price range")
    short = _og(["x", "y"], {"a": "filtered: rebounds"}, {"g": "og: outside top 5"})
    assert CR.og(short, 6).startswith("2 picks — 6-game slate; 4 candidates: ")
    assert CR.og(_og(list("abcde")), 9) == ""  # a full card: no line
    assert CR.og(_og([], no_lines=True), 3) == ""  # card_note's "No lines posted in time"
    assert CR.og({**_og([]), "ranking": None}, 3) == ""  # no stored reasons: generic text
    assert CR.og(_og([]), 2) == "no picks — 2-game slate; no OG model picks for today's games"


def test_next_gen_reason_line(tmp_path):
    pdb = engine_for(tmp_path / "p.sqlite")
    ng = {"sport": "nba", "day": "2026-11-06", "selected_at": NOW.isoformat(),
          "source": "engine", "picks": []}
    with pdb.connect() as p:
        assert CR.nextgen(p, ng, 4) == "no picks — no edge found on 4 games"
        assert CR.nextgen(p, ng, None) == ""  # slate unknown: generic text
    with pdb.begin() as p:
        _alert(p, "engine", "e1", "A", market="points")
        _alert(p, "engine", "e2", "B", market="assists")
    ng2 = {**ng, "filtered": {"e1": "filtered: non-assist prop"}}
    with pdb.connect() as p:
        assert CR.nextgen(p, ng2, 4) == ("no picks — 4-game slate; 2 engine picks: 1 non-assist "
                                         "market, 1 below the bar (EV, long-shot gate or no "
                                         "longer Live)")


def test_push_uses_the_reason_only_when_short_one_line_per_card(tmp_path):
    pdb = engine_for(tmp_path / "p.sqlite")
    day = NOW.date()
    with pdb.begin() as p:
        set_state(p, M.STATE.format(day=day.isoformat()), {"slate": 4, "sports": ["nba"]}, NOW)
        set_state(p, CD.key("nba", day), {"sport": "nba", "day": day.isoformat(),
                                          "selected_at": NOW.isoformat(), "source": "engine",
                                          "picks": []}, NOW)
        og = _og([], {"a": "filtered: rebounds"}, {"g": "og: price outside -150..+130"})
        og["day"] = day.isoformat()
        set_state(p, CD.shadow_key("nba", day), og, NOW)
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nba", NOW)
    lines = body.split("\n")
    assert "Today: no picks — 4-game slate; 2 candidates: 1 rebounds, 1 outside price range" \
        in lines
    assert "Today: no picks — no edge found on 4 games" in lines
    assert sum("no picks —" in x for x in lines) == 2  # one line per card
    assert not any("found no value" in x for x in lines)
