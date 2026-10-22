from __future__ import annotations

import logging

import typer
from sqlalchemy import desc, select

from betdata.config import get_settings
from betdata.core.quota import QuotaManager
from betdata.db import seed as seed_mod
from betdata.db.engine import make_engine, upgrade
from betdata.db.schema import ingest_runs, teams, unmatched_entities, utcnow
from betdata.nba.cli import nba_app
from betdata.nfl.cli import nfl_app
from betdata.props.cli import props_app
from betdata.runner.cli import page_app, runner_app
from betdata.sources import REGISTRY

app = typer.Typer(help="Sports data ingestion.", no_args_is_help=True)
db_app = typer.Typer(help="Database setup.", no_args_is_help=True)
app.add_typer(db_app, name="db")
sched_app = typer.Typer(help="Unattended scheduled polling.", no_args_is_help=True)
app.add_typer(sched_app, name="scheduler")
teams_app = typer.Typer(help="Team registry.", no_args_is_help=True)
app.add_typer(teams_app, name="teams")
alias_app = typer.Typer(help="Map source spellings to teams.", no_args_is_help=True)
app.add_typer(alias_app, name="alias")
lines_app = typer.Typer(help="Stored game and prop lines.", no_args_is_help=True)
app.add_typer(lines_app, name="lines")
games_app = typer.Typer(help="Game rows and their source ids.", no_args_is_help=True)
app.add_typer(games_app, name="games")
app.add_typer(nfl_app, name="nfl")
app.add_typer(nba_app, name="nba")
app.add_typer(props_app, name="props")
app.add_typer(runner_app, name="runner")
app.add_typer(page_app, name="page")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _engine():
    return make_engine(get_settings().db_url)


def _source(name: str):
    name = name.replace("-", "_")
    if name not in REGISTRY:
        raise typer.BadParameter(f"unknown source {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](_engine(), get_settings().source(name))


@db_app.command("init")
def db_init():
    """Create/upgrade the schema and load seed data."""
    settings = get_settings()
    upgrade(settings.db_url)
    counts = seed_mod.seed(make_engine(settings.db_url))
    typer.echo(f"database ready at {settings.db_url}; seeded {counts}")


@db_app.command("seed")
def db_seed():
    """(Re)load seeds/*.csv. Safe to repeat."""
    typer.echo(seed_mod.seed(_engine()))


@app.command()
def run(
    source: str,
    job: str,
    sport: str = typer.Option(None, help="nfl, ncaaf, nba, ncaab (odds sources)"),
    seasons: str = typer.Option(None, help="2026 | 2015-2026 | all (nflverse; default current)"),
    priority: str = typer.Option(None, help="normal (paced) or high (skips pacing, never the cap)"),
    param: list[str] = typer.Option(None, "--param", help="Extra job parameter key=value."),
):
    """Run one job, e.g. `betdata run the-odds-api odds --sport nfl`,
    `betdata run nflverse schedules --seasons all`,
    `betdata run cfbd games_basketball --param start=2026-11-01 --param end=2026-11-30`."""
    params = {k: v for k, v in dict(sport=sport, seasons=seasons).items() if v is not None}
    for item in param or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"--param expects key=value, got {item!r}")
        params[key] = value
    result = _source(source).run(job, priority=priority, **params)
    typer.echo(
        f"run {result.run_id}: {result.status} inserted={result.rows_inserted} "
        f"seen={result.rows_seen} {result.message}"
    )
    if result.status == "failed":
        raise typer.Exit(1)


