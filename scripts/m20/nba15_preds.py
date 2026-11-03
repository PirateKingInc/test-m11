import json, pickle, numpy as np
from betdata.nba.select15 import rows15
from betdata.nba.pipeline import TEST, TRAIN, VALIDATE, columns
from betdata.nba.selection import fits_for, market_frame, predictions, win_probs
d=json.load(open('config/nba15_model.json'))
store,out,rows,_=rows15()
m,t=columns(rows,d['groups'])
cal=(d['calibration']['slope'],d['calibration']['intercept'])
res={}
for name,fit_s,pred_s in (('valid',TRAIN,VALIDATE),('test',TRAIN+VALIDATE,TEST)):
    fits=fits_for(rows,out,m,t,fit_s,d['lam_m'],d['lam_t'])
    pr=predictions(rows,out,fits,pred_s).join(market_frame(store,rows.game_id.unique()),on='game_id')
    pr['p_home']=win_probs(pr,cal)
    res[name]=pr
    for pt in ('morning','pretip'):
        s=pr[pr.point==pt]; print(name,pt,'margin MAE',round(np.abs(s.pred-s.margin).mean(),3),'total MAE',round(np.abs(s.pred_total-s.total).mean(),3), len(s))
pickle.dump(res,open('/tmp/claude-0/-home-user-test-m11/6186d2e3-8ed2-59f7-a6ed-0aff62948dc7/scratchpad/w/nba15_preds.pkl','wb'))
