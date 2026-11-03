"""refit.py <last season> <out.json>: constants (pads, play-by-minutes, multipliers, sim) on
2015..last with the frozen validation design."""
import pickle, json, sys
from betdata.nba.engine import fitting as F
from betdata.nba.engine.stability import stability, paddings
from betdata.nba.engine.states import Params
from betdata.nba.engine import derive as D
W = 'data/m20/'  # the working folder of pickles (scripts/m20/README.md)
last, out = int(sys.argv[1]), sys.argv[2]
g,p,tg=pickle.load(open(W+'data.pkl','rb')); inj=pickle.load(open(W+'inj.pkl','rb'))
seasons=range(2015,last+1)
pads=paddings(stability(p,tg,seasons,'cm1'))
play=json.load(open('config/fits/nba_fit_2015_2025.json'))['play_prob']
prm=Params(**json.load(open('reports/nba/m20_selection.json'))['params'])
inp=F.Inputs(g,p,tg,inj)
fit=F.base_fit(inp,pads,prm,play,seasons)
fit.sim={'script': False, 'addons': [], 'era_priors': True, 'era_team': True}
eng=F.Engine(inp,fit,addons=[])
F.fit_play_by_minutes(eng,seasons); F.fit_multipliers(eng,seasons)
fit.sim={'script': False, 'addons': [], 'era_priors': True, 'era_team': True}
F.fit_sim(eng,seasons,n_games=250,n_sims=2000,log=lambda s: print(s,flush=True))
fit.sim.update({'script': False, 'addons': [], 'era_priors': True, 'era_team': True})
json.dump(fit.to_json(),open(out,'w'),indent=1,default=float)
print('done')