@app.command()
def backfill(
    sport: str = typer.Option(
        None, help="ncaaf/ncaab (cfbd), nba/ncaab (sportsdataverse), nfl/ncaaf (open_meteo)"
    ),  # fmt: skip
    seasons: str = typer.Option(..., help="2025 | 2018-2025"),
    source_name: str = typer.Option(
        "cfbd", "--source",
        help="cfbd, sportsdataverse, open_meteo (weather), nflverse or nflverse_extra",
    ),  # fmt: skip
    dataset: str = typer.Option(
        None, help="nflverse: injuries, pbp, team_stats, snaps or rosters; nflverse_extra: "
        "comma-separated (participation,ftn,pfr_pass,...,depth_charts,player_stats,ngs_*) or all; "
        "sportsdataverse nba: schedule (default), player_box or pbp (lineup stints; box first)",
    ),  # fmt: skip
    yes: bool = typer.Option(False, "--yes", help="Run without asking."),
    refetch: bool = typer.Option(False, help="Call again even for windows already fetched."),
):
    """Load past seasons. CFBD: 2 calls per football season, 12 per basketball season, shown
    against the remaining budget first. sportsdataverse (no quota): one file per season.
    NFL history: `betdata run nflverse schedules --seasons all`."""
    from betdata.sources.cfbd import backfill_plan
    from betdata.sources.nflverse import parse_seasons

    years = sorted(parse_seasons(seasons) or [])
    if source_name == "nflverse":
        datasets = ("injuries", "pbp", "team_stats", "snaps", "rosters")
        if dataset not in datasets:
            raise typer.BadParameter(
                f"--dataset must be one of {', '.join(datasets)} (schedules: `run`)"
            )
        nfl = _source("nflverse")
        for year in years:
            result = nfl.run(dataset, seasons=str(year))
            typer.echo(f"  {dataset} {year}: {result.status} inserted={result.rows_inserted} "
                       f"{result.message}")  # fmt: skip
            if result.status == "failed":
                raise typer.Exit(1)
        return
    if source_name == "nflverse_extra":
        from betdata.sources.nflverse_extra import DATASETS

        names = list(DATASETS) if dataset in (None, "all") else dataset.split(",")
        unknown = sorted(set(names) - set(DATASETS))
        if unknown:
            raise typer.BadParameter(f"unknown datasets {unknown}; available: {list(DATASETS)}")
        extra = _source("nflverse_extra")
        for name in names:
            per_season = DATASETS[name].first is not None
            for year in years if per_season else [None]:
                params = {"seasons": str(year)} if per_season else {}
                result = extra.run(name, **params, **({"refetch": "1"} if refetch else {}))
                typer.echo(f"  {name} {year or 'all'}: {result.status} "
                           f"new={result.rows_inserted} {result.message}")  # fmt: skip
                if result.status == "failed":
                    raise typer.Exit(1)
        return
    if source_name == "open_meteo":
        if sport not in ("nfl", "ncaaf"):
            raise typer.BadParameter("weather backfill covers nfl and ncaaf")
        meteo = _source("open_meteo")
        while True:  # each run does up to 200 requests; stops when done or out of daily budget
            result = meteo.run("observed", priority="high", sport=sport, seasons=seasons)
            typer.echo(f"  {result.status}: games={result.rows_seen} rows={result.rows_inserted} "
                       f"{result.message}")  # fmt: skip
            if result.status != "ok":
                done = "NothingToDo" in result.message
                typer.echo("done" if done else "stopped; rerun later to continue")
                raise typer.Exit(0 if done else 1)
    if source_name == "sportsdataverse":
        if sport not in ("nba", "ncaab"):
            raise typer.BadParameter("sportsdataverse backfill covers nba and ncaab")
        job = dataset or "schedule"
        if job not in ("schedule", "player_box", "pbp") or (job != "schedule" and sport != "nba"):
            raise typer.BadParameter("--dataset must be schedule, player_box or pbp (nba only)")
        sdv = _source("sportsdataverse")
        for year in years:
            if job == "schedule":
                result = sdv.run("schedule", sport=sport, seasons=str(year))
                typer.echo(f"  {sport} {year}: {result.status} games={result.rows_seen} "
                           f"results_changed={result.rows_inserted} {result.message}")  # fmt: skip
            else:
                result = sdv.run(job, seasons=str(year))
                typer.echo(f"  {job} {year}: {result.status} games={result.rows_seen} "
                           f"rows={result.rows_inserted} {result.message}")  # fmt: skip
            if result.status == "failed":
                raise typer.Exit(1)
        return
    if source_name != "cfbd":
        raise typer.BadParameter(
            "--source must be cfbd, sportsdataverse, open_meteo, nflverse or nflverse_extra"
        )
    if sport not in ("ncaaf", "ncaab"):
        raise typer.BadParameter("sport must be ncaaf or ncaab")
    source = _source("cfbd")
    plan = backfill_plan(sport, years)
    cached = [] if refetch else [(j, p) for j, p in plan if source.already_fetched(j, p)]
    plan = [item for item in plan if item not in cached]
    if cached:
        typer.echo(f"{len(cached)} windows already fetched (skipped; --refetch to call again)")
    status = source.quota.status()
    typer.echo(
        f"{len(plan)} CFBD calls; {status.remaining_usable} usable left this month "
        f"(resets {status.period_end:%Y-%m-%d})"
    )
    if len(plan) > status.remaining_usable:
        typer.echo("not enough budget this month; backfill fewer seasons now and the rest later")
        raise typer.Exit(1)
    if not yes and not typer.confirm("Run it?"):
        raise typer.Abort()
    total = 0
    for job, params in plan:
        result = source.run(job, priority="high", **params)
        typer.echo(
            f"  {job} {params}: {result.status} inserted={result.rows_inserted} {result.message}"
        )
        if result.status != "ok":
            typer.echo("stopped; rerun the same command to continue (loading is idempotent)")
            raise typer.Exit(1)
        total += result.rows_inserted
    typer.echo(f"done: {total} rows")


