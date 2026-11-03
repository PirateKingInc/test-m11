"""compare.py valid|test: nba15 (sides) and nbap15 (props) three checks on their own picks,
plus the closing line as a 'model' (calibration of the close; checks 2-3 trivially fail)."""
import json, pickle, sys, math, numpy as np, pandas as pd
from betdata.nba.engine import evaluate as V
from betdata.checks import Pick, three_checks
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
mode=sys.argv[1]
n15=pickle.load(open(W+'nba15_preds.pkl','rb'))['valid' if mode=='valid' else 'test']
n15=n15[n15.point=='pretip']
def ck(ps):
    c=three_checks(ps); return {'n':c.n,'status':c.status,'calibration':vars(c.calibration),'brier':vars(c.brier),'ev':vars(c.ev)}
out={}
# nba15 spreads: edge >= 6 at the close, -110; fair = Phi((pred+L)/sigma)
ps=[];rec=[]
for r in n15.itertuples():
    L=r.close_spread_home
    if not np.isfinite(L): continue
    e=r.pred+L
    if abs(e)<6: continue
    x=r.margin+L
    if x==0: continue
    pc=0.5*(1+math.erf((e)/r.sigma/math.sqrt(2)))
    home=e>0
    ps.append(Pick(pc if home else 1-pc, 0.5, int((x>0)==home), 1+100/110))
out['nba15_spread_picks_at_close']=ck(ps)
ps=[]
for r in n15.itertuples():
    a,b=r.close_ml_home,r.close_ml_away
    if not (np.isfinite(a) and np.isfinite(b)): continue
    nv=float(V.novig(a,b)); ph=r.p_home
    for side,p_,q,pr in ((1,ph,nv,a),(0,1-ph,1-nv,b)):
        if p_-q>=0.05:
            ps.append(Pick(p_,q,int((r.margin>0)==bool(side)),float(V.dec(pr))))
out['nba15_ml_picks_at_close']=ck(ps)
# closing line itself (all games, home side): calibration only meaningful
ps=[Pick(float(V.novig(r.close_ml_home,r.close_ml_away)),float(V.novig(r.close_ml_home,r.close_ml_away)),int(r.margin>0),float(V.dec(r.close_ml_home))) for r in n15.itertuples() if np.isfinite(r.close_ml_home) and np.isfinite(r.close_ml_away)]
out['closing_ml_home_side']=ck(ps)
# margin/total MAE same games
c=n15.close_spread_home.notna()
out['nba15_margin_mae']=float((n15.pred-n15.margin).abs().mean()); out['nba15_total_mae']=float((n15.pred_total-n15.total).abs().mean())
out['close_margin_mae']=float((-n15.close_spread_home[c]-n15.margin[c]).abs().mean())
ct=n15.close_total.notna(); out['close_total_mae']=float((n15.close_total[ct]-n15.total[ct]).abs().mean())
# nbap15 at opening lines, its thresholds; 'close' = opening no-vig
p15=pickle.load(open(W+'nbap15_preds.pkl','rb'))['valid' if mode=='valid' else 'test']
allp=pd.concat(p15)
out['nbap15_props']={}
for mk,d in allp.groupby('market'):
    th=d.threshold.iloc[0]
    d=d[d.over.notna()]
    res={'rows':int(len(d)),'brier_model':float(((d.p-d.over)**2).mean()),'brier_novig':float(((d.nv-d.over)**2).mean())}
    if th==th and th is not None:
        b=d[d.edge.abs()>=th]
        ps=[Pick(r.p if r.edge>0 else 1-r.p, r.nv if r.edge>0 else 1-r.nv, int((r.over==1)==(r.edge>0)), float(V.dec(r.over_price if r.edge>0 else r.under_price))) for r in b.itertuples()]
        res['picks']=ck(ps)
        ru=b[(d.market=='rebounds') & (b.edge<0)] if mk=='rebounds' else None
        if ru is not None:
            res['rebound_unders']=ck([Pick(1-r.p,1-r.nv,int(r.over==0),float(V.dec(r.under_price))) for r in ru.itertuples()])
    out['nbap15_props'][mk]=res
json.dump(out,open(f'reports/nba/m20_compare_{mode}.json','w'),indent=1,default=float)
print(json.dumps(out,indent=1,default=float)[:5000])
