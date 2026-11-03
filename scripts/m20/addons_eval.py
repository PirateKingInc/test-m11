import pickle, json, time, numpy as np
from betdata.nba.engine import fitting as F, addons as A
from betdata.nba.engine.project import Fit
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
g,p,tg=pickle.load(open(W+'data.pkl','rb')); inj=pickle.load(open(W+'inj.pkl','rb'))
fit=Fit.from_json(json.load(open(W+'fit_train.json')))
inp=F.Inputs(g,p,tg,inj)
base=F.Engine(inp,fit,addons=[])
F.fit_d2(base, range(2015,2021))

val=g[g.season.isin([2021,2022])&(g.status=='final')].game_id
def mae(d,col,stat,mask=None):
    a=F.actuals(p,d,stat); ok=a.notna().to_numpy()
    if mask is not None: ok = ok & mask
    return float(np.abs(d[col].to_numpy()[ok]-a.to_numpy()[ok]).mean()), int(ok.sum())
res={}
d0=base.predict(val); res['core']={s:mae(d0,f'{s}_m',s) for s in ['pts','reb','ast','fg3m','pra']}; res['core']['minutes']=mae(d0,'minutes','minutes')
print('core',res['core'],flush=True)
# D4: without the script term
dn=base.predict(val,script=False); res['d4_removed']={'minutes':mae(dn,'minutes','minutes'),'pts':mae(dn,'pts_m','pts'),'reb':mae(dn,'reb_m','reb'),'ast':mae(dn,'ast_m','ast')}
print('d4 removed',res['d4_removed'],flush=True)
for ad in ['d2','d3','d1']:
    e=F.Engine(inp,fit,states=base.states,addons=[ad]); t=time.time()
    d=e.predict(val)
    r={s:mae(d,f'{s}_m',s) for s in ['pts','reb','ast','fg3m','pra']}
    if ad=='d1':
        c=e.frame(val); mask=None
        # rows a D1 factor touched
        cb=base.frame(val)
        ch=(np.abs(c.ast.to_numpy()-cb.ast.to_numpy())>1e-12)|(np.abs(c.fg2a.to_numpy()-cb.fg2a.to_numpy())>1e-12)
        r['touched']=int(ch.sum())
        for s in ['ast','reb','pts']:
            r[f'{s}_touched']=mae(d,f'{s}_m',s,ch); r[f'{s}_touched_core']=mae(d0,f'{s}_m',s,ch)
        ua=(F.actuals(p,d,'fga')+0.44*F.actuals(p,d,'fta')+F.actuals(p,d,'tov')).to_numpy()
        for nm,dd in (('engine',d),('core',d0)):
            up=(dd.fg2a_m+dd.fg3a_m+0.44*dd.fta_m+dd.tov_m).to_numpy(); ok=np.isfinite(ua)&ch
            r[f'usage_touched_{nm}']=float(np.abs(up[ok]-ua[ok]).mean())
    res[ad]=r; print(ad,r,time.time()-t,flush=True)
json.dump(res,open('reports/nba/m20_addons.json','w'),indent=1)
