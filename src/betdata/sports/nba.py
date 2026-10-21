"""NBA behind the sport interface (M14): the frozen M14 model's stored predictions, grading, and
the history rebuild from free files when the data directory is lost."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, and_, func, select

from betdata.core.matching import season_for, sport_id
from betdata.db.schema import games, model_predictions, teams
from betdata.moneyline import no_vig
from betdata.nba.live import DAYS, MODEL, Design, load_fit
from betdata.quotes import current_quotes
from betdata.sports import Card, RecordRow, Sport

log = logging.getLogger(__name__)
FIRST_HISTORY_SEASON = 2015
POINT_NAMES = {"early": "early (before game-day injury statuses)",
               "morning": "morning (game-day injury report)",
               "pretip": "pre-tip (latest injury report)"}  # fmt: skip


class NbaSport(Sport):
    def _design(self) -> Design:
        return Design.load()

    def predict(self, engine: Engine, now: datetime) -> int:
        from betdata.nba.live import run_predict

        try:
            return len(run_predict(engine, now))
        except (ValueError, KeyError) as e:  # no history loaded yet
            log.warning("nba predict skipped: %s", e)
            return 0

    def cards(self, conn: Connection, now: datetime) -> list[Card]:
        sid = sport_id(conn, "nba")
        mp = model_predictions
        h, a = teams.alias("h"), teams.alias("a")
        rows = conn.execute(
            select(games.c.game_id, games.c.start_time_utc, h.c.abbrev.label("home"),
                   a.c.abbrev.label("away"))
            .join(h, h.c.team_id == games.c.home_team_id)
            .join(a, a.c.team_id == games.c.away_team_id)
            .where(games.c.sport_id == sid, games.c.status == "scheduled",
                   games.c.start_time_utc > now,
                   games.c.start_time_utc <= now + timedelta(days=DAYS))
            .order_by(games.c.start_time_utc, games.c.game_id)).all()  # fmt: skip
        ids = [r.game_id for r in rows]
        if not ids:
            return []
        last = (select(mp.c.game_id, func.max(mp.c.predicted_at).label("at"))
                .where(mp.c.model == MODEL, mp.c.game_id.in_(ids), mp.c.predicted_at <= now)
                .group_by(mp.c.game_id).subquery())  # fmt: skip
        preds = {p.game_id: p for p in conn.execute(
            select(mp).join(last, and_(last.c.game_id == mp.c.game_id,
                                       last.c.at == mp.c.predicted_at))
            .where(mp.c.model == MODEL))}  # fmt: skip
        quotes = current_quotes(conn, ids, now)
        design = self._design()
        out = []
        for r in rows:
            p, q = preds.get(r.game_id), quotes.get(r.game_id, {})
            c = Card(r.game_id, r.start_time_utc, r.home or "?", r.away or "?",
                     POINT_NAMES.get(p.prediction_point, p.prediction_point) if p else "none",
                     p.predicted_at if p else None, market_spread=q.get("spread_home"),
                     ml_home=q.get("ml_home"), ml_away=q.get("ml_away"))  # fmt: skip
            if c.ml_home is not None and c.ml_away is not None:
                c.market_win_home = no_vig(c.ml_home.price, c.ml_away.price)[0]
            if p is not None:
                c.model_spread_home = round(-p.pred_home_margin, 1)
                if c.market_spread is not None:
                    c.spread_edge = round(p.pred_home_margin + c.market_spread.line, 1)
                    if abs(c.spread_edge) >= design.spread_threshold:
                        c.spread_pick = c.home if c.spread_edge > 0 else c.away
                c.model_win_home = p.pred_home_win_prob
                if c.model_win_home is not None and c.market_win_home is not None:
                    c.ml_edge = round(100 * (c.model_win_home - c.market_win_home), 1)
                    if abs(c.ml_edge) >= design.ml_threshold:
                        c.ml_pick = c.home if c.ml_edge > 0 else c.away
                f = p.factors or {}
                c.factors = [(str(k), float(v)) for k, v in (f.get("margin") or [])[:3]]
                missing = f.get("missing") or {}
                c.missing = {c.home: missing.get("home", []), c.away: missing.get("away", [])}
                c.notes = list(f.get("notes") or [])
            out.append(c)
        return out

    def grade(self, conn: Connection, now: datetime) -> dict[str, int]:
        from betdata.nba.grade import grade_nba

        d = self._design()
        run = grade_nba(conn, now, d.spread_threshold, d.ml_threshold)
        return {"inserted": run.inserted, "updated": run.updated, "pending": run.pending}

    def records(self, conn: Connection) -> list[RecordRow]:
        from betdata.nba.grade import records

        return [RecordRow(r.market, r.model_version, r.wins, r.losses, r.pushes, r.voids,
                          r.profit, r.avg_clv, r.beat_close,
                          "% pts" if r.market == "moneyline" else "pts")
                for r in records(conn)]  # fmt: skip

    def thresholds(self) -> dict[str, str]:
        d = self._design()
        return {"spread": f"model and market differ by ≥ {d.spread_threshold:g} pts",
                "moneyline": f"model win chance beats the no-vig market by ≥ "
                             f"{d.ml_threshold:g} % pts"}  # fmt: skip

    def page_version(self) -> str | None:
        return self.version()

    def props(self, conn: Connection, now: datetime) -> list:
        from betdata.nba.props.grade import DESIGN_PATH, MODEL, thresholds
        from betdata.props.page import prop_rows

        if not DESIGN_PATH.exists():
            return []
        from betdata.nba.props.live import PropDesign

        d = PropDesign.load()
        return prop_rows(conn, now, MODEL, DAYS, d.flag_allowed,
                         lambda m: m in d.research_only or m not in d.thresholds,
                         thresholds())  # fmt: skip

    def moneyline_final_roi(self) -> float | None:
        return self._design().ml_final_test_roi

    def version(self) -> str:
        return load_fit(self._design())[2]

    def bootstrap(self, engine: Engine, now: datetime, source_kwargs: dict | None = None) -> None:
        """Rebuild NBA history from free files into an empty database: every season's schedule
        and box scores (sportsdataverse), then this season's games so far (ESPN)."""
        from betdata.config import get_settings
        from betdata.sources.espn_nba import EspnNbaSource
        from betdata.sources.sportsdataverse import SportsDataverseSource

        settings = get_settings()
        current = season_for("nba", now)
        sdv = SportsDataverseSource(engine, settings.source("sportsdataverse"),
                                    **(source_kwargs or {}))  # fmt: skip
        try:
            for year in range(FIRST_HISTORY_SEASON, current + 1):
                res = sdv.run("schedule", sport="nba", seasons=str(year))
                log.info("bootstrap nba schedule %s: %s %s", year, res.status, res.message)
                if year < current:
                    res = sdv.run("player_box", seasons=str(year))
                    log.info("bootstrap nba box %s: %s %s", year, res.status, res.message)
        finally:
            sdv.close()
        espn = EspnNbaSource(engine, settings.source("espn_nba"), **(source_kwargs or {}))
        try:
            start = datetime(current, 10, 1, tzinfo=now.tzinfo)
            day = start
            while day < now:  # this season so far: scores, then box scores
                end = min(now, day + timedelta(days=30))
                espn.run("scoreboard", dates=f"{day:%Y%m%d}-{end:%Y%m%d}")
                day = end + timedelta(days=1)
            espn.run("boxscores", days=(now - start).days + 1)
        finally:
            espn.close()
