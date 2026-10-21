"""ESPN athlete id -> nflverse gsis id, for prop lines (ESPN ids) and our panel (gsis ids).

Source: nflverse depth charts from 2025 (daily ESPN-based snapshots carry both `espn_id` and
`gsis_id`; `betdata backfill --source nflverse_extra --dataset depth_charts`). A player missing
there is matched by name: a unique normalised name among players with a stat line in the
seasons given (nflverse weekly player stats). Unmatched players get no prediction.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

FIRST_ESPN_DEPTH_SEASON = 2025
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = SUFFIX.sub("", re.sub(r"[^a-z ]", "", s.replace("-", " ")))
    return " ".join(s.split())


def depth_ids(data_dir: Path, seasons) -> dict[str, str]:
    """The map from the depth-chart files on disk, merged into a cache under `derived/` (the
    hourly runner keeps that directory, not the nflverse files, which arrive once a day)."""
    cache = Path(data_dir) / "derived" / "nfl_espn_gsis.json"
    out: dict[str, str] = json.loads(cache.read_text()) if cache.exists() else {}
    new = _from_files(data_dir, seasons)
    if new and any(out.get(k) != v for k, v in new.items()):
        out.update(new)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, sort_keys=True))
    return out


def _from_files(data_dir: Path, seasons) -> dict[str, str]:
    root = Path(data_dir) / "nflverse" / "depth_charts"
    out: dict[str, str] = {}
    for s in sorted(seasons):
        p = root / f"depth_charts_{s}.parquet"
        if s < FIRST_ESPN_DEPTH_SEASON or not p.exists():
            continue
        d = pd.read_parquet(p, columns=["dt", "espn_id", "gsis_id"]).dropna()
        d = d.sort_values("dt")
        out.update(zip(d.espn_id.astype(str).str.replace(r"\.0$", "", regex=True),
                       d.gsis_id.astype(str), strict=True))  # fmt: skip
    return out


def name_ids(stats: pd.DataFrame) -> dict[str, str]:
    """Normalised name -> gsis id, for names that are unique (stats: player_id, name)."""
    d = stats[["player_id", "name"]].dropna().drop_duplicates()
    d = d.assign(k=d.name.map(norm_name))
    n = d.groupby("k").player_id.nunique()
    d = d[d.k.map(n) == 1]
    return dict(zip(d.k, d.player_id, strict=True))


def espn_to_gsis(lines: pd.DataFrame, data_dir: Path, seasons, stats: pd.DataFrame,
                 ) -> pd.Series:  # fmt: skip
    """gsis id per row of `lines` (player_id = ESPN id, player_name); None where unmatched."""
    by_id = depth_ids(data_dir, seasons)
    by_name = name_ids(stats)
    out = [by_id.get(str(e)) or by_name.get(norm_name(n)) if n is not None else by_id.get(str(e))
           for e, n in zip(lines.player_id, lines.player_name, strict=True)]  # fmt: skip
    return pd.Series(out, index=lines.index, dtype=object)
