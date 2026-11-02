"""M19 review fields and reason tags (MODEL.md "M19: Why each pick won or lost").

Two write-once ledger files, written by `sync` (the hourly ledger sync, which has the poller and
the main databases):
- `ledger/review_pick.csv` at the first sync after a pick is created: team and opponent, the
  projected median and minutes (NBA props: the alert's own numbers, else nbap15 re-run as of the
  pick time), the line, and the injury-report statuses the model assumed for the player and his
  key teammates (game markets: the key players of both teams);
- `ledger/review_result.csv` once the pick is final: actual stat and minutes (or the final score),
  status changes between pick time and tip, closing line, CLV, and the reason tag.

Reason tag, the first that applies: News after pick > Minutes miss > Market disagreed > Variance;
"not enough data" when a rule cannot be checked and no earlier rule applied. Voids have no tag.
Nothing here changes a pick, a result or a grade.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

log = logging.getLogger(__name__)
KEY_MINUTES = 24.0  # NBA key teammate: average minutes over his last 10 games
LAST_N = 10
NFL_KEY_PCT = 0.70  # NFL key player: offensive snap share in the team's previous game
MINUTES_PCT, MINUTES_ABS = 0.25, 8.0
SNAP_PTS = 25.0
NEWS, MINUTES, MARKET, VARIANCE, NED = ("News after pick", "Minutes miss", "Market disagreed",
                                        "Variance", "not enough data")  # fmt: skip
TAGS = (NEWS, MINUTES, MARKET, VARIANCE, NED)
PICK_FIELDS = ("pick_id", "team", "opponent", "proj_median", "proj_minutes", "proj_snap_share",
               "line", "statuses", "captured_at", "source")  # fmt: skip
RESULT_FIELDS = ("pick_id", "actual_stat", "actual_minutes", "actual_snap_share", "final_score",
                 "status_changes", "close_line", "clv", "tag", "reviewed_at")  # fmt: skip
STATUS = {"out": "Out", "doubtful": "Doubtful", "questionable": "Questionable",
          "probable": "Probable", "available": "Available", "removed": "Available",
          "active": "Available"}  # fmt: skip


def _norm(name: str | None) -> str:
    from betdata.alerts.grade import _norm as n

    return n(name or "")


def _report_name(name: str) -> str:
    """The NBA report's "Last, First" as "First Last"."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return name


def _utc(t):
    from betdata.alerts.grade import utc

    return None if t is None else utc(t)


def _f(x):
    return None if x in ("", None) else float(x)


def _num(x, nd=2) -> str:
    return "" if x is None else str(round(float(x), nd))


# --- teams --------------------------------------------------------------------------------------


def abbrevs(m, sport: str) -> dict[int, str]:
    from betdata.db.schema import sports, teams

    return {tid: (ab or "") for tid, ab in m.execute(
        select(teams.c.team_id, teams.c.abbrev).join(sports, sports.c.sport_id == teams.c.sport_id)
        .where(sports.c.key == sport))}  # fmt: skip


def game_teams(label: str) -> tuple[str, str]:
    away, home = (label or "? @ ?").split(" @ ")
    return away.strip(), home.strip()


def nba_player_team(m, player: str, game_id: int, before: datetime) -> int | None:
    """The player's team: his row in this game's box score, else his latest game before."""
    from betdata.db.schema import games
    from betdata.db.schema import nba_player_games as npg

    want = _norm(player)
    rows = m.execute(select(npg.c.player_name, npg.c.team_id, npg.c.game_id,
                            games.c.start_time_utc)
                     .join(games, games.c.game_id == npg.c.game_id)
                     .where((npg.c.game_id == game_id) | (games.c.start_time_utc < before))
                     .order_by(games.c.start_time_utc.desc())).all()  # fmt: skip
    mine = [r for r in rows if _norm(r.player_name) == want]
    for r in mine:
        if r.game_id == game_id:
            return r.team_id
    return mine[0].team_id if mine else None


