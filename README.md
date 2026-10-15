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
uv run betdata unmatched                              # team names that need an alias
uv run betdata reparse the_odds_api <raw_id>          # re-parse a stored response, no API call
```

`--priority high` skips daily pacing (never the hard cap). Tests: `uv run pytest`.

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
- **Portability**: SQLAlchemy Core + Alembic, generic types, UTC everywhere. Set
  `BETDATA_DB_URL` to a Postgres URL to switch.

## Data sources and polling plan

| Source | Free limit | Plan |
|---|---|---|
| The Odds API | 500 credits/mo | NFL ~5×/wk, NCAAF 3×/wk, NBA and NCAAB daily (~285/mo) |
| SharpAPI | 12 req/min, DK + FD, 60 s delay | every 10 min near tip/kickoff, hourly otherwise; capped at 6/min |
| OddsPapi | ~250/mo (unconfirmed) | ~8/day: one board per sport plus big slates |
| nflverse (`nflreadpy`) | none | schedules/closing lines/results daily, PBP nightly, injuries daily |
| nba_api | unofficial | daily, 1 req/2 s, falls back to sportsdataverse |
| sportsdataverse | none | NBA fallback; NCAAB/NCAAF results |
| CFBD | 1,000/mo shared FB + BB | ~150/mo ongoing, ~300 for backfill, 900 hard cap |
| Open-Meteo | 10k/day, non-commercial | outdoor games: forecasts at T-72/24/3 h, observed next day |

## Milestones

- [x] **M0** foundation: schema + migration, seeds, HTTP client, quota, usage log, raw store
- [x] **M1** The Odds API → NFL end to end
- [x] **M2** nflverse schedules, results and closing lines; cross-source game matching; backfill
- [ ] M3 unattended scheduler + status
- [ ] M4 team-mapping tools; The Odds API for NBA/NCAAF/NCAAB
- [ ] M5 CFBD football + basketball
- [ ] M6 SharpAPI + OddsPapi
- [ ] M7 NBA (nba_api + sportsdataverse fallback), NCAAB results
- [ ] M8 Open-Meteo + venues
- [ ] M9 injuries, PBP parquet, Postgres check