@app.command()
def quota(source: str = typer.Argument(None)):
    """Show budget status for metered sources."""
    settings, engine = get_settings(), _engine()
    for name, cfg in settings.sources.items():
        if not cfg.quota or (source and name != source.replace("-", "_")):
            continue
        s = QuotaManager(engine, name, cfg.quota).status()
        reported = "n/a" if s.remaining_reported is None else s.remaining_reported
        typer.echo(
            f"{name:14} used {s.used_effective}/{s.limit} (usable {s.usable}) "
            f"remaining_usable={s.remaining_usable} api_reported_remaining={reported} "
            f"today={s.used_today}/{s.daily_allowance} period_end={s.period_end:%Y-%m-%d}"
        )


@app.command()
def runs(limit: int = 20):
    """Recent ingestion runs."""
    with _engine().connect() as conn:
        for r in conn.execute(
            select(ingest_runs).order_by(desc(ingest_runs.c.run_id)).limit(limit)
        ):
            typer.echo(
                f"{r.run_id:5} {r.started_at:%Y-%m-%d %H:%M} {r.source}.{r.job} {r.status} "
                f"ins={r.rows_inserted} seen={r.rows_seen} {r.message or ''}"
            )


@app.command()
def reparse(source: str, raw_id: int):
    """Re-run parse+load on a stored raw response (no API call, no quota)."""
    inserted, seen = _source(source).reparse(raw_id)
    typer.echo(f"raw {raw_id}: inserted={inserted} seen={seen}")


def _pending(conn, sport: str | None = None, ids: list[int] | None = None):
    from betdata.db.schema import sports

    q = (
        select(unmatched_entities, sports.c.key.label("sport"))
        .join(sports, sports.c.sport_id == unmatched_entities.c.sport_id)
        .where(unmatched_entities.c.resolved.is_(False), unmatched_entities.c.kind == "team")
        .order_by(sports.c.key, desc(unmatched_entities.c.times_seen))
    )
    if sport:
        q = q.where(sports.c.key == sport)
    if ids:
        q = q.where(unmatched_entities.c.unmatched_id.in_(ids))
    return conn.execute(q).all()


def _suggestions(conn, rows):
    """(row, Suggestion | None) with suggestions recomputed against the current team list."""
    from betdata.core.normalize.teams import TeamResolver

    resolvers: dict[int, TeamResolver] = {}
    out = []
    for r in rows:
        resolver = resolvers.setdefault(r.sport_id, TeamResolver(conn, r.sport_id))
        out.append((r, resolver.suggest(r.raw_value)))
    return out


@app.command()
def unmatched(sport: str = typer.Option(None, help="Only this sport.")):
    """Team names no source could map. Fix with `betdata alias accept ID` or `alias add`."""
    with _engine().connect() as conn:
        rows = _suggestions(conn, _pending(conn, sport))
    if not rows:
        typer.echo("nothing to review")
    for r, sug in rows:
        hint = f"-> {sug.label!r} ({sug.score:.2f})" if sug else "-> no suggestion"
        name = repr(r.raw_value)
        typer.echo(
            f"{r.unmatched_id:5} {r.sport:6} {r.source:14} {name:40} x{r.times_seen:<4} {hint}"
        )


@alias_app.command("add")
def alias_add(
    sport: str,
    alias: str,
    team: str = typer.Argument(..., help="Canonical name (or any name that already resolves)."),
    source: str = typer.Option("*", help="Source that uses this spelling; * = any source."),
):
    """Map a name to a team, e.g. `betdata alias add ncaab "UConn Huskies" Connecticut`."""
    from betdata.core.teams_admin import AliasConflict, add_alias, find_team, relink

    with _engine().begin() as conn:
        team_id = find_team(conn, sport, team)
        if team_id is None:
            raise typer.BadParameter(f"no {sport} team called {team!r} (see `betdata teams list`)")
        try:
            added = add_alias(conn, sport=sport, source=source, alias=alias, team_id=team_id)
        except AliasConflict as e:
            raise typer.BadParameter(str(e)) from None
        stats = relink(conn)
    typer.echo(
        f"{'added' if added else 'already present'}; relinked {stats.events_linked} events, "
        f"{stats.snapshots_linked} odds rows"
    )


