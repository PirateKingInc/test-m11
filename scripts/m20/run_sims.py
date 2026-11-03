"""Usage: run_sims.py <fit.json> <seasons comma> <out.pkl> [n_sims]"""
import pickle, json, sys, time
import pandas as pd
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.nba.engine import fitting as F, evaluate as V
from betdata.nba.engine.project import Fit
from betdata.nba.props.select import history_lines
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
fitp, seasons, out = sys.argv[1], [int(x) for x in sys.argv[2].split(',')], sys.argv[3]
n = int(sys.argv[4]) if len(sys.argv) > 4 else 10000
g,p,tg=pickle.load(open(W+'data.pkl','rb')); inj=pickle.load(open(W+'inj.pkl','rb'))
fit=Fit.from_json(json.load(open(fitp)))
eng=F.Engine(F.Inputs(g,p,tg,inj),fit)
e=make_engine(get_settings().db_url)
with e.connect() as c:
    side=V.side_lines(c,g)
    pl=history_lines(c)
pl=pl.rename(columns={'open_line':'line','open_over_price':'over_price','open_under_price':'under_price'}).dropna(subset=['line'])
gids=g[g.season.isin(seasons)&(g.status=='final')].game_id
t=time.time()
games,props,d=V.simulate(eng,gids,pl[pl.game_id.isin(set(gids))],side,n,log=lambda s: print(s,time.time()-t,flush=True))
dm=eng.predict(gids,("morning",))
pickle.dump({'games':games,'props':props,'means':d,'means_morning':dm,'side':side,'plines':pl},open(out,'wb'))
print('done',time.time()-t)
