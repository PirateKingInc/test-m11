"""M17 fetches that only GitHub Actions can make (MODEL.md "M17" Parts A, D and F). Each goes
through `core/http.py` (`SourceClient`, one request a second) into a throwaway database, and
writes plain files that the `nfl-m17-data` workflow publishes on the `m17-data` branch:

  coordinators   Wikipedia team-season infoboxes (head coach, offensive and defensive
                 coordinator), 2016-2026, with each page's revision id (CC BY-SA 4.0), and 40
                 spot-checks against the coaches' own articles
  lines          ESPN's per-book opening and closing NFL lines (the core odds API M14 used for
                 the NBA), per season
  timing         nflverse release asset names and update times (GitHub API)
  fantasy        reachability and samples of Sleeper and ESPN fantasy projections and the
                 FantasyPros rankings files

No parsing here reads a betting line into the engine: the lines file is for Part D only.
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import date
from pathlib import Path
from typing import Any

from betdata.core.errors import ApiError, SourceUnavailable
from betdata.core.http import SourceClient

log = logging.getLogger(__name__)
WIKI = "https://en.wikipedia.org/w/api.php"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
GITHUB_API = "https://api.github.com"
USER_AGENT = ("betdata-m17/0.1 (https://github.com/PirateKingInc/test-m11; research use) "
              "python-httpx")  # Wikimedia user-agent policy: a URL to reach the owner

# nflverse abbreviation -> franchise name by season (relocations and renames since 2016)
TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}  # fmt: skip


def team_name(team: str, season: int) -> str:
    if team == "LAC" and season < 2017:
        return "San Diego Chargers"
    if team == "LV" and season < 2020:
        return "Oakland Raiders"
    if team == "WAS":
        return ("Washington Redskins" if season < 2020 else "Washington Football Team"
                if season < 2022 else "Washington Commanders")  # fmt: skip
    return TEAMS[team]


def season_title(team: str, season: int) -> str:
    return f"{season} {team_name(team, season)} season"


# --- Wikipedia ---------------------------------------------------------------------------------

ROLE_KEYS = {
    "head_coach": re.compile(r"^(head[ _]?)?coach$"),
    "oc": re.compile(r"^(off(ensive)?[ _]?(coach|coordinator)|oc)$"),
    "dc": re.compile(r"^(def(ensive)?[ _]?(coach|coordinator)|dc)$"),
}
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
WEEK = re.compile(r"[Ww]eek\s*(\d{1,2})")


def infobox(wikitext: str) -> dict[str, str]:
    """The first infobox's top-level `| key = value` fields (nested templates kept whole)."""
    start = wikitext.find("{{Infobox")
    if start < 0:
        return {}
    depth, i, end = 0, start, len(wikitext)
    while i < len(wikitext) - 1:
        two = wikitext[i:i + 2]
        if two == "{{":
            depth, i = depth + 1, i + 2
            continue
        if two == "}}":
            depth, i = depth - 1, i + 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    body = wikitext[start + 2:end - 2]
    fields, depth, buf, links = {}, 0, [], 0
    parts: list[str] = []
    for ch_i, ch in enumerate(body):
        nxt = body[ch_i:ch_i + 2]
        if nxt in ("{{",):
            depth += 1
        elif nxt in ("}}",):
            depth -= 1
        elif nxt == "[[":
            links += 1
        elif nxt == "]]":
            links -= 1
        if ch == "|" and depth == 0 and links == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    return fields


def people(value: str) -> list[dict]:
    """Linked names in an infobox value, in order, with any week stated next to each."""
    out = []
    pieces = re.split(r"<br\s*/?>|\n\*|;", value)
    for piece in pieces:
        m = LINK.search(piece)
        if m:
            name = (m.group(2) or m.group(1)).strip()
            article = m.group(1).strip()
        else:
            plain = re.sub(r"\{\{[^}]*\}\}|\([^)]*\)|<[^>]+>|'''?", "", piece).strip(" ,*")
            if not plain:
                continue
            name, article = plain, None
        w = WEEK.search(piece)
        out.append({"name": name, "article": article, "week": int(w.group(1)) if w else None,
                     "note": re.sub(r"\[\[[^\]]*\]\]|<[^>]+>", "", piece).strip()[:120]})
    return out


def coordinators_row(team: str, season: int, page: dict) -> dict:
    box = infobox(page.get("wikitext", ""))
    row: dict[str, Any] = {"season": season, "team": team, "title": page["title"],
                           "revid": page.get("revid"),
                           "source": f"https://en.wikipedia.org/w/index.php?oldid={page.get('revid')}"}  # noqa: E501  # fmt: skip
    for role, pat in ROLE_KEYS.items():
        value = next((v for k, v in box.items() if pat.match(k)), "")
        ps = people(value)
        row[role] = ps[0]["name"] if ps else None
        row[f"{role}_article"] = ps[0]["article"] if ps else None
        row[f"{role}_after"] = " -> ".join(p["name"] for p in ps[1:]) or None
        row[f"{role}_week"] = next((p["week"] for p in ps[1:] if p["week"]), None)
        row[f"{role}_raw"] = value[:300]
    return row


