"""`betdata nfl ...`: M12 data maintenance and checks."""

from __future__ import annotations

import typer

nfl_app = typer.Typer(help="NFL model (M12): as-of data, checks.", no_args_is_help=True)


def _engine():
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    return make_engine(get_settings().db_url)


@nfl_app.command("backfill-report-dates")
def backfill_report_dates():
    """Fill injury publish times (nflverse date_modified) on rows loaded before they were
    parsed, from the archived raw files. No network call; safe to repeat."""
    from betdata.sources.nflverse_data import fill_report_dates

    with _engine().begin() as conn:
        updated, files = fill_report_dates(conn)
    typer.echo(f"read {files} archived injury files; filled report_date on {updated} rows")


def _data_dir():
    import os
    from pathlib import Path

    from betdata.config import PROJECT_ROOT

    return Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")


@nfl_app.command("derive")
def derive(
    seasons: str = typer.Option(None, help="pbp seasons to rebuild, e.g. 2025 (default: all)"),
):
    """Rebuild derived tables from files already stored: game info from the archived
    schedule, play-by-play aggregates from data/pbp. No network call."""
    from betdata.nfl.derive import derive_game_info, derive_pbp
    from betdata.sources.nflverse import parse_seasons

    years = sorted(parse_seasons(seasons) or []) if seasons else None
    with _engine().begin() as conn:
        n_info = derive_game_info(conn)
        n_pbp = derive_pbp(conn, _data_dir(), years)
    typer.echo(f"game info: {n_info} games; play-by-play: {n_pbp} team/player rows")


def _groups(names: str | None):
    from betdata.nfl.groups import GROUPS, resolve

    return resolve(names) if names else list(GROUPS.values())


@nfl_app.command("asof-check")
def asof_check(
    sample: int = typer.Option(40, help="Games to check (spread over seasons)."),
    groups: str = typer.Option(None, help="Comma-separated groups (default: all)."),
    seed: int = typer.Option(0),
):
    """Rebuild features as of each prediction point from (a) all data, (b) only rows known by
    then, (c) all data with later rows scrambled; fail if any value differs."""
    import random

    from betdata.nfl.asof import check
    from betdata.nfl.data import load_store

    with _engine().connect() as conn:
        store = load_store(conn)
    ids = [i for i in store.games.index if store.games.at[i, "season"] >= 2013]
    rng = random.Random(seed)
    picked = sorted(rng.sample(ids, min(sample, len(ids))))
    bad = check(store, _groups(groups), picked, seed=seed)
    for v in bad[:20]:
        typer.echo(f"LOOK-AHEAD game {v.game_id} {v.point} ({v.variant}): {', '.join(v.features)}")
    typer.echo(f"checked {len(picked)} games x 2 points: {len(bad)} violations")
    if bad:
        raise typer.Exit(1)


def feature_rows(store, group, game_ids, refresh: bool = False):
    """One group's features for these games at both points, cached under data/derived."""
    import hashlib

    import pandas as pd

    from betdata.nfl.pipeline import build

    tag = hashlib.sha1(",".join(map(str, sorted(game_ids))).encode()).hexdigest()[:10]
    version = getattr(group, "version", 1)
    path = _data_dir() / "derived" / f"features_{group.name}_{version}_{tag}.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    rows = build(store, [group], game_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(path)
    return rows


def closing_lines(conn):
    """game_id -> (home spread, total) at the close. For scoring only, never a feature."""
    from betdata.analysis.dataset import load_games

    return {g.game_id: (g.spread_home, g.total) for g in load_games(conn, "nfl")}


def close_mae(close, out, game_ids):
    import numpy as np

    m = [abs(-close[i][0] - out.at[i, "margin"]) for i in game_ids
         if i in out.index and close.get(i, (None,))[0] is not None]  # fmt: skip
    t = [abs(close[i][1] - out.at[i, "total"]) for i in game_ids
         if i in out.index and close.get(i, (None, None))[1] is not None]  # fmt: skip
    return float(np.mean(m)), float(np.mean(t))


def model_games(store, seasons):
    g = store.games
    return [i for i in g.index if g.at[i, "season"] in set(seasons)
            and g.at[i, "season_type"] in ("REG", "POST")]  # fmt: skip


@nfl_app.command("evaluate")
def evaluate_cmd(
    groups: str = typer.Option(None, help="Groups to add in order (default: all built)."),
    refresh: bool = typer.Option(False, help="Recompute cached feature rows."),
):
    """Add feature groups one at a time on 2013-2019 (fit) / 2020-2022 (validate) and report
    validation MAE. Test seasons (2023-2025) are never touched here."""
    from betdata.nfl.data import load_store
    from betdata.nfl.groups import GROUPS as ALL
    from betdata.nfl.groups import ORDER, columns
    from betdata.nfl.pipeline import TRAIN, VALIDATE, evaluate, outcomes

    names = [n.strip() for n in groups.split(",")] if groups else ORDER
    if "base" not in names:
        names = ["base", *names]
    with _engine().connect() as conn:
        store = load_store(conn)
        close = closing_lines(conn)
    ids = model_games(store, (*TRAIN, *VALIDATE))
    out = outcomes(store)
    vids = [i for i in ids if store.games.at[i, "season"] in VALIDATE]
    cm, ct = close_mae(close, out, vids)
    typer.echo(f"{'closing line (reference)':28} {cm:8.3f} {ct:8.3f}  (validation games)")
    rows = None
    kept_m, kept_t = [], []
    best = None
    typer.echo(f"{'step':28} {'margin':>8} {'total':>8}  {'lam':>9}  decision")
    for name in names:
        r = feature_rows(store, ALL[name], ids, refresh)
        rows = r if rows is None else rows.merge(
            r.drop(columns=["season", "week"]), on=["game_id", "point"])  # fmt: skip
        m, t = columns(rows.columns, name)
        ev = evaluate(rows, out, kept_m + m or ["m_base_hfa"], kept_t + t or ["t_base_league"])
        if best is None:
            decision = "base"
            keep = True
        else:
            keep = ev.margin < best.margin or ev.total < best.total
            decision = "KEEP" if keep else "drop"
        typer.echo(f"{'+ ' + name:28} {ev.margin:8.3f} {ev.total:8.3f}  "
                   f"{ev.lam_m:4g}/{ev.lam_t:<4g}  {decision}")  # fmt: skip
        for p, (em, et, n) in ev.valid.items():
            typer.echo(f"    {p:14} valid {em:6.3f} {et:6.3f} ({n} games)   "
                       f"train {ev.train[p][0]:6.3f} {ev.train[p][1]:6.3f}")  # fmt: skip
        if keep:
            kept_m, kept_t, best = kept_m + m, kept_t + t, ev
