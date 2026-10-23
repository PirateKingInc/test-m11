"""NFL behind the sport interface: the M12 model (frozen fit), its stored predictions, grading,
and the history rebuild from nflverse when the data directory is lost."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, and_, func, select

from betdata.core.matching import season_for, sport_id
from betdata.db.schema import games, model_predictions, teams
from betdata.moneyline import no_vig
from betdata.nfl.grade import THRESHOLDS, grade_nfl, ml_calibration, ml_threshold, records
from betdata.quotes import current_quotes
from betdata.sports import Card, RecordRow, Sport

log = logging.getLogger(__name__)
DAYS = 8  # games predicted and shown: the next 8 days
PAGE_MARKETS = ("spread", "moneyline")  # totals stay in the CLI (they lost money in testing)
FIRST_HISTORY_SEASON = 2010  # what the M12 store reads


class NflSport(Sport):
    @property
    def page_model(self) -> str:
        """The version the page shows: m15 if its switch rule passed (`page: true` in
        config/nfl_model_m15.json), else M13's weather version once validation accepted it,
        otherwise frozen M12. All are predicted, graded and listed in the season record."""
        from betdata.nfl.live15 import page15

        return "m15" if page15() else self.base_model

    @property
    def base_model(self) -> str:
        from betdata.nfl.weather import WX_DESIGN_PATH

        return "m13w" if WX_DESIGN_PATH.exists() else "m12"

    def page_version(self) -> str | None:
        if self.page_model == "m15":
            from betdata.nfl.live15 import version15

            return version15()
        return self.page_model

    def background_versions(self) -> list[str]:
        """m15 runs in the background unless the switch rule moved the page to it."""
        from betdata.nfl.live15 import version15

        v = version15()
        if v is None:
            return []
        return [self.base_model] if self.page_model == "m15" else [v]

    def predict(self, engine: Engine, now: datetime) -> int:
        from betdata.nfl.live import loaded_store, run_predict

        store = None
        try:
            store = loaded_store(engine)  # loaded once: m12, m13w and m15 share it
            _, picks = run_predict(engine, now, DAYS, store=store)
        except ValueError as e:  # no history loaded
            log.warning("nfl predict skipped: %s", e)
            picks = []
        self.predict_15(engine, now, store)
        self.predict_props(engine, now)
        return len(picks)

    def predict_15(self, engine: Engine, now: datetime, store=None) -> int:
        """The M15 side version m15 (background unless the switch rule moved the page)."""
        from betdata.nfl.live15 import run_predict15

        try:
            return len(run_predict15(engine, now, DAYS, store))
        except Exception as e:  # never breaks the run or the m12/m13w predictions
            log.warning("m15 predict skipped: %s: %s", type(e).__name__, e)
            return 0

    def predict_props(self, engine: Engine, now: datetime) -> int:
        """M15 prop predictions for players with a stored line (never breaks the run)."""
        from betdata.nfl.props.live import run as run_props

        try:
            n = run_props(engine, now)
            log.info("nfl props: %d new predictions", n)
            return n
        except Exception as e:  # a prop failure must not stop the side predictions or page
            log.warning("nfl props skipped: %s: %s", type(e).__name__, e)
            return 0

    def props(self, conn: Connection, now: datetime) -> list:
        from betdata.nfl.props.grade import DESIGN_PATH, MODEL, thresholds
        from betdata.props.page import prop_rows

        if not DESIGN_PATH.exists():
            return []
        from betdata.nfl.props.live import load_design

        d = load_design()
        return prop_rows(conn, now, MODEL, DAYS, d.flag_allowed,
                         lambda m: m in d.research_only or m not in d.thresholds,
                         thresholds())  # fmt: skip

    def cards(self, conn: Connection, now: datetime) -> list[Card]:
        sid = sport_id(conn, "nfl")
        mp = model_predictions
        home_t, away_t = teams.alias("h"), teams.alias("a")
        upcoming = (
            select(games.c.game_id, games.c.start_time_utc, home_t.c.abbrev.label("home"),
                   away_t.c.abbrev.label("away"))
            .join(home_t, home_t.c.team_id == games.c.home_team_id)
            .join(away_t, away_t.c.team_id == games.c.away_team_id)
            .where(games.c.sport_id == sid, games.c.status == "scheduled",
                   games.c.start_time_utc > now,
                   games.c.start_time_utc <= now + timedelta(days=DAYS))
            .order_by(games.c.start_time_utc, games.c.game_id)
        )  # fmt: skip
        rows = conn.execute(upcoming).all()
        ids = [r.game_id for r in rows]
        if not ids:
            return []
        preds: dict = {}
        # The page's version first; a game it has no prediction for falls back to m13w (m12).
        for model in dict.fromkeys((self.page_model, self.base_model)):
            last = (
                select(mp.c.game_id, func.max(mp.c.predicted_at).label("at"))
                .where(mp.c.model == model, mp.c.game_id.in_(ids), mp.c.predicted_at <= now)
                .group_by(mp.c.game_id)
                .subquery()
            )
            for p in conn.execute(
                select(mp).join(last, and_(last.c.game_id == mp.c.game_id,
                                           last.c.at == mp.c.predicted_at))
                .where(mp.c.model == model)
            ):  # fmt: skip
                preds.setdefault(p.game_id, p)
        quotes = current_quotes(conn, ids, now)
        ml_t, calib = ml_threshold(), ml_calibration()
        out = []
        for r in rows:
            p = preds.get(r.game_id)
            q = quotes.get(r.game_id, {})
            c = Card(r.game_id, r.start_time_utc, r.home or "?", r.away or "?",
                     p.prediction_point if p else "none", p.predicted_at if p else None,
                     market_spread=q.get("spread_home"), ml_home=q.get("ml_home"),
                     ml_away=q.get("ml_away"))  # fmt: skip
            if c.ml_home is not None:
                c.market_win_home = no_vig(c.ml_home.price, c.ml_away.price)[0]
            if p is not None:
                c.model_spread_home = round(-p.pred_home_margin, 1)
                if c.market_spread is not None:
                    c.spread_edge = round(p.pred_home_margin + c.market_spread.line, 1)
                    if abs(c.spread_edge) >= THRESHOLDS["spread"]:
                        c.spread_pick = c.home if c.spread_edge > 0 else c.away
                if p.pred_home_win_prob is not None:
                    c.model_win_home = calib(p.pred_home_win_prob)
                    if c.market_win_home is not None:
                        c.ml_edge = round(100 * (c.model_win_home - c.market_win_home), 1)
                        if abs(c.ml_edge) >= ml_t:
                            c.ml_pick = c.home if c.ml_edge > 0 else c.away
                f = p.factors or {}
                c.factors = [(str(k), float(v)) for k, v in (f.get("margin") or [])[:3]]
                missing = f.get("missing") or {}
                c.missing = {c.home: missing.get("home", []), c.away: missing.get("away", [])}
            out.append(c)
        return out

    def grade(self, conn: Connection, now: datetime) -> dict[str, int]:
        run = grade_nfl(conn, now)
        out = {"inserted": run.inserted, "updated": run.updated, "pending": run.pending}
        from betdata.nfl.props.grade import grade_nfl_props

        props = grade_nfl_props(conn, now)
        out["props_inserted"], out["props_pending"] = props.inserted, props.pending
        return out

    def records(self, conn: Connection) -> list[RecordRow]:
        return [
            RecordRow(r.market, r.model_version, r.wins, r.losses, r.pushes, r.voids, r.profit,
                      r.avg_clv, r.beat_close, "% pts" if r.market == "moneyline" else "pts",
                      r.ties)
            for r in records(conn) if r.market in PAGE_MARKETS
        ]  # fmt: skip

    def thresholds(self) -> dict[str, str]:
        return {"spread": f"model and market differ by ≥ {THRESHOLDS['spread']:g} pts",
                "moneyline": f"model win chance beats the no-vig market by ≥ {ml_threshold():g} "
                             "% pts"}  # fmt: skip

    def moneyline_final_roi(self) -> float | None:
        from betdata.moneyline import MoneylineConfig
        from betdata.nfl.moneyline import ML_PATH

        return MoneylineConfig.load(ML_PATH).final_test_roi

    def bootstrap(self, engine: Engine, now: datetime, source_kwargs: dict | None = None) -> None:
        """Load NFL history from nflverse (free) into an empty database and derive the model's
        tables: what the M12 build did, season by season."""
        from betdata.config import get_settings
        from betdata.nfl.derive import derive_game_info, derive_pbp
        from betdata.nfl.workflow import data_dir
        from betdata.sources.nflverse import NflverseSource

        current = season_for("nfl", now)
        seasons = range(FIRST_HISTORY_SEASON, current + 1)
        src = NflverseSource(engine, get_settings().source("nflverse"), **(source_kwargs or {}))
        try:
            res = src.run("schedules", seasons="all")
            if res.status != "ok":
                raise RuntimeError(f"nflverse schedules: {res.status} {res.message}")
            for dataset in ("team_stats", "snaps", "injuries", "rosters", "pbp"):
                for year in seasons:
                    res = src.run(dataset, seasons=str(year))
                    log.info("bootstrap %s %s: %s %s", dataset, year, res.status, res.message)
        finally:
            src.close()
        with engine.begin() as conn:
            derive_game_info(conn)
            derive_pbp(conn, data_dir(), list(seasons))