def wiki_page(client: SourceClient, title: str) -> dict | None:
    r = client.get(WIKI, {"action": "parse", "page": title, "prop": "wikitext|revid",
                        "redirects": 1, "format": "json", "formatversion": 2}, est_cost=0,
                   store_raw=False)  # fmt: skip
    data = json.loads(r.body)
    if "error" in data:
        log.warning("wikipedia %s: %s", title, data["error"].get("info"))
        return None
    p = data["parse"]
    return {"title": p["title"], "revid": p.get("revid"), "wikitext": p.get("wikitext", "")}


ROLE_WORDS = {"head_coach": "head coach", "oc": "offensive coordinator",
              "dc": "defensive coordinator"}  # fmt: skip


def spot_check(article: dict, team: str, season: int, role: str) -> str:
    """'match' when one line of the coach's article names the team (nickname), the role and a
    year span covering the season; 'no match' otherwise (checked by hand)."""
    nick = team_name(team, season).split()[-1]
    word = ROLE_WORDS[role]
    for line in article.get("wikitext", "").splitlines():
        low = line.lower()
        if nick.lower() not in low or word not in low.replace("_", " "):
            continue
        years = [int(y) for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", line)]
        present = "present" in low
        if years and (min(years) <= season <= max(years) or (present and min(years) <= season)):
            return "match"
    return "no match"


def fetch_coordinators(client: SourceClient, seasons: range, out: Path, checks: int = 40,
                       seed: int = 17) -> dict:  # fmt: skip
    rows, missing = [], []
    for season in seasons:
        for team in sorted(TEAMS):
            title = season_title(team, season)
            try:
                page = wiki_page(client, title)
            except (ApiError, SourceUnavailable) as e:
                log.warning("wikipedia %s: %s", title, e)
                page = None
            if page is None:
                missing.append(title)
                continue
            rows.append(coordinators_row(team, season, page))
    out.mkdir(parents=True, exist_ok=True)
    (out / "coordinators_raw.json").write_text(json.dumps(rows, indent=1))
    rng = random.Random(seed)
    pool = [(r, role) for r in rows for role in ROLE_WORDS if r.get(f"{role}_article")]
    results = []
    for r, role in rng.sample(pool, min(checks, len(pool))):
        try:
            art = wiki_page(client, r[f"{role}_article"]) or {}
        except (ApiError, SourceUnavailable):
            art = {}
        results.append({"season": r["season"], "team": r["team"], "role": role,
                        "name": r[role], "article": art.get("title"), "revid": art.get("revid"),
                        "result": spot_check(art, r["team"], r["season"], role) if art
                        else "article missing"})  # fmt: skip
    (out / "coordinators_spotcheck.json").write_text(json.dumps(results, indent=1))
    return {"rows": len(rows), "missing": missing,
            "spot_checks": {k: sum(x["result"] == k for x in results)
                            for k in ("match", "no match", "article missing")}}  # fmt: skip


# --- ESPN lines --------------------------------------------------------------------------------


def season_events(client: SourceClient, season: int) -> list[dict]:
    """Every regular-season and playoff game ESPN lists for the season: id, date, teams."""
    out: dict[str, dict] = {}
    for stype, weeks in ((2, range(1, 19)), (3, range(1, 6))):
        for week in weeks:
            try:
                r = client.get(f"{ESPN_SITE}/scoreboard", {"dates": season, "seasontype": stype,
                                               "week": week, "limit": 100}, est_cost=0,
                               store_raw=False)  # fmt: skip
            except (ApiError, SourceUnavailable) as e:
                log.warning("espn scoreboard %s %s/%s: %s", season, stype, week, e)
                continue
            for e in json.loads(r.body).get("events", []):
                comp = (e.get("competitions") or [{}])[0]
                teams = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation")
                         for c in comp.get("competitors", [])}  # fmt: skip
                out[str(e["id"])] = {"espn_id": str(e["id"]), "date": e.get("date"),
                                     "season_type": stype, "week": week,
                                     "home": teams.get("home"), "away": teams.get("away")}
    return list(out.values())


def fetch_lines(client: SourceClient, season: int, out: Path) -> dict:
    from betdata.sources.espn_nba import core_odds

    core = client
    events = season_events(client, season)
    games, empty = [], 0
    for ev in events:
        eid = ev["espn_id"]
        try:
            r = core.get(f"{ESPN_CORE}/events/{eid}/competitions/{eid}/odds", {"limit": 50},
                         est_cost=0, store_raw=False, not_found_ok=True)  # fmt: skip
            books = [] if r.status == 404 else core_odds(eid, json.loads(r.body))["books"]
        except (ApiError, ValueError) as e:
            log.warning("espn odds %s: %s", eid, e)
            books = []
        empty += not books
        games.append({**ev, "books": books})
    out.mkdir(parents=True, exist_ok=True)
    (out / f"nfl_lines_{season}.json").write_text(json.dumps(games))
    return {"season": season, "games": len(games), "without_lines": empty}