def teams_for(m, pick: dict, details: dict) -> tuple[str, str]:
    """(team, opponent) abbreviations: the player's team for props, the picked side for game
    markets ("" for totals)."""
    away, home = game_teams(pick["game"])
    if pick["player"]:
        tid = details.get("team_id")
        if tid is None and m is not None and pick["sport"] == "nba":
            tid = nba_player_team(m, pick["player"], int(pick["game_id"]),
                                  datetime.fromisoformat(pick["created_at"]))
        ab = ("" if tid is None or m is None else
              abbrevs(m, pick["sport"]).get(int(tid), ""))
        if ab in (away, home):
            return ab, home if ab == away else away
        return ab, ""
    if pick["market"] == "totals":
        return "", ""
    return (home, away) if pick["side"] == "home" else (away, home)


# --- key players and statuses -------------------------------------------------------------------


def nba_key_players(m, team_id: int, before: datetime) -> list[str]:
    """Teammates averaging >= 24.0 minutes over their last 10 games before `before`."""
    from betdata.db.schema import games
    from betdata.db.schema import nba_player_games as npg

    rows = m.execute(select(npg.c.player_name, npg.c.minutes, games.c.start_time_utc)
                     .join(games, games.c.game_id == npg.c.game_id)
                     .where(npg.c.team_id == team_id, games.c.start_time_utc < before)
                     .order_by(games.c.start_time_utc.desc())).all()  # fmt: skip
    by: dict[str, list[float]] = {}
    for r in rows:
        mins = by.setdefault(r.player_name, [])
        if len(mins) < LAST_N:
            mins.append(r.minutes or 0.0)
    return sorted(n for n, v in by.items() if v and sum(v) / len(v) >= KEY_MINUTES)


def nba_statuses(m, game_id: int, names: list[str], at: datetime) -> dict[str, str] | None:
    """Each player's latest NBA injury-report status for the game known at `at` (not listed:
    Available); None when no report for the game was known yet."""
    from betdata.db.schema import nba_injury_reports as r

    rows = m.execute(select(r.c.player_name, r.c.status, r.c.report_at)
                     .where(r.c.game_id == game_id, r.c.known_at <= at)
                     .order_by(r.c.report_at)).all()  # fmt: skip
    if not rows:
        return None
    latest = {}
    for row in rows:
        latest[_norm(_report_name(row.player_name))] = STATUS.get(
            (row.status or "").strip().lower(), row.status)
    return {n: latest.get(_norm(n), "Available") for n in names}


def nfl_key_players(m, team_id: int, before: datetime) -> list[str]:
    """The quarterback with the most snaps in the team's previous game, and every player with
    >= 70% of the offensive snaps in it."""
    from betdata.db.schema import games
    from betdata.db.schema import player_game_snaps as s

    last = m.execute(select(s.c.game_id).join(games, games.c.game_id == s.c.game_id)
                     .where(s.c.team_id == team_id, games.c.start_time_utc < before)
                     .order_by(games.c.start_time_utc.desc()).limit(1)).scalar()  # fmt: skip
    if last is None:
        return []
    rows = m.execute(select(s.c.player_name, s.c.position, s.c.offense_snaps, s.c.offense_pct)
                     .where(s.c.game_id == last, s.c.team_id == team_id)).all()  # fmt: skip
    out = set()
    qbs = [r for r in rows if (r.position or "") == "QB"]
    if qbs:
        out.add(max(qbs, key=lambda r: r.offense_snaps or 0).player_name)
    for r in rows:
        pct = r.offense_pct or 0.0
        if (pct / 100 if pct > 1 else pct) >= NFL_KEY_PCT:
            out.add(r.player_name)
    return sorted(out)


def nfl_statuses(m, team_ids: list[int], names: list[str], tip: datetime,
                 at: datetime) -> dict[str, str] | None:  # fmt: skip
    """Each player's latest NFL injury-report game status known at `at` for this week (not
    listed: Available); None when no report for these teams was known yet."""
    from betdata.db.schema import injuries as i

    rows = m.execute(select(i.c.player_name, i.c.report_status, i.c.fetched_at)
                     .where(i.c.team_id.in_(team_ids), i.c.fetched_at <= at,
                            i.c.report_date >= tip - timedelta(days=8))
                     .order_by(i.c.fetched_at)).all()  # fmt: skip
    if not rows:
        return None
    latest = {_norm(r.player_name): STATUS.get((r.report_status or "").strip().lower(),
                                               r.report_status or "Available") for r in rows}
    return {n: latest.get(_norm(n), "Available") for n in names}


