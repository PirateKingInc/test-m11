import pickle, json, numpy as np
from betdata.nba.engine import fitting as F, addons as A
from betdata.nba.engine.project import Fit
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
g,p,tg=pickle.load(open(W+'data.pkl','rb')); inj=pickle.load(open(W+'inj.pkl','rb'))
fit=Fit.from_json(json.load(open(W+'fit_train_v.json')))
inp=F.Inputs(g,p,tg,inj)
val=g[g.season.isin([2021,2022])&(g.status=='final')].game_id
base=F.Engine(inp,fit,addons=[]); cb=base.frame(val); d0=base.means(cb)
A.D1_STATS=("ast","oreb","dreb")
e=F.Engine(inp,fit,states=base.states,addons=['d1']); c=e.frame(val); d=e.means(c)
ch=(np.abs(c.ast.to_numpy()-cb.ast.to_numpy())>1e-12)
res={}
for s in ['ast','reb','pts']:
    a=F.actuals(p,d,s).to_numpy(); ok=np.isfinite(a)
    res[s]=(float(np.abs(d[s+'_m'].to_numpy()[ok&ch]-a[ok&ch]).mean()), float(np.abs(d0[s+'_m'].to_numpy()[ok&ch]-a[ok&ch]).mean()), float(np.abs(d[s+'_m'].to_numpy()[ok]-a[ok]).mean()), float(np.abs(d0[s+'_m'].to_numpy()[ok]-a[ok]).mean()))
print(res)
r=json.load(open('reports/nba/m20_addons.json')); r['d1_adjustment1_ast_reb_only']={k:{'touched_d1':v[0],'touched_core':v[1],'all_d1':v[2],'all_core':v[3]} for k,v in res.items()}
json.dump(r,open('reports/nba/m20_addons.json','w'),indent=1)