# --- nflverse timing ---------------------------------------------------------------------------

TIMING_TAGS = ("pbp", "pbp_participation", "ftn_charting", "pfr_advstats", "nextgen_stats",
               "stats_player", "depth_charts", "weekly_rosters", "injuries", "snap_counts",
               "schedules")  # fmt: skip


def fetch_timing(client: SourceClient, out: Path) -> dict:
    res = {}
    for tag in TIMING_TAGS:
        try:
            r = client.get(f"{GITHUB_API}/repos/nflverse/nflverse-data/releases/tags/{tag}", None,
                           est_cost=0, store_raw=False)  # fmt: skip
            assets = json.loads(r.body).get("assets", [])
            res[tag] = sorted(({"name": a["name"], "updated_at": a["updated_at"],
                                "created_at": a["created_at"], "size": a["size"]}
                               for a in assets if re.search(r"20(2[4-6])", a["name"])
                               and a["name"].endswith((".parquet", ".csv"))),
                              key=lambda a: a["name"])  # fmt: skip
        except (ApiError, SourceUnavailable) as e:
            res[tag] = {"error": str(e)[:200]}
    out.mkdir(parents=True, exist_ok=True)
    (out / "nflverse_timing.json").write_text(json.dumps(res, indent=1))
    return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}


# --- fantasy -----------------------------------------------------------------------------------

FANTASY_PROBES = (
    ("sleeper_state", "https://api.sleeper.app/v1/state/nfl", None, None),
    ("sleeper_proj_2021_w5", "https://api.sleeper.app/projections/nfl/2021/5",
     {"season_type": "regular", "position[]": ["WR", "RB", "TE", "QB"]}, None),
    ("sleeper_proj_2026_w8", "https://api.sleeper.app/projections/nfl/2026/8",
     {"season_type": "regular", "position[]": ["WR", "RB", "TE", "QB"]}, None),
    ("sleeper_proj_2016_w5", "https://api.sleeper.app/projections/nfl/2016/5",
     {"season_type": "regular", "position[]": ["WR"]}, None),
    ("espn_fantasy_2021_w5",
     "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2021/segments/0/"
     "leaguedefaults/3", {"scoringPeriodId": 5, "view": "kona_player_info"},
     {"X-Fantasy-Filter": json.dumps({"players": {"limit": 50, "sortPercOwned":
                                                  {"sortPriority": 1, "sortAsc": False}}})}),
    ("espn_fantasy_2026_w8",
     "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/"
     "leaguedefaults/3", {"scoringPeriodId": 8, "view": "kona_player_info"},
     {"X-Fantasy-Filter": json.dumps({"players": {"limit": 50, "sortPercOwned":
                                                  {"sortPriority": 1, "sortAsc": False}}})}),
    ("ff_rankings_db_fpecr", "https://github.com/dynastyprocess/data/raw/master/files/"
     "db_fpecr.parquet", None, None),
    ("ff_rankings_weekly", "https://github.com/dynastyprocess/data/raw/master/files/"
     "fp_latest_weekly.csv", None, None),
    ("ff_opportunity_2024", "https://github.com/ffverse/ffopportunity/releases/download/"
     "latest-data/ep_weekly_2024.parquet", None, None),
    ("ff_opportunity_2026", "https://github.com/ffverse/ffopportunity/releases/download/"
     "latest-data/ep_weekly_2026.parquet", None, None),
    ("sleeper_terms", "https://docs.sleeper.com/", None, None),
    ("espn_terms", "https://disneytermsofuse.com/english/", None, None),
)  # fmt: skip


def fetch_fantasy(make_client, out: Path) -> dict:
    """Status, size and a short sample of each probe; the full bodies of the small JSON ones
    (projections) and the terms pages' text, for the record."""
    res = {}
    out.mkdir(parents=True, exist_ok=True)
    for name, url, params, headers in FANTASY_PROBES:
        client = make_client(name, headers)  # own breaker per probe
        try:
            r = client.get(url, params, est_cost=0, store_raw=False)
            body = r.body
            res[name] = {"status": r.status, "bytes": len(body),
                         "content_type": r.headers.get("content-type"),
                         "last_modified": r.headers.get("last-modified")}
            ext = ("json" if "json" in (r.headers.get("content-type") or "") else
                   "parquet" if url.endswith(".parquet") else
                   "csv" if url.endswith(".csv") else "html")  # fmt: skip
            if len(body) < 40_000_000:
                (out / f"{name}.{ext}").write_bytes(body)
        except ApiError as e:
            res[name] = {"status": e.status, "error": str(e)[:200]}
        except SourceUnavailable as e:
            res[name] = {"error": str(e)[:200]}
        except Exception as e:  # noqa: BLE001 - a probe never stops the others
            res[name] = {"error": f"{type(e).__name__}: {e}"[:200]}
        finally:
            client.close()
    (out / "fantasy_probes.json").write_text(json.dumps(res, indent=1))
    return res


def today() -> date:
    return date.today()