def watched(m, pick: dict, team_ids: dict[str, int]) -> list[str]:
    """The player and his key teammates, or (game markets) the key players of both teams."""
    at = datetime.fromisoformat(pick["created_at"])
    key = nba_key_players if pick["sport"] == "nba" else nfl_key_players
    if pick["player"]:
        tid = team_ids.get("team")
        mates = [n for n in key(m, tid, at) if _norm(n) != _norm(pick["player"])] if tid else []
        return [pick["player"], *mates]
    out = []
    for tid in (team_ids.get("home"), team_ids.get("away")):
        if tid:
            out += key(m, tid, at)
    return out


def statuses(m, pick: dict, names: list[str], team_ids: dict, at: datetime):
    if pick["sport"] == "nba":
        return nba_statuses(m, int(pick["game_id"]), names, at)
    tip = datetime.fromisoformat(pick["tip"])
    return nfl_statuses(m, [t for t in team_ids.values() if t], names, tip, at)


def _team_ids(m, pick: dict, details: dict) -> dict[str, int]:
    from betdata.db.schema import games

    g = m.execute(select(games.c.home_team_id, games.c.away_team_id)
                  .where(games.c.game_id == int(pick["game_id"]))).first()  # fmt: skip
    out = {} if g is None else {"home": g.home_team_id, "away": g.away_team_id}
    if pick["player"]:
        tid = details.get("team_id")
        if tid is None and pick["sport"] == "nba":
            tid = nba_player_team(m, pick["player"], int(pick["game_id"]),
                                  datetime.fromisoformat(pick["created_at"]))
        if tid is not None:
            out["team"] = int(tid)
    return out


# --- projections --------------------------------------------------------------------------------


_PANELS: dict = {}


def rebuild_projection(m, pick: dict) -> dict | None:
    """nbap15 re-run as of the pick time for an NBA prop whose alert kept no minutes: median,
    minutes, team. The panel marks the game scheduled so the model prices it as it would have."""
    import pandas as pd

    from betdata.core.matching import season_for
    from betdata.nba.props.live import PropDesign, predict
    from betdata.nba.props.panel import Panel
    from betdata.nfl.props.ids import norm_name
    from betdata.props.sharp_import import nba_ids

    at = datetime.fromisoformat(pick["created_at"])
    pid = nba_ids(m).get(norm_name(pick["player"]))
    if pid is None:
        return None
    season = season_for("nba", at) - 1
    if season not in _PANELS:  # loaded once per sync, not once per pick
        _PANELS[season] = Panel.load(m, season)
    panel = _PANELS[season]
    gid = int(pick["game_id"])
    if gid not in panel.g.index:
        return None
    status = panel.g.at[gid, "status"]
    panel.g.loc[gid, "status"] = "scheduled"  # restored below: the cached panel stays as is
    over = int(pick["odds"]) if pick["side"] == "over" else None
    under = int(pick["odds"]) if pick["side"] == "under" else None
    df = pd.DataFrame([dict(game_id=gid, player_name=pick["player"], market=pick["market"],
                            line=float(pick["line"]), over_price=over, under_price=under,
                            book=pick["book"], book_id=0, fetched_at=pd.Timestamp(at),
                            source="review", player_id=pid)])  # fmt: skip
    try:
        preds = predict(m, at, panel, PropDesign.load(), lines=df)
    finally:
        panel.g.loc[gid, "status"] = status
    if not preds:
        return None
    d = preds[0]
    return {"median": (d.get("dist") or {}).get("median"),
            "minutes": (d.get("dist") or {}).get("minutes"), "team_id": d.get("team_id")}