@alias_app.command("accept")
def alias_accept(
    ids: list[int] = typer.Argument(None, help="unmatched ids from `betdata unmatched`"),
    sport: str = typer.Option(None, help="With --all: only this sport."),
    all_: bool = typer.Option(False, "--all", help="Every pending name that has a suggestion."),
    min_score: float = typer.Option(0.0, help="With --all: skip suggestions below this score."),
    yes: bool = typer.Option(False, "--yes", help="Apply without asking."),
):
    """Accept the suggested team for reviewed names. Shows the list and asks first."""
    from betdata.core.teams_admin import AliasConflict, add_alias, relink

    if not ids and not all_:
        raise typer.BadParameter("give unmatched ids, or --all")
    with _engine().begin() as conn:
        picks = [
            (r, s) for r, s in _suggestions(conn, _pending(conn, sport, ids or None))
            if s and s.score >= min_score
        ]  # fmt: skip
        if not picks:
            typer.echo("nothing to accept")
            return
        for r, s in picks:
            typer.echo(
                f"  {r.sport:6} {r.source:14} {r.raw_value!r:40} -> {s.label!r} ({s.score:.2f})"
            )
        if not yes and not typer.confirm(f"Add these {len(picks)} aliases?"):
            raise typer.Abort()
        added = 0
        for r, s in picks:
            try:
                added += add_alias(
                    conn, sport=r.sport, source=r.source, alias=r.raw_value, team_id=s.team_id
                )
            except AliasConflict as e:
                typer.echo(f"skipped: {e}")
        stats = relink(conn)
    typer.echo(
        f"added {added}; relinked {stats.events_linked} events, {stats.snapshots_linked} odds rows"
    )


@app.command("relink")
def relink_cmd(source: str = typer.Option(None)):
    """Link stored events whose teams are now known to games (no API calls)."""
    from betdata.core.teams_admin import relink

    with _engine().begin() as conn:
        stats = relink(conn, source=source.replace("-", "_") if source else None)
    typer.echo(
        f"checked {stats.events_checked} events; linked {stats.events_linked}; "
        f"{stats.snapshots_linked} odds rows now have a game"
    )


@lines_app.command("coverage")
def lines_coverage(
    sport: str = typer.Option(..., help="nba or nfl"),
    days: int = typer.Option(7, help="Games that started in the last N days (plus the next 2)."),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 if a started game has no close or an upcoming one no line."
    ),
    at: str = typer.Option(None, help="As of this instant (ISO, UTC; default now)."),
):
    """Opening, current and pre-game closing lines per source and book (issue #157)."""
    from datetime import datetime

    from betdata.lines_coverage import coverage, report

    now = datetime.fromisoformat(at) if at else utcnow()
    with _engine().connect() as conn:
        cov = coverage(conn, sport, now=now, days=days)
    for line in report(cov):
        typer.echo(line)
    if strict and not cov.complete:
        raise typer.Exit(1)
@games_app.command("split-merged")
def games_split_merged(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; change nothing."),
    relink_odds: bool = typer.Option(
        False, "--relink-odds", help="Also point stored odds/prop rows at the new game."
    ),
):
    """Split games holding two events of one source (issue #156); reload results after."""
    from betdata.core.split_merged import merged_games, split_game

    engine = _engine()
    with engine.connect() as conn:
        tx = conn.begin()
        done = []
        for gid in merged_games(conn):
            r = split_game(conn, gid, relink_odds=relink_odds)
            done.append(r)
            ids = ", ".join(f"{s}:{i}" for s, i in r.detached)
            if r.skipped:
                typer.echo(f"game {gid}: skipped ({r.skipped}); events {ids}")
                continue
            typer.echo(
                f"game {gid}: {ids} -> new game(s) {r.new_games}; deleted {r.deleted}; "
                f"odds/prop rows relinked {r.relinked}; predictions/grades left {r.left_on_kept}"
            )
        split = sum(1 for r in done if not r.skipped)
        if dry_run:
            tx.rollback()
        else:
            tx.commit()
    typer.echo(
        f"{len(done)} merged game(s); {split} {'would be ' if dry_run else ''}split, "
        f"{len(done) - split} skipped."
    )
    if split and not dry_run:
        typer.echo(
            "Reload results, box scores and stints to refill the deleted rows: `betdata backfill "
            "--source sportsdataverse --sport nba --seasons ...` (then `--dataset player_box`, "
            "`--dataset pbp`) or the source that owns the games."
        )


