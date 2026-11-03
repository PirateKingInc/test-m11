"""nbap15 priced at the cleaned opening lines (pretip): validation 2022 (fit on train, the tune
calibration), test 2023-25 (fit on 2015-22, as its final test). Row-level, with bets marked."""
import json, pickle
import numpy as np
from betdata.nba.cli import _engine
from betdata.nba.props import model as M
from betdata.nba.props import select as S
f=S.usable(S.history_features())
tune=json.loads(open('reports/nba/props_tune.json').read())
_,e=_engine()
with e.connect() as c: lines=S.history_lines(c)
out={}
for name, fit_s, pred_s in (('valid', S.TRAIN, (2022,)), ('test', (*S.TRAIN, *S.VALIDATE), S.TEST)):
    mod=M.fit(f[f.season.isin(fit_s)])
    for mk,r in tune['markets'].items():
        if r.get('calibration_applied'): mod.calib[mk]=tuple(r['platt'])
    parts=[]
    for mk in S.MARKETS:
        d=S.priced(mod, f[f.season.isin(pred_s)&(f.point=='pretip')], lines, mk, 'open')
        if d.empty: continue
        th=(tune['markets'].get(mk) or {}).get('threshold')
        d['market']=mk; d['threshold']=th
        parts.append(d[['game_id','player_id','market','season','line','over_price','under_price','p','nv','edge','over','mu','threshold']])
    out[name]=parts
    print(name, sum(len(x) for x in parts), flush=True)
pickle.dump(out,open('/tmp/claude-0/-home-user-test-m11/6186d2e3-8ed2-59f7-a6ed-0aff62948dc7/scratchpad/w/nbap15_preds.pkl','wb'))
