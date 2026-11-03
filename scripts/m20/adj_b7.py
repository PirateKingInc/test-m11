import pickle, json, numpy as np, pandas as pd, copy
from betdata.nba.engine import fitting as F, project as P
from betdata.nba.engine.project import Fit
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
g,p,tg=pickle.load(open(W+'data.pkl','rb')); inj=pickle.load(open(W+'inj.pkl','rb'))
base=Fit.from_json(json.load(open(W+'fit_train_v.json')))
inp=F.Inputs(g,p,tg,inj)
val=g[g.season.isin([2021,2022])&(g.status=='final')].game_id
h=tg[tg.is_home].set_index('game_id'); res_=pd.DataFrame({'margin':h.pts-h.o_pts,'total':h.pts+h.o_pts})
states=None; rows=[]
rows=json.load(open('reports/nba/m20_b7_adjustments.json'))
for name,era,st in (('adj3 era team priors (+adj1)',True,1.0),):
    f=copy.deepcopy(base); f.sim['era_priors']=era; f.sim['stretch']=st; f.sim['era_team']=True
    e=F.Engine(inp,f,states=states,addons=[]); states=e.states
    d=e.predict(val); gm=P.game_means(d).reset_index(); gm=gm[gm.point=='pretip'].set_index('game_id').join(res_)
    m=gm.home_pts-gm.away_pts; t=gm.home_pts+gm.away_pts
    a=F.actuals(p,d); ok=a.notna()
    r={'step':name,'margin_mae':float((m-gm.margin).abs().mean()),'total_mae':float((t-gm.total).abs().mean()),'total_bias':float((t-gm.total).mean()),'margin_sd':float(m.std())}
    for s in ['pts','reb','ast','fg3m']:
        aa=F.actuals(p,d,s); r[s]=float((d[s+'_m'][ok]-aa[ok]).abs().mean())
    rows.append(r); print(r,flush=True)
json.dump(rows,open('reports/nba/m20_b7_adjustments.json','w'),indent=1)