@teams_app.command("import")
def teams_import(
    sport: str = typer.Option(..., help="ncaaf or ncaab"),
    season: int = typer.Option(None, help="Season (default: current)."),
):
    """Import college teams and their common names from CFBD (1 call from the shared quota)."""
    from betdata.core.teams_admin import relink

    job = {"ncaaf": "teams_football", "ncaab": "teams_basketball"}.get(sport)
    if job is None:
        raise typer.BadParameter("sport must be ncaaf or ncaab (NFL/NBA teams come from seeds)")
    params = {"season": season} if season else {}
    result = _source("cfbd").run(job, **params)
    typer.echo(
        f"run {result.run_id}: {result.status} teams={result.rows_seen} "
        f"aliases_added={result.rows_inserted} {result.message}"
    )
    if result.status != "ok":
        raise typer.Exit(1)
    counts = seed_mod.seed(_engine())  # load saved aliases that were waiting for these teams
    with _engine().begin() as conn:
        stats = relink(conn)
    typer.echo(
        f"seed aliases pending={counts['aliases_pending']}; relinked {stats.events_linked} events"
    )


@teams_app.command("list")
def teams_list(sport: str, search: str = typer.Option(None, help="Substring filter.")):
    """Canonical team names for a sport (use these with `alias add`)."""
    from betdata.core.matching import sport_id

    with _engine().connect() as conn:
        q = select(teams.c.canonical_name, teams.c.abbrev, teams.c.conference).where(
            teams.c.sport_id == sport_id(conn, sport)
        )
        if search:
            q = q.where(teams.c.canonical_name.ilike(f"%{search}%"))
        for name, abbrev, conf in conn.execute(q.order_by(teams.c.canonical_name)):
            typer.echo(f"{name:35} {abbrev or '':8} {conf or ''}")


@app.command()
def backtest(
    sport: str = typer.Argument("nfl", help="nfl (the only sport with free historical lines)"),
    model: str = typer.Option(
        "elo", help="elo; units (unit ratings + missing starters); market (never bets)"
    ),
    train: str = typer.Option(
        None, help="Seasons that choose parameters and threshold (elo 2000-2014, units 2013-2019)."
    ),
    test: str = typer.Option(
        None, help="Unseen seasons, scored once (elo 2015-2025, units 2020-2025)."
    ),
    threshold: float = typer.Option(None, help="Bet when model and close differ by >= this."),
    k: float = typer.Option(None, help="Elo: fix K instead of choosing it on training seasons."),
    hfa: float = typer.Option(None, help="Elo: home-field advantage in Elo points."),
    revert: float = typer.Option(None, help="Elo: share of rating pulled to the mean yearly."),
    bye: float = typer.Option(None, help="Elo: bonus after 13+ days of rest."),
    regular_only: bool = typer.Option(False, help="Bet regular-season games only."),
    lines_source: str = typer.Option("nflverse", help="Source of closing lines."),
):
    """Walk-forward backtest against closing lines. Load history first:
    `betdata run nflverse schedules --seasons all`; for --model units also team_stats
    (1999+), snaps (2012+), injuries (2009+) and rosters (2011+) via `betdata backfill`."""
    import os
    from pathlib import Path

    from betdata.analysis.dataset import load_games
    from betdata.analysis.models import MODELS
    from betdata.analysis.report import run_backtest, write_csv
    from betdata.config import PROJECT_ROOT
    from betdata.sources.nflverse import parse_seasons

    if sport != "nfl":
        raise typer.BadParameter("only nfl has free historical closing lines so far")
    if model not in (*MODELS, "units"):
        raise typer.BadParameter(f"model must be one of {sorted((*MODELS, 'units'))}")
    fixed = {k_: v for k_, v in dict(k=k, hfa=hfa, revert=revert, bye=bye).items() if v is not None}
    if fixed and model != "elo":
        raise typer.BadParameter("--k/--hfa/--revert/--bye apply to the elo model")
    if fixed and len(fixed) < 4:
        raise typer.BadParameter("give all of --k --hfa --revert --bye, or none to choose them")
    train = train or ("2013-2019" if model == "units" else "2000-2014")
    test = test or ("2020-2025" if model == "units" else "2015-2025")
    with _engine().connect() as conn:
        games = load_games(conn, sport, lines_source=lines_source)
    train_s, test_s = sorted(parse_seasons(train) or []), sorted(parse_seasons(test) or [])
    have = {g.season for g in games if g.final and g.spread_home is not None}
    if not set(train_s) & have or not set(test_s) & have:
        typer.echo("no games with scores and closing lines in those seasons; load them with "
                   "`betdata run nflverse schedules --seasons all`")  # fmt: skip
        raise typer.Exit(1)
    data_dir = Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")
    stamp = f"{utcnow():%Y%m%dT%H%M%S}"
    if model == "units":
        from betdata.analysis.context import load_context
        from betdata.analysis.units_backtest import run_units_backtest, write_units_csv

        with _engine().connect() as conn:
            ctx = load_context(conn)
        if not ctx.stats or not ctx.snaps:
            typer.echo("no team stats or snap counts; load them with `betdata backfill --source "
                       "nflverse --dataset team_stats --seasons 1999-2026` (and snaps, injuries, "
                       "rosters)")  # fmt: skip
            raise typer.Exit(1)
        try:
            result = run_units_backtest(games, ctx, train_s, test_s, regular_only=regular_only,
                                        spread_threshold=threshold)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
        typer.echo("\n".join(result.lines))
        path = write_units_csv(result, data_dir / "reports" / f"backtest_{sport}_units_{stamp}.csv")
        typer.echo(f"Bets written to {path}")
        f = result.fits["units+missing"]
        typer.echo(f"Live picks with these settings: betdata predict {sport} --model units "
                   f"--alpha {f.alpha:g} --revert {f.revert:g}")  # fmt: skip
        return
    try:
        result = run_backtest(
            games, model, train_s, test_s, params=fixed or None, threshold=threshold,
            regular_only=regular_only,
        )  # fmt: skip
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo("\n".join(result.lines))
    path = write_csv(result, data_dir / "reports" / f"backtest_{sport}_{model}_{stamp}.csv")
    typer.echo(f"Bets written to {path}")
    if model == "elo":
        p = result.model.params()
        typer.echo(f"Live picks with these settings: betdata predict {sport} --model elo "
                   f"--k {p['k']:g} --hfa {p['hfa']:g} --revert {p['revert']:g} "
                   f"--bye {p['bye']:g}")  # fmt: skip


