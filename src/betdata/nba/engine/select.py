"""M20 B3-B6 selection on validation (2021-22, 2022-23), pretip, every player who played:
the competitive-minutes definition, the outlier weights (one flag at a time), and the recency
half-lives for stable players and players with a role change (minutes, then rates by usage).
Constants (paddings, play-by-minutes, multipliers, script curve) come from training seasons.
Each step logs every candidate; the simplest wins ties within 0.1% (MODEL.md "M20")."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from betdata.nba.engine import fitting as F
from betdata.nba.engine.states import FLAG_ORDER, H_GRID, Params

TRAIN = tuple(range(2015, 2021))
VALID = (2021, 2022)
TIE = 0.001
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def metrics(inp: F.Inputs, d) -> dict:
    act = F.actuals(inp.p, d)
    ok = act.notna().to_numpy()
    out = {"n": int(ok.sum()),
           "minutes": float(np.abs(d.minutes.to_numpy()[ok] - act.to_numpy()[ok]).mean())}
    use_a = (F.actuals(inp.p, d, "fga") + 0.44 * F.actuals(inp.p, d, "fta")
             + F.actuals(inp.p, d, "tov")).to_numpy()  # fmt: skip
    use_p = (d.fg2a_m + d.fg3a_m + 0.44 * d.fta_m + d.tov_m).to_numpy()
    out["usage"] = float(np.abs(use_p[ok] - use_a[ok]).mean())
    for s in ("pts", "reb", "ast", "fg3m", "pra"):
        a = F.actuals(inp.p, d, s).to_numpy()
        out[s] = float(np.abs(d[f"{s}_m"].to_numpy()[ok] - a[ok]).mean())
    return out


class Selector:
    def __init__(self, inp: F.Inputs, pads: dict, play: dict, log=print):
        self.inp, self.pads, self.play, self.log = inp, pads, play, log
        g = inp.g
        self.valid = g[g.season.isin(VALID) & (g.status == "final")].game_id
        self.steps: list[dict] = []
        self._consts: dict = {}

    def engine(self, prm: Params) -> F.Engine:
        fit = F.base_fit(self.inp, self.pads, prm, self.play, TRAIN)
        if prm.cm not in self._consts:  # training constants once per definition
            eng = F.Engine(self.inp, fit)
            F.fit_play_by_minutes(eng, TRAIN)
            F.fit_multipliers(eng, TRAIN)
            self._consts[prm.cm] = (fit.play_by_minutes, dict(fit.b2b), fit.ret)
        fit.play_by_minutes, fit.b2b, fit.ret = copy.deepcopy(self._consts[prm.cm])
        return F.Engine(self.inp, fit)

    def score(self, prm: Params) -> dict:
        eng = self.engine(prm)
        m = metrics(self.inp, eng.predict(self.valid))
        return m

    def step(self, name: str, base: Params, options: list, target: str = "minutes") -> Params:
        rows, best, best_v = [], None, None
        for label, prm in options:
            m = self.score(prm)
            rows.append({"option": label, **m})
            self.log(f"  {name} {label}: {target} {m[target]:.4f} (minutes {m['minutes']:.4f})")
            # options are listed simplest first: a later one must beat by more than TIE
            if best_v is None or m[target] < best_v * (1 - TIE):
                best, best_v = (label, prm), m[target]
        self.steps.append({"step": name, "target": target, "rows": rows, "kept": best[0]})
        self.log(f"{name}: kept {best[0]}")
        return best[1]

    def run(self) -> Params:
        prm = Params()
        prm = self.step("B3 competitive minutes", prm,
                        [(cm, Params(cm=cm)) for cm in ("cm1", "cm2", "cm3")])
        for flag in FLAG_ORDER:  # B4, one flag at a time; 1.0 (no down-weight) is simplest
            opts = []
            for w in (1.0, *[x for x in WEIGHTS if x != 1.0]):
                q = copy.deepcopy(prm)
                q.w[flag] = w
                opts.append((f"{flag}={w:g}", q))
            prm = self.step(f"B4 outlier weight {flag}", prm, opts)
        hs = (10, *[h for h in H_GRID if h != 10])  # the default first
        prm = self.step("B5 minutes half-life, stable", prm,
                        [(f"h={h}", _with(prm, h_min_stable=h, h_min_change=h)) for h in hs])
        prm = self.step("B5 minutes half-life, role change", prm,
                        [(f"h={h}", _with(prm, h_min_change=h))
                         for h in (prm.h_min_stable, *[x for x in H_GRID
                                                       if x != prm.h_min_stable])])  # fmt: skip
        hr = (20, *[h for h in H_GRID if h != 20])
        prm = self.step("B5 rate half-life, stable", prm,
                        [(f"h={h}", _with(prm, h_rate_stable=h, h_rate_change=h)) for h in hr],
                        target="usage")  # fmt: skip
        prm = self.step("B5 rate half-life, role change", prm,
                        [(f"h={h}", _with(prm, h_rate_change=h))
                         for h in (prm.h_rate_stable, *[x for x in H_GRID
                                                        if x != prm.h_rate_stable])],
                        target="usage")  # fmt: skip
        self.params = prm
        return prm

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"train": TRAIN, "valid": VALID, "point": "pretip",
                                    "steps": self.steps, "params": asdict(self.params)},
                                   indent=1))  # fmt: skip


def _with(prm: Params, **kw) -> Params:
    q = copy.deepcopy(prm)
    for k, v in kw.items():
        setattr(q, k, v)
    return q
