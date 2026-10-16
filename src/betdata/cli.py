from __future__ import annotations

import logging

import typer
from sqlalchemy import desc, select

from betdata.config import get_settings
from betdata.core.quota import QuotaManager
from betdata.db import seed as seed_mod
from betdata.db.engine import make_engine, upgrade
from betdata.db.schema import ingest_runs, teams, unmatched_entities, utcnow
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
    sport: str = typer.Option(..., help="ncaaf, ncaab (cfbd) or nba, ncaab (sportsdataverse)"),
    seasons: str = typer.Option(..., help="2025 | 2018-2025"),
    source_name: str = typer.Option("cfbd", "--source", help="cfbd or sportsdataverse"),
    yes: bool = typer.Option(False, "--yes", help="Run without asking."),
    refetch: bool = typer.Option(False, help="Call again even for windows already fetched."),
):
    """Load past seasons. CFBD: 2 calls per football season, 12 per basketball season, shown
    against the remaining budget first. sportsdataverse (no quota): one file per season.
    NFL history: `betdata run nflverse schedules --seasons all`."""
    from betdata.sources.cfbd import backfill_plan
    from betdata.sources.nflverse import parse_seasons

    years = sorted(parse_seasons(seasons) or [])
    if source_name == "sportsdataverse":
        if sport not in ("nba", "ncaab"):
            raise typer.BadParameter("sportsdataverse backfill covers nba and ncaab")
        sdv = _source("sportsdataverse")
        for year in years:
            result = sdv.run("schedule", sport=sport, seasons=str(year))
            typer.echo(f"  {sport} {year}: {result.status} games={result.rows_seen} "
                       f"results_changed={result.rows_inserted} {result.message}")  # fmt: skip
            if result.status == "failed":
                raise typer.Exit(1)
        return
    if source_name != "cfbd":
        raise typer.BadParameter("--source must be cfbd or sportsdataverse")
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
def scheduler_plan(days: int = typer.Option(30, help="How many days ahead to project.")):
    """Project quota spend from the schedule and the games already in the database."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.scheduler.runner import project, projection_report

    settings, engine = get_settings(), _engine()
    projections = project(engine, settings, REGISTRY, load_schedule(), utcnow(), days)
    typer.echo("\n".join(projection_report(engine, settings, projections, days)))


if __name__ == "__main__":
    app()