def pick_review(m, pick: dict, details: dict, now: datetime, rebuild: bool = False) -> dict:
    """The pick-time review row (MODEL.md "M19")."""
    src = "alert"
    med = details.get("median", details.get("mean_after"))
    mins = details.get("minutes")
    if pick["player"] and pick["sport"] == "nba" and mins is None and rebuild:
        try:
            r = rebuild_projection(m, pick)
        except Exception as e:  # noqa: BLE001 - "not enough data", never a failed sync
            log.warning("projection rebuild %s failed: %s", pick["pick_id"], e)
            r = None
        if r:
            med = med if med is not None else r["median"]
            mins = r["minutes"]
            details = {**details, "team_id": details.get("team_id", r["team_id"])}
            src = "as-of rebuild"
    team, opp = teams_for(m, pick, details)
    ids = _team_ids(m, pick, details)
    names = watched(m, pick, ids)
    st = statuses(m, pick, names, ids, datetime.fromisoformat(pick["created_at"]))
    snap = None
    if pick["player"] and pick["sport"] == "nfl" and ids.get("team"):
        snap = nfl_snap_share(m, pick["player"], ids["team"],
                              datetime.fromisoformat(pick["created_at"]))
    return {"pick_id": pick["pick_id"], "team": team, "opponent": opp,
            "proj_median": _num(med), "proj_minutes": _num(mins),
            "proj_snap_share": _num(snap), "line": pick["line"],
            "statuses": "" if st is None else json.dumps(st, sort_keys=True),
            "captured_at": now.isoformat(timespec="seconds"), "source": src}  # fmt: skip


def nfl_snap_share(m, player: str, team_id: int, before: datetime,
                   game_id: int | None = None) -> float | None:  # fmt: skip
    """Offensive snap share in % (game_id: that game; else the mean of the previous 3)."""
    from betdata.db.schema import games
    from betdata.db.schema import player_game_snaps as s

    q = (select(s.c.player_name, s.c.offense_pct).join(games, games.c.game_id == s.c.game_id)
         .where(s.c.team_id == team_id))
    q = (q.where(s.c.game_id == game_id) if game_id is not None else
         q.where(games.c.start_time_utc < before).order_by(games.c.start_time_utc.desc()))
    vals = [r.offense_pct for r in m.execute(q) if _norm(r.player_name) == _norm(player)]
    vals = [(v * 100 if v <= 1 else v) for v in vals if v is not None][: 1 if game_id else 3]
    return sum(vals) / len(vals) if vals else None


# --- results and the tag ------------------------------------------------------------------------


def tag(*, result: str, player_pick: bool, news: bool | None, proj_minutes: float | None,
        actual_minutes: float | None, proj_snap: float | None, actual_snap: float | None,
        nfl: bool, clv: float | None) -> str:  # fmt: skip
    """The primary reason (MODEL.md "M19"): the first rule that applies, in order; "not enough
    data" when a rule cannot be checked and no earlier rule applied. Voids: none."""
    if result == "void":
        return ""
    if news is None:
        return NED
    if news:
        return NEWS
    if player_pick:
        if nfl:
            if proj_snap is None or actual_snap is None:
                return NED
            if abs(actual_snap - proj_snap) >= SNAP_PTS:
                return MINUTES
        else:
            if proj_minutes is None or actual_minutes is None:
                return NED
            diff = abs(actual_minutes - proj_minutes)
            if diff >= MINUTES_ABS or (proj_minutes > 0 and diff >= MINUTES_PCT * proj_minutes):
                return MINUTES
    if clv is None:
        return NED
    return MARKET if clv < 0 else VARIANCE


