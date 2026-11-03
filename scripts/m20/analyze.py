"""analyze.py valid|test : Part C/E/F tables. valid fits the design; test applies it (once)."""
import json, pickle, sys, numpy as np, pandas as pd
from betdata.nba.engine import evaluate as V
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
mode=sys.argv[1]
S=pickle.load(open(W+('valid_sims.pkl' if mode=='valid' else 'test_sims.pkl'),'rb'))
g,p,tg=pickle.load(open(W+'data.pkl','rb'))
h=tg[tg.is_home].set_index('game_id')
res_=pd.DataFrame({'margin':h.pts-h.o_pts,'total':h.pts+h.o_pts})
games=S['games']; side=S['side']
at='c'
gv=V.game_rows(games, side, res_, 'c')
pa=p[p.played][['game_id','player_id','pts','reb','ast','fg3m','pra']]
pv=V.prop_rows(S['props'], S['plines'], pa)
out={'mode':mode,'n_games':int(len(games)),'checks_ok_all_games':bool(games.checks_ok.all())}
# accuracy
gm=games.set_index('game_id').join(res_).join(side)
out['margin_mae']=float((gm.mu_margin-gm.margin).abs().mean()); out['total_mae']=float((gm.mu_total-gm.total).abs().mean())
c=gm.c_spread.notna(); out['close_margin_mae']=float((-gm.c_spread[c]-gm.margin[c]).abs().mean()); out['engine_margin_mae_same']=float((gm.mu_margin[c]-gm.margin[c]).abs().mean())
ct=gm.c_total.notna(); out['close_total_mae']=float((gm.c_total[ct]-gm.total[ct]).abs().mean()); out['engine_total_mae_same']=float((gm.mu_total[ct]-gm.total[ct]).abs().mean())
d=S['means']; dm=S['means_morning']
from betdata.nba.engine.fitting import actuals
for nm,dd in (('pretip',d),('morning',dm)):
    a=actuals(p,dd); ok=a.notna()
    out[f'player_mae_{nm}']={'minutes':float((dd.minutes[ok]-a[ok]).abs().mean())}
    for s in ['pts','reb','ast','fg3m','pra']:
        aa=actuals(p,dd,s); out[f'player_mae_{nm}'][s]=float((dd[s+'_m'][ok]-aa[ok]).abs().mean())
# prop accuracy at lines
out['props_at_lines']={}
for m in V.PROP_MARKETS:
    x=pv[pv.market==m]
    if len(x)==0: continue
    out['props_at_lines'][m]={'n':int(len(x)),'mae_engine_median':float((x['median']-x.actual).abs().mean()),'mae_engine_mean':float((x['mean']-x.actual).abs().mean()),'mae_line':float((x.line-x.actual).abs().mean()),'brier_engine':float(((x.p_eng-x.y)**2).mean()),'brier_novig':float(((x.p_mkt-x.y)**2).mean()),'logloss_engine':float(V.logloss(x.p_eng,x.y).mean()),'logloss_novig':float(V.logloss(x.p_mkt,x.y).mean())}
out['games_at_lines']={}
for m in V.GAME_MARKETS:
    x=gv[gv.market==m]
    out['games_at_lines'][m]={'n':int(len(x)),'brier_engine':float(((x.p_eng-x.y)**2).mean()),'brier_market':float(((x.p_mkt-x.y)**2).mean()),'logloss_engine':float(V.logloss(x.p_eng,x.y).mean()),'logloss_market':float(V.logloss(x.p_mkt,x.y).mean()),'calib_engine':V.calibrate(x.p_eng.to_numpy(),x.y.to_numpy())[1] if len(x)>50 else None}
if mode=='valid':
    design=V.fit_design(gv,pv)
    json.dump(design,open('reports/nba/m20_design.json','w'),indent=1,default=float)
else:
    design=json.load(open('reports/nba/m20_design.json'))
out['design']={k:design[k] for k in ('w','platt','theta','validated')}
gp=V.scored_picks(gv,design,'game'); pp=V.scored_picks(pv,design,'prop')
out['engine_game_picks']=V.checks_of(gp) if len(gp) else None
out['engine_prop_picks']=V.checks_of(pp) if len(pp) else None
out['engine_picks_by_market']={m:V.checks_of(x) for m,x in pd.concat([gp,pp]).groupby('market')} if len(gp)+len(pp) else {}
# rebound unders and primary-ballhandler assists
if len(pp):
    ru=pp[(pp.market=='rebounds')&(~pp.side_a)]
    out['rebound_unders']=V.checks_of(ru) if len(ru) else None
    # primary ballhandlers: assist share of team assists >= 25% over last 10 (as of: shift)
    pl=p[p.played].sort_values('tip').copy()
    ta=pl.groupby(['game_id','team_id']).ast.transform('sum')
    pl['sh']=pl.ast/ta.replace(0,np.nan)
    pl['sh10']=pl.groupby('player_id').sh.transform(lambda s:s.shift(1).rolling(10,3).mean())
    key=pl.set_index(['game_id','player_id']).sh10
    key=key[~key.index.duplicated()]
    def pb(df):
        k=key.reindex(pd.MultiIndex.from_arrays([df.game_id.astype(int), df.player_id.astype(str)])).to_numpy()
        return df[(df.market=='assists')&(df.line>=5.5)&(np.nan_to_num(k)>=0.25)]
    a5=pb(pp); out['primary_ballhandler_assists']=V.checks_of(a5) if len(a5) else None
    allp=pb(pv); out['primary_ballhandler_assist_lines']={'n':int(len(allp)),'over_rate':float(allp.y.mean()) if len(allp) else None,'engine_p_over':float(allp.p_eng.mean()) if len(allp) else None,'novig_over':float(allp.p_mkt.mean()) if len(allp) else None}
pickle.dump({'gv':gv,'pv':pv,'gp':gp,'pp':pp},open(W+f'analysis_{mode}.pkl','wb'))
json.dump(out,open(f'reports/nba/m20_{"validation" if mode=="valid" else "final_test"}.json','w'),indent=1,default=float)
print(json.dumps({k:v for k,v in out.items() if k not in ('design',)},indent=1,default=float)[:6000])
print(json.dumps(out['design'],default=float))
# --- game picks at the OPENING line, graded against the close (test seasons have openers) ---
from scipy.stats import norm
go=V.game_rows(games, side, res_, 'o')
if len(go):
    sd=side.reindex(go.game_id)
    cl=np.where(go.market=='spreads', norm.cdf((go.line.to_numpy()-sd.c_spread.to_numpy())/12.0),
       np.where(go.market=='totals', norm.cdf((sd.c_total.to_numpy()-go.line.to_numpy())/18.0),
                V.novig(sd.c_ml_home.to_numpy(), sd.c_ml_away.to_numpy())))
    go=go.assign(p_close=cl)
    go=go[np.isfinite(go.p_close)]
    go2=go.copy(); 
    # the market at pick time is the opening no-vig (spreads/totals -110: 0.5)
    gpo=V.scored_picks(go2,design,'game',close=go2.p_close.to_numpy())
    out['engine_game_picks_at_open_vs_close']=V.checks_of(gpo) if len(gpo) else None
    out['engine_game_picks_at_open_by_market']={m:V.checks_of(x) for m,x in gpo.groupby('market')} if len(gpo) else {}
    json.dump(out,open(f'reports/nba/m20_{"validation" if mode=="valid" else "final_test"}.json','w'),indent=1,default=float)
    print('AT OPEN', json.dumps(out['engine_game_picks_at_open_vs_close'],default=float)[:1500])
