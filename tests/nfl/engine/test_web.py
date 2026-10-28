import json

import httpx

from betdata.core.http import SourceClient
from betdata.nfl.engine import web

PAGE = (
    """{{Short description|NFL team season}}
{{Infobox NFL team season
| team = Detroit Lions
| year = 2016
| record = 9–7
| coach = [[Jim Caldwell]]
| off_coach = [[Jim Bob Cooter]]
| def_coach = [[Teryl Austin]] <small>(fired after week 7)</small><br />""" \
    """[[Kris Kocurek|Kris Kocurek]] <small>(interim, week 8)</small>
| stadium = [[Ford Field]]
| uniform = {{NFL uniform|DET}}
}}
The '''2016 Detroit Lions season''' was ...
"""
)

ARTICLE = """{{Infobox NFL biography
| coach_years = {{plainlist|
* 2008–2009
* 2014–{{nflyear|2017}} }}
}}
* [[Detroit Lions]] (2014–2017) Defensive coordinator
* [[Baltimore Ravens]] (2008–2009) Defensive backs coach
"""


def test_team_names_follow_relocations_and_renames():
    assert web.season_title("LV", 2019) == "2019 Oakland Raiders season"
    assert web.season_title("LV", 2020) == "2020 Las Vegas Raiders season"
    assert web.season_title("LAC", 2016) == "2016 San Diego Chargers season"
    assert web.season_title("WAS", 2021) == "2021 Washington Football Team season"
    assert web.season_title("WAS", 2016) == "2016 Washington Redskins season"
    assert web.season_title("WAS", 2023) == "2023 Washington Commanders season"
    assert len(web.TEAMS) == 32


def test_infobox_roles_and_a_mid_season_change():
    row = web.coordinators_row("DET", 2016, {"title": "2016 Detroit Lions season",
                                             "revid": 123, "wikitext": PAGE})
    assert (row["head_coach"], row["oc"], row["dc"]) == ("Jim Caldwell", "Jim Bob Cooter",
                                                         "Teryl Austin")
    assert row["dc_after"] == "Kris Kocurek" and row["dc_week"] == 8
    assert row["oc_after"] is None
    assert row["source"].endswith("oldid=123")


def test_spot_check_matches_team_role_and_years():
    art = {"wikitext": ARTICLE}
    assert web.spot_check(art, "DET", 2016, "dc") == "match"
    assert web.spot_check(art, "DET", 2018, "dc") == "no match"
    assert web.spot_check(art, "DET", 2016, "oc") == "no match"


def test_fetch_coordinators_through_the_client(engine, tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        page = req.url.params["page"]
        seen.append(page)
        if page.endswith("season") and "Detroit" in page:
            return httpx.Response(200, json={"parse": {"title": page, "revid": 1,
                                                       "wikitext": PAGE}})
        if page == "Teryl Austin" or page.startswith("Jim") or page.startswith("Kris"):
            return httpx.Response(200, json={"parse": {"title": page, "revid": 2,
                                                       "wikitext": ARTICLE}})
        return httpx.Response(200, json={"error": {"info": "missing"}})

    c = SourceClient(engine, "wikipedia", "", transport=httpx.MockTransport(handler),
                     sleep=lambda s: None)
    res = web.fetch_coordinators(c, range(2016, 2017), tmp_path, checks=3)
    assert res["rows"] == 1 and len(res["missing"]) == 31
    rows = json.loads((tmp_path / "coordinators_raw.json").read_text())
    assert rows[0]["team"] == "DET" and rows[0]["dc"] == "Teryl Austin"
    checks = json.loads((tmp_path / "coordinators_spotcheck.json").read_text())
    assert len(checks) == 3
    assert {x["result"] for x in checks if x["role"] == "dc"} <= {"match"}


def test_staff_lines_fill_a_role_the_infobox_lacks():
    page = {"title": "2016 Arizona Cardinals season", "revid": 5, "wikitext": (
        "{{Infobox NFL team season\n| coach = [[Bruce Arians]]\n}}\n==Staff==\n")}
    staff = {"revid": 9, "wikitext": (
        "* Offensive coordinator – [[Harold Goodwin]]\n"
        "* Assistant offensive coordinator – [[Somebody Else]]\n"
        "* Defensive coordinator – [[James Bettcher]]\n")}
    row = web.coordinators_row("ARI", 2016, page, staff)
    assert (row["head_coach"], row["oc"], row["dc"]) == ("Bruce Arians", "Harold Goodwin",
                                                         "James Bettcher")
    assert row["oc_from"] == "staff template" and row["staff_revid"] == 9
    assert row["head_coach_from"] == "infobox"


def test_old_franchise_codes_map_to_their_names():
    assert web.team_name("OAK", 2018) == "Oakland Raiders"
    assert web.team_name("SD", 2016) == "San Diego Chargers"
    assert web.team_name("STL", 2015) == "Los Angeles Rams"


def test_prose_lines_never_fill_a_role():
    prose = ("Head coach [[Matt Rhule]] fired defensive coordinator [[Phil Snow]] after "
             "the season.\n* Special teams coordinator – [[John Fassel]]\n"
             "* Outside linebackers – [[Brandon Staley]]")
    assert web.role_from_text(prose, "dc") is None
    assert web.role_from_text(prose, "oc") is None
    staff = ("* '''Defensive coordinator''' – [[Phil Snow]]\n"
             "|Offensive Coordinator=[[Joe Brady]]")
    assert web.role_from_text(staff, "dc")["name"] == "Phil Snow"
    assert web.role_from_text(staff, "oc")["name"] == "Joe Brady"
