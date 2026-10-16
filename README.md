# betdata — sports odds and stats ingestion

Pulls free-tier sports data (NFL, NCAAF, NBA, NCAAB) on a schedule into a local database, so
models never depend on live API calls. Odds are stored as an append-only history.

## Setup

```bash
uv sync
cp .env.example .env          # add your API keys
uv run betdata db init        # create/upgrade schema (Alembic) + load seeds/*.csv
```

## Usage

```bash
uv run betdata run the-odds-api events --sport nfl   # free: discovers upcoming games
uv run betdata run the-odds-api odds --sport nfl     # 3 credits: h2h + spreads + totals, US books
uv run betdata run nflverse schedules                 # NFL schedule, scores, lines (current season)
uv run betdata run nflverse schedules --seasons all  # backfill 1999-present (~15 s, no quota)
uv run betdata quota                                  # budget per metered API
uv run betdata runs                                   # recent ingestion runs
uv run betdata unmatched                              # team names waiting for review
uv run betdata reparse the_odds_api <raw_id>          # re-parse a stored response, no API call
```

`--priority high` skips daily pacing (never the hard cap). Tests: `uv run pytest`.

## Teams and name mapping

NFL and NBA teams ship in `seeds/`. College teams come from CFBD (1 call each, shared quota):

```bash
uv run betdata teams import --sport ncaaf      # FBS + FCS, with "School Mascot" and alt names
uv run betdata teams import --sport ncaab      # D-I, with ESPN display names and ESPN ids
uv run betdata run the-odds-api participants --sport ncaab   # 1 credit: check every Odds API name
uv run betdata unmatched                        # what didn't map, with a suggested team + score
uv run betdata alias accept 12 15 --yes         # accept reviewed suggestions
uv run betdata alias accept --all --sport ncaab --min-score 0.9   # shows the list, asks first
uv run betdata alias add ncaab "Saint Marys (CA) Gaels" "Saint Mary's" --source the_odds_api
uv run betdata teams list ncaab --search miami
```

Then load college schedules, scores and lines (CFBD is the schedule authority for NCAAF/NCAAB):

```bash
uv run betdata backfill --sport ncaaf --seasons 2015-2025   # 2 calls/season; asks first
uv run betdata backfill --sport ncaab --seasons 2022-2025   # 12 calls/season (monthly windows)
uv run betdata run cfbd games_basketball --param start=2026-11-01 --param end=2026-11-30
```

Backfill shows the cost against this month's remaining CFBD budget, refuses if it doesn't fit,
and skips windows already fetched (stored responses), so an interrupted backfill can be rerun
for free. CFBD lines are stored per provider (consensus, DraftKings, ESPN Bet, Bovada, ...) as
`source_opening`, then `poll` while the game is upcoming, and `source_closing` once final;
spreads/totals carry a line but no price. Basketball games against non-D-I teams are skipped.

Names are never guessed into a match. An alias two schools share ("Miami") is never added;
odds for a game with an unmapped team are still stored, and accepting the alias links them
(`betdata relink` runs automatically). Accepted aliases are also appended to
`seeds/team_aliases.csv`, so a rebuilt database gets them back — commit that file.

## NBA schedules and scores

```bash
uv run betdata run nba-cdn schedule      # NBA's own feed: whole season (the NBA authority)
uv run betdata run nba-cdn scoreboard    # today's games, same-night finals
uv run betdata backfill --source sportsdataverse --sport nba --seasons 2015-2026   # ~40 s
uv run betdata run sportsdataverse schedule --sport ncaab   # backup college scores (ESPN)
```

`nba_cdn` reads cdn.nba.com (the backend nba_api uses for live data) and owns NBA games.
`sportsdataverse` reads ESPN-derived schedule files from GitHub releases (scores up to a day
behind) and is the automatic fallback: if NBA's servers refuse this machine, its games and
scores still flow; when nba_cdn works, it takes the games over. All-Star exhibitions and TBD
placeholders are skipped. For college basketball, sportsdataverse attaches to CFBD's games by
the ESPN game id CFBD records, giving a second, keyless source of scores.