def _absence_text(team: str, out) -> str:
    if not out:
        return f"{team}: no starters missing"
    parts = []
    for a in sorted(out, key=lambda a: (-a.out_prob * a.starter.share, a.starter.name)):
        chance = "" if a.out_prob >= 0.99 else f" {100 * a.out_prob:.0f}%"
        parts.append(f"{a.starter.group} {a.starter.name} ({a.reason}{chance})")
    return f"{team}: " + ", ".join(parts)


@app.command()
def predict(
    sport: str = typer.Argument("nfl"),
    model: str = typer.Option(
        "m12", help="m12 (default), units (M11: unit ratings + missing starters) or elo (M10)"
    ),
    days: int = typer.Option(8, help="Predict games kicking off within this many days."),
    alpha: float = typer.Option(0.05, help="Units: rating speed (use what `backtest` prints)."),
    revert: float = typer.Option(None, help="Season carry-over (units 0.5, elo 0.33)."),
    train: str = typer.Option(None, help="Units: seasons to fit on (default 2013-last season)."),
    k: float = typer.Option(20.0, help="Elo K (use the values `betdata backtest` prints)."),
    hfa: float = typer.Option(48.0, help="Elo home field."),
    bye: float = typer.Option(0.0, help="Elo bye bonus."),
    lines_source: str = typer.Option("nflverse", help="Source of the market line shown."),
):
    """Predict upcoming games and store them in model_predictions (a new row only when the
    prediction or the market line changed). The units model also lists missing starters."""
    from datetime import timedelta

    from betdata.analysis.dataset import load_games
    from betdata.core.matching import season_for

    if sport != "nfl":
        raise typer.BadParameter("only nfl so far")
    now = utcnow()
    until = now + timedelta(days=days)
    if model == "m12":
        from betdata.nfl.live import run_predict
        from betdata.nfl.predict import fmt_pick

        try:
            version, picks = run_predict(_engine(), now, days)
        except ValueError as e:
            typer.echo(str(e))
            raise typer.Exit(1) from None
        if not picks:
            typer.echo("no upcoming games in that window")
            return
        typer.echo(f"{version}: model line and total next to the market (blend = model and "
                   "market combined); factors in points, largest first")  # fmt: skip
        for p in picks:
            typer.echo("\n".join(fmt_pick(p)))
        return
    if model == "units":
        from betdata.analysis.context import load_context
        from betdata.analysis.units_backtest import predict_units_upcoming
        from betdata.sources.nflverse import parse_seasons

        seasons = sorted(parse_seasons(train) or []) if train else list(
            range(2013, season_for(sport, now))
        )  # fmt: skip
        try:
            with _engine().begin() as conn:
                games = load_games(conn, sport, lines_source=lines_source)
                ctx = load_context(conn)
                fitted, rows = predict_units_upcoming(
                    conn, games, ctx, now=now, until=until, train=seasons, alpha=alpha,
                    revert=0.5 if revert is None else revert, lines_source=lines_source,
                )  # fmt: skip
        except ValueError as e:
            typer.echo(f"{e}; load team_stats, snaps, injuries and rosters with `betdata "
                       "backfill --source nflverse --dataset ...`, or use --model elo")  # fmt: skip
            raise typer.Exit(1) from None
        if not rows:
            typer.echo("no upcoming games in that window (or no snap counts loaded)")
            return
        typer.echo(f"{fitted.version}; spreads are the home side (negative = home favoured)")
        for r in rows:
            g = r.game
            line = (f"{g.start:%a %m-%d %H:%M}Z {g.away:>4} @ {g.home:<4} "
                    f"spread model {-r.home_margin:+5.1f}")  # fmt: skip
            if g.spread_home is not None:
                edge = r.home_margin + g.spread_home
                line += (f" market {g.spread_home:+5.1f} lean {g.home if edge > 0 else g.away:<4}"
                         f" {abs(edge):4.1f}")  # fmt: skip
            line += f" | total model {r.total:5.1f}"
            if g.total is not None:
                edge = r.total - g.total
                side = "over" if edge > 0 else "under"
                line += f" market {g.total:5.1f} lean {side:<5} {abs(edge):4.1f}"
            typer.echo(line + ("" if r.stored else "  (unchanged)"))
            typer.echo("    " + _absence_text(g.away, r.away_out))
            typer.echo("    " + _absence_text(g.home, r.home_out))
        return
    if model != "elo":
        raise typer.BadParameter("model must be m12, units or elo")
    from betdata.analysis.models import EloModel
    from betdata.analysis.report import predict_upcoming

    with _engine().begin() as conn:
        games = load_games(conn, sport, lines_source=lines_source)
        rows = predict_upcoming(
            conn, games, EloModel(k=k, hfa=hfa, revert=0.33 if revert is None else revert, bye=bye),
            now=now, until=until, lines_source=lines_source,
        )  # fmt: skip
    if not rows:
        typer.echo("no upcoming games in that window")
        return
    typer.echo("Home spreads (negative = home favoured); lean = side the model prefers.")
    for r in rows:
        g = r.game
        line = f"{g.start:%a %m-%d %H:%M}Z {g.away:>4} @ {g.home:<4} model {-r.home_margin:+6.1f}"
        if g.spread_home is not None:
            edge = r.home_margin + g.spread_home
            lean = g.home if edge > 0 else g.away
            line += f"  market {g.spread_home:+6.1f}  lean {lean:<4} by {abs(edge):4.1f}"
        typer.echo(line + ("" if r.stored else "  (unchanged)"))


