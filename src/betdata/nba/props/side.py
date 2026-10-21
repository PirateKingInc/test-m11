"""The M14 side model's margin and total per game and point, as a prop model input.

History: the frozen M14 design refitted the way M14 scored it, so no season's side input comes
from a fit that saw it: fitted on the training seasons for training and validation seasons, on
training + validation for the test seasons (M14's final test fit). Live: the M14 predictions the
hourly runner stores in model_predictions (model "nba")."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import model_predictions
from betdata.nba.live import MODEL, Design
from betdata.nba.pipeline import TEST, TRAIN, VALIDATE, columns, fit_point
from betdata.nba.points import POINTS


def side_history(rows: pd.DataFrame, out: pd.DataFrame, design: Design | None = None,
                 ) -> pd.DataFrame:  # fmt: skip
    d = design or Design.load()
    m_cols, t_cols = columns(rows, d.groups)
    parts = []
    for train, seasons in ((TRAIN, (*TRAIN, *VALIDATE)), ((*TRAIN, *VALIDATE), TEST)):
        for p in POINTS:
            f = fit_point(rows, out, p, train, m_cols, t_cols, d.lam_m, d.lam_t)
            r = rows[(rows.point == p) & rows.season.isin(seasons)]
            Xm = r[list(f.margin.names)].fillna(0.0).to_numpy(dtype=float)
            Xt = r[list(f.total.names)].fillna(0.0).to_numpy(dtype=float)
            parts.append(pd.DataFrame({"game_id": r.game_id.to_numpy(), "point": p,
                                       "pred_margin": f.margin.predict(Xm),
                                       "pred_total": f.total.predict(Xt)}))  # fmt: skip
    return pd.concat(parts, ignore_index=True)


def side_live(conn: Connection, game_ids, at) -> pd.DataFrame:
    """The latest stored M14 prediction per game known by `at` (any point; a prop prediction
    at a point uses the side prediction made at that run)."""
    mp = model_predictions
    df = pd.read_sql(select(mp.c.game_id, mp.c.prediction_point, mp.c.predicted_at,
                            mp.c.pred_home_margin, mp.c.pred_total)
                     .where(mp.c.model == MODEL, mp.c.game_id.in_(list(game_ids)),
                            mp.c.predicted_at <= at), conn)  # fmt: skip
    df = df.sort_values("predicted_at").groupby("game_id").last().reset_index()
    return df.rename(columns={"pred_home_margin": "pred_margin", "pred_total": "pred_total"})
