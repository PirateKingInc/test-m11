"""M17 A4: the coordinator table and the persistence analysis (MODEL.md "M17" A4).

seeds/coordinators_nfl.csv is built from the Wikipedia fetch (`betdata nfl engine fetch
coordinators`, the `m17-data` branch): one row per team-season 2016-2026 with head coach,
offensive and defensive coordinator, any mid-season successor, where each came from (infobox,
staff list on the page, or the season's staff template) and the source revision. Wikipedia text
is CC BY-SA 4.0; the file's header carries the attribution. A role the page does not state in
its infobox or a staff-list entry is left empty: nothing is filled from memory.

Persistence: does a team's style carry over less when its coordinator changes? Season-to-season
correlation of offensive neutral pass rate and pace (by offensive coordinator) and defensive
pressure rate and EPA per dropback allowed (by defensive coordinator), same vs changed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

HEADER = ("# NFL head coaches and coordinators 2016-2026, from English Wikipedia team-season "
          "articles (infobox or staff list), text licensed CC BY-SA 4.0. Each row's source is "
          "the revision it was read from. Built by betdata (M17).")
COLUMNS = ["season", "team", "head_coach", "head_coach_after", "oc", "oc_after", "oc_week",
           "dc", "dc_after", "dc_week", "oc_from", "dc_from", "title", "revid", "source",
           "staff_revid"]


def build(raw: list[dict], out: Path) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    for c in COLUMNS:
        if c not in df:
            df[c] = None
    df = df[COLUMNS].sort_values(["season", "team"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        f.write(HEADER + "\n")
        df.to_csv(f, index=False, quoting=csv.QUOTE_MINIMAL)
    return df


def load(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, comment="#")


def _r(a, b):
    ok = a.notna() & b.notna()
    return float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() >= 10 else float("nan")


def persistence(coords: pd.DataFrame, team_games: pd.DataFrame) -> dict:
    tg = team_games
    off = tg.groupby(["team", "season"]).agg(
        neutral_pass=("neutral_dropbacks", "sum"), neutral_plays=("neutral_plays", "sum"),
        secs=("neutral_secs", "sum"), gaps=("neutral_gaps", "sum"))  # fmt: skip
    off["neutral_pass_rate"] = off.neutral_pass / off.neutral_plays
    off["pace"] = off.secs / off.gaps
    de = tg.groupby(["opp", "season"]).agg(pr=("pressures", "sum"), n=("pressure_n", "sum"),
                                            epa=("pass_epa", "sum"), db=("dropbacks", "sum"))
    de.index = de.index.set_names(["team", "season"])
    de["pressure_rate"] = de.pr / de.n.replace(0, np.nan)
    de["pass_epa_allowed"] = de.epa / de.db
    c = coords.set_index(["team", "season"])
    out = {}
    for rate, frame, role in (("neutral_pass_rate", off, "oc"), ("pace", off, "oc"),
                              ("pressure_rate", de, "dc"), ("pass_epa_allowed", de, "dc")):
        x = frame[[rate]].join(c[[role, "head_coach"]], how="left").reset_index()
        nxt = x.assign(season=x.season - 1).rename(columns={rate: "next", role: "next_role",
                                                            "head_coach": "next_hc"})
        p = x.merge(nxt, on=["team", "season"])
        known = p[role].notna() & p.next_role.notna()
        same = known & (p[role] == p.next_role) & (p.head_coach == p.next_hc)
        changed = known & ~same
        out[rate] = {"role": role, "pairs_same": int(same.sum()),
                     "pairs_changed": int(changed.sum()),
                     "r_same": round(_r(p[rate][same], p.next[same]), 3),
                     "r_changed": round(_r(p[rate][changed], p.next[changed]), 3),
                     "r_all": round(_r(p[rate], p.next), 3)}  # fmt: skip
        d = out[rate]
        d["used"] = bool(d["pairs_same"] >= 60 and d["pairs_changed"] >= 60
                         and d["r_same"] - d["r_changed"] > 0.1)  # fmt: skip
    return out


def write(raw_path: Path, seed: Path, report: Path) -> dict:
    from betdata.nfl.engine.plays import load as load_tables

    df = build(json.loads(raw_path.read_text()), seed)
    tables = load_tables(range(2016, 2026))
    res = {"rows": len(df), "missing": {r: int(df[r].isna().sum()) for r in
                                        ("head_coach", "oc", "dc")},
           "persistence": persistence(df, tables["team_games"]),
           "persistence_2016_2022": persistence(
               df, tables["team_games"][tables["team_games"].season <= 2022])}  # fmt: skip
    report.write_text(json.dumps(res, indent=1))
    return res