def _grade_props(sport: str) -> None:
    from betdata.props.grade import records

    if sport == "nba":
        from betdata.nba.props.grade import MODEL
        from betdata.nba.props.grade import grade_nba_props as run_grade
    elif sport == "nfl":
        try:
            from betdata.nfl.props.grade import MODEL
            from betdata.nfl.props.grade import grade_nfl_props as run_grade
        except ImportError as e:  # NFL props arrive later in M15
            raise typer.BadParameter("NFL prop grading is not built yet") from e
    else:
        raise typer.BadParameter("props: nba or nfl")
    with _engine().begin() as conn:
        run = run_grade(conn, utcnow())
        recs = records(conn, (MODEL,))
    typer.echo(f"graded: {run.inserted} new, {run.updated} updated, {run.pending} waiting for a "
               "box score")
    for r in recs:
        roi = f"{100 * r.profit / r.bets:+.1f}%" if r.bets else "-"
        avg = "-" if r.avg_clv is None else f"{r.avg_clv:+.2f}"
        typer.echo(f"{r.model_version:28} {r.market:11} graded {r.graded:5} over {r.over:4} "
                   f"under {r.under:4} push {r.push:3} void {r.void:3} | bets {r.bets:4} "
                   f"{r.wins}-{r.losses} ROI {roi:>7} CLV {avg}")