## Venues and weather

```bash
uv run betdata run cfbd venues_football              # 1 CFBD call: every college venue + coords
uv run betdata run open-meteo forecast --sport nfl   # outdoor games in the next 7 days
uv run betdata run open-meteo observed --sport nfl   # finished games, once the archive has them
uv run betdata backfill --source open_meteo --sport nfl --seasons 2015-2025   # history
```

NFL venues ship in `seeds/venues_nfl.csv` (all 67 stadium ids nflverse uses since 1999, with
coordinates and roof type; nflverse's roof field is wrong for a few international venues and
is corrected there). College venues come from CFBD: home stadiums with the team import,
neutral and bowl sites from `venues_football`. Each game also stores its own roof state when
nflverse reports it (`open`/`closed` for retractable roofs).

Weather comes from Open-Meteo (free, non-commercial, CC BY 4.0 — attribute "Weather data by
Open-Meteo.com"). Forecasts for outdoor and retractable-roof NFL/college football games run every
3 hours for the coming week and every one is kept (`lead_hours` says how far ahead it was);
observed (ERA5 reanalysis) weather is added ~6 days after each game. Rows cover the kickoff
hour and the next three, in UTC: temperature (°C), wind and gusts (km/h), direction, rain (mm),
humidity. Domes are skipped. `betdata status` flags upcoming outdoor games with no venue
coordinates.

## Sharp and frequent odds (SharpAPI, OddsPapi)

```bash
uv run betdata run sharpapi odds --sport nba          # DraftKings + FanDuel, paged, >=10 s apart
uv run betdata run oddspapi tournaments --sport nba   # 1 request: find the league's tournament id
uv run betdata run oddspapi markets --sport nba       # 1 request: market/outcome id catalog
uv run betdata run oddspapi odds --sport nba          # 1 request: Pinnacle's board for the league
uv run betdata reparse oddspapi <raw_id>              # rebuild after editing the market map
```

SharpAPI is polled every 10 minutes in the 3 hours before games (hourly earlier that day);
suspended prices are kept as `suspended`. OddsPapi is archive-first: its market and outcome
ids are mapped in `config/sources.yaml` (`oddspapi.markets`, only NBA moneyline is known so
far), unmapped markets are counted and skipped, and every response is archived so the history
can be rebuilt with `reparse` once the map is filled in. Leagues without a tournament id or
market map are skipped without spending a request.

**Closing lines:** `v_closing_lines` gives one closing line per source/game/book/market/side:
the source's own close (nflverse, CFBD) when it reports one, otherwise the last polled state at
or before kickoff (`closing_basis` says which; check `fetched_at` for freshness).

## Running unattended

```bash
uv run betdata scheduler plan --days 30   # projected quota spend from schedule + known games
uv run betdata scheduler list             # validate config/schedule.yaml, show next fire times
uv run betdata scheduler run              # foreground daemon (one instance; lock in data/)
uv run betdata status                     # health, last/next runs, quota, data, problems
uv run betdata status --check             # exit 1 on problems, for cron/monitoring
```

Jobs live in `config/schedule.yaml` (standard cron, US Eastern). Odds jobs are gated on the
games table, so credits are only spent when games are about to start: NFL gets one snapshot
before every kickoff window (~80 credits/month), NBA and college basketball one before the
evening tips (plus weekend/Saturday day games), college football three on Saturdays. Event
lists for sports without a schedule source yet are gated by season months. Worst case with all
four sports in season is ~330 of 450 usable credits/month; daily pacing allows up to 2x an even
share so busy Saturdays/Sundays are not skipped. Jobs run one at a time; a job that crashes is logged and the scheduler carries on;
missed runs (machine asleep) run once on wake if within the job's grace period. Logs go to
`data/logs/scheduler.log`. To keep it running, see `deploy/betdata-scheduler.service`
(systemd); on macOS use a launchd agent or `tmux`.

## How it works

- **One module per source** in `src/betdata/sources/`, each implementing `fetch` / `parse` /
  `load` (`core/source.py`). Only `fetch` touches the network.
- **Every call** goes through `core/http.py`: circuit breaker → quota check → request with
  exponential backoff (honors `Retry-After`; no retry on 4xx auth errors) → logged to
  `api_usage` → body stored gzipped in `raw_responses` before parsing. API keys are redacted.
- **Quota** (`core/quota.py`): limits live in `config/sources.yaml`. A safety margin (10% by
  default) is never spent; normal-priority jobs are also paced evenly over the rest of the
  period. API-reported remaining counts (e.g. The Odds API headers) override the local count
  when they are more conservative.
- **Odds history** (`core/odds_loader.py`): a snapshot row is written only when a
  selection's line, price or status changes; every poll is in `ingest_runs`. Selections that
  disappear from a requested market of a game still listed are written as `removed` with their
  last line/price. Rows are never updated or deleted. Re-loading the same response is a no-op.
- **Team names** (`core/normalize/teams.py`): exact alias → canonical name → normalized
  match. Fuzzy matches are only suggestions in `unmatched_entities`; odds for unmatched teams
  are still stored (with `game_id` null). Fix by adding a row to `seeds/team_aliases.csv` and
  running `betdata db seed`.
- **Games across sources** (`core/matching.py`): one `games` row per real game; each source's
  id maps to it via `game_source_ids` (same teams within 36 h). An authoritative schedule
  source (nflverse for NFL) owns kickoff, season, week and which team is home; others can only
  update games they created. Odds keep each source's own home/away orientation forever;
  `home_away_swapped` records neutral-site disagreements and the `v_odds_snapshots` view
  returns `game_outcome` in the game's orientation.
- **nflverse lines** are stored as book `nflverse` (nflverse does not name the book): `poll`
  snapshots while a game is upcoming, `source_closing` once it is final.
- **Game status**: `scheduled`, `final`, `postponed`, `cancelled` (from CFBD basketball). Odds
  jobs only spend credits on `scheduled` games.
- **Portability**: SQLAlchemy Core + Alembic, generic types, UTC everywhere. Set
  `BETDATA_DB_URL` to a Postgres URL to switch.

## Data sources and polling plan

| Source | Free limit | Plan |
|---|---|---|
| The Odds API | 500 credits/mo | one multi-book snapshot before each kickoff window (~74–330/mo) |
| SharpAPI | 12 req/min, DK + FD, 60 s delay | every 10 min near tip/kickoff, hourly otherwise; capped at 6/min |
| OddsPapi | ~250/mo (vendor figure) | one Pinnacle board per league before game time (~80/mo at peak) |
| nflverse (`nflreadpy`) | none | schedules/closing lines/results daily, PBP nightly, injuries daily |
| NBA (cdn.nba.com) | unofficial, no quota | season schedule daily, scoreboard twice nightly |
| sportsdataverse | none (GitHub releases) | NBA fallback + history; backup NCAAB scores |
| CFBD | 1,000/mo shared FB + BB | ~150/mo ongoing, ~300 for backfill, 900 hard cap |
| Open-Meteo | 10k/day (weighted), non-commercial | outdoor games: forecast every 3 h for the week, observed ~6 days after (~150/day) |

## Milestones

- [x] **M0** foundation: schema + migration, seeds, HTTP client, quota, usage log, raw store
- [x] **M1** The Odds API → NFL end to end
- [x] **M2** nflverse schedules, results and closing lines; cross-source game matching; backfill
- [x] **M3** unattended scheduler, gating, spend projection, status
- [x] **M4** college team import, review queue and alias tools, relink; NBA/NCAAF/NCAAB odds
- [x] **M5** CFBD college schedules, scores and lines; budgeted backfill
- [x] **M6** SharpAPI (frequent DK/FD), OddsPapi (Pinnacle, archive-first), closing-line view
- [x] **M7** NBA schedules/scores (cdn.nba.com + sportsdataverse fallback); backup NCAAB scores
- [x] **M8** venues (NFL seed, CFBD) and Open-Meteo forecasts / observed weather
- [ ] M9 injuries, PBP parquet, Postgres check