def result_review(m, p, pick: dict, rv: dict, now: datetime) -> dict | None:
    """The result review row once the pick is final (None while it is not)."""
    from betdata.alerts.db import alert_grades, alerts
    from betdata.alerts.grade import NBA_STAT
    from betdata.db.schema import nba_player_games as npg
    from betdata.db.schema import results

    res = pick.get("result") or ""
    if res not in ("win", "loss", "push", "void"):
        return None
    gid = int(pick["game_id"])
    tip = datetime.fromisoformat(pick["tip"])
    actual = mins = snap = None
    score = ""
    fin = m.execute(select(results.c.home_score, results.c.away_score)
                    .where(results.c.game_id == gid, results.c.final.is_(True))
                    .order_by(results.c.source)).first()  # fmt: skip
    away, home = game_teams(pick["game"])
    if fin is not None and fin.home_score is not None:
        score = f"{away} {fin.away_score}-{fin.home_score} {home}"
    if pick["player"] and pick["sport"] == "nba":
        row = next((r for r in m.execute(select(npg).where(npg.c.game_id == gid)).mappings()
                    if _norm(r["player_name"]) == _norm(pick["player"])), None)  # fmt: skip
        if row is not None:
            mins = row["minutes"] or 0.0
            cols = NBA_STAT.get(pick["market"])
            actual = sum(row[c] or 0 for c in cols) if cols else None
    elif pick["player"] and pick["sport"] == "nfl":
        ids = _team_ids(m, pick, {})
        if ids.get("team"):
            snap = nfl_snap_share(m, pick["player"], ids["team"], tip, gid)
    elif fin is not None and fin.home_score is not None:
        if pick["market"] == "totals":
            actual = fin.home_score + fin.away_score
        else:
            sgn = 1 if pick["side"] == "home" else -1
            actual = sgn * (fin.home_score - fin.away_score)
    # status changes between pick time and tip, for the players watched at pick time
    before = json.loads(rv["statuses"]) if rv.get("statuses") else None
    news = None
    changes: list = []
    if before is not None:
        ids = _team_ids(m, pick, {})
        after = statuses(m, pick, list(before), ids, tip)
        if after is not None:
            changes = [{"player": n, "from": before[n], "to": after.get(n, before[n])}
                       for n in before if after.get(n, before[n]) != before[n]]
            news = bool(changes)
    close = None
    if p is not None:
        g = p.execute(select(alert_grades.c.close_line)
                      .join(alerts, alerts.c.alert_id == alert_grades.c.alert_id)
                      .where(alerts.c.dedup_key == pick["dedup_key"])).first()  # fmt: skip
        close = None if g is None else g.close_line
    clv = _f(pick.get("clv_consensus"))
    t = tag(result=res, player_pick=bool(pick["player"]), news=news,
            proj_minutes=_f(rv.get("proj_minutes")), actual_minutes=mins,
            proj_snap=_f(rv.get("proj_snap_share")), actual_snap=snap,
            nfl=pick["sport"] == "nfl", clv=clv)  # fmt: skip
    return {"pick_id": pick["pick_id"], "actual_stat": _num(actual), "actual_minutes": _num(mins),
            "actual_snap_share": _num(snap), "final_score": score,
            "status_changes": json.dumps(changes) if before is not None and news is not None
            else "", "close_line": _num(close), "clv": "" if clv is None else _num(100 * clv),
            "tag": t, "reviewed_at": now.isoformat(timespec="seconds")}  # fmt: skip


# --- the sync -----------------------------------------------------------------------------------


def sync(p, m, out: Path, now: datetime) -> dict:
    """Write the review rows due (each once). Official picks without the alert's minutes get the
    as-of projection rebuild; a failure only leaves "not enough data"."""
    from betdata.alerts.db import alerts
    from betdata.ledger import append_rows, joined, read_rows

    if m is None:
        return {"pick": 0, "result": 0}
    have_p = {r["pick_id"]: r for r in read_rows(out / "review_pick.csv")}
    have_r = {r["pick_id"] for r in read_rows(out / "review_result.csv")}
    det = {}
    if p is not None:
        det = {r.dedup_key: (r.details or {}) for r in p.execute(
            select(alerts.c.dedup_key, alerts.c.details))}  # fmt: skip
    new_p, new_r = [], []
    for pick in joined(out):
        if pick.get("signal") == "news" or pick.get("duplicate") == "true":
            continue  # M16 legacy picks are not reviewed
        d = det.get(pick["dedup_key"], {})
        d = json.loads(d) if isinstance(d, str) else d
        rv = have_p.get(pick["pick_id"])
        if rv is None:
            try:
                rv = pick_review(m, pick, d, now, rebuild=pick.get("card") == "yes")
            except Exception as e:  # noqa: BLE001 - one pick never stops the sync
                log.warning("review pick %s failed: %s", pick["pick_id"], e)
                continue
            new_p.append(rv)
        if pick["pick_id"] in have_r:
            continue
        try:
            rr = result_review(m, p, pick, rv, now)
        except Exception as e:  # noqa: BLE001
            log.warning("review result %s failed: %s", pick["pick_id"], e)
            continue
        if rr is not None:
            new_r.append(rr)
    n_p = append_rows(out / "review_pick.csv", PICK_FIELDS, new_p)
    n_r = append_rows(out / "review_result.csv", RESULT_FIELDS, new_r)
    return {"pick": n_p, "result": n_r}