@app.command()
def grade(sport: str = typer.Argument("nfl"), props: bool = typer.Option(
        False, "--props", help="grade stored player-prop predictions (M15)")):  # fmt: skip
    """Grade stored predictions on final games (idempotent): result, closing-line value, and
    per model version the record, ROI, average CLV and share of bets that beat the close."""
    if props:
        _grade_props(sport)
        return
    from betdata.nfl.grade import THRESHOLDS, grade_nfl, ml_threshold, records

    if sport != "nfl":
        raise typer.BadParameter("only nfl so far (use --props for NBA prop grading)")
    with _engine().begin() as conn:
        run = grade_nfl(conn, utcnow())
        recs = records(conn)
    typer.echo(f"graded: {run.inserted} new, {run.updated} updated, {run.pending} waiting for a "
               f"final score (bets at >= {THRESHOLDS['spread']:g} pts spread, "
               f">= {THRESHOLDS['total']:g} pts total, -110; moneyline at the price when the "
               f"model's win chance beats the no-vig market by >= {ml_threshold():g} % points)")
    # fmt: skip
    if not recs:
        typer.echo("no graded bets yet")
        return
    typer.echo("avg CLV: points for spreads and totals, % points of no-vig win probability for "
               "the moneyline")
    typer.echo(f"{'model version':42} {'market':9} {'W-L-P':>10} {'void':>4} {'ROI':>7} "
               f"{'avg CLV':>8} {'beat close':>10}")  # fmt: skip
    for r in recs:
        roi = "-" if r.roi is None else f"{100 * r.roi:+.1f}%"
        avg = "-" if r.avg_clv is None else f"{r.avg_clv:+.2f}"
        beat = "-" if r.beat_close is None else f"{100 * r.beat_close:.0f}%"
        typer.echo(f"{r.model_version:42} {r.market:9} {f'{r.wins}-{r.losses}-{r.pushes}':>10} "
                   f"{r.voids:4} {roi:>7} {avg:>8} {beat:>10}")


@app.command()
def status(
    check: bool = typer.Option(False, help="Exit 1 if there are problems (for monitoring)."),
):
    """Health, last/next runs, quota, data counts and problems."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.status import build_status

    report = build_status(_engine(), get_settings(), load_schedule(), utcnow())
    typer.echo("\n".join(report.lines))
    if check and report.problems:
        raise typer.Exit(1)


@sched_app.command("run")
def scheduler_run():
    """Run the scheduler in the foreground (use systemd/launchd/tmux to keep it alive)."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.scheduler.runner import run_forever

    run_forever(_engine(), get_settings(), REGISTRY, load_schedule())


@sched_app.command("list")
def scheduler_list():
    """Validate config/schedule.yaml and show each job's next fire time."""
    from betdata.scheduler.jobs import load_schedule, validate
    from betdata.scheduler.runner import next_runs

    schedule = load_schedule()
    for problem in validate(schedule, get_settings(), REGISTRY):
        typer.echo(f"PROBLEM {problem}")
    for job_id, nxt in next_runs(schedule, utcnow()):
        when = nxt.astimezone(schedule.timezone).strftime("%a %Y-%m-%d %H:%M %Z") if nxt else "-"
        typer.echo(f"{job_id:30} next {when}")


@sched_app.command("plan")
def scheduler_plan(
    days: int = typer.Option(30, help="How many days ahead to project."),
    runner: bool = typer.Option(False, help="Project the hourly runner's jobs "
                                            "(config/sports.yaml) instead of the daemon's."),
    start: str = typer.Option(None, help="Start of the projection (ISO, UTC; default now)."),
):
    """Project quota spend from the schedule and the games already in the database."""
    from datetime import datetime

    from betdata.scheduler.jobs import load_schedule
    from betdata.scheduler.runner import project, projection_report

    settings, engine = get_settings(), _engine()
    schedule = load_schedule()
    if runner:
        from betdata.runner.hourly import runner_schedule

        schedule = runner_schedule()
    t0 = datetime.fromisoformat(start) if start else utcnow()
    projections = project(engine, settings, REGISTRY, schedule, t0, days)
    typer.echo("\n".join(projection_report(engine, settings, projections, days)))


if __name__ == "__main__":
    app()
