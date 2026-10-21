"""The side model's margin and total, and the M13 weather, per game and point: prop inputs.

History: the frozen m13w design (config/nfl_model_wx.json; its total model is M12's) refitted so
that no season's side input comes from a fit that saw it: each training season (2013-2019) from
a fit on the other training seasons, validation seasons (2020-2022) from the fit on 2013-2019
(as M12 scored them), test seasons (2023-2025) from the fit on 2013-2022 (M12's final test fit).
Weather: the `wx` group's wind above 10 km/h and rain (outdoor share; zero indoors), as of the
point. Live: the latest stored m13w prediction known at the run (m12 when there is none), with
the weather features stored with it.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import model_predictions
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.selection import fit_design, predict_rows

SIDE_MODELS = ("m13w", "m12")


def side_history(rows: pd.DataFrame, out: pd.DataFrame, design) -> pd.DataFrame:
    plans = [(tuple(s for s in TRAIN if s != held), (held,)) for held in TRAIN]
    plans += [(TRAIN, VALIDATE), ((*TRAIN, *VALIDATE), TEST)]
    parts = []
    for fit_seasons, seasons in plans:
        fits = fit_design(rows, out, design, fit_seasons)
        r = rows[rows.season.isin(seasons)]
        p = predict_rows(r, fits).rename(columns={"margin": "pred_margin", "total": "pred_total"})
        wx = r.set_index(["game_id", "point"])
        k = pd.MultiIndex.from_frame(p[["game_id", "point"]])
        p["wind"] = wx.t_wx_wind.reindex(k).to_numpy() if "t_wx_wind" in wx else 0.0
        p["rain"] = wx.t_wx_rain.reindex(k).to_numpy() if "t_wx_rain" in wx else 0.0
        parts.append(p[["game_id", "point", "pred_margin", "pred_total", "wind", "rain"]])
    return pd.concat(parts, ignore_index=True)


def side_live(conn: Connection, game_ids, at) -> pd.DataFrame:
    """Per game: the latest stored side prediction known by `at` (m13w, else m12)."""
    mp = model_predictions
    df = pd.read_sql(select(mp.c.game_id, mp.c.model, mp.c.predicted_at, mp.c.pred_home_margin,
                            mp.c.pred_total, mp.c.features)
                     .where(mp.c.model.in_(SIDE_MODELS), mp.c.game_id.in_(list(game_ids)),
                            mp.c.predicted_at <= at), conn)  # fmt: skip
    cols = ["game_id", "pred_margin", "pred_total", "wind", "rain"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["rank"] = df.model.map({m: i for i, m in enumerate(SIDE_MODELS)})
    df = df.sort_values(["rank", "predicted_at"], ascending=[True, False])
    df = df.drop_duplicates("game_id").reset_index(drop=True)
    feats = [f if isinstance(f, dict) else {} for f in df.features]
    df["wind"] = [float(f.get("t_wx_wind", 0.0)) for f in feats]
    df["rain"] = [float(f.get("t_wx_rain", 0.0)) for f in feats]
    df = df.rename(columns={"pred_home_margin": "pred_margin"})
    return df[cols]
