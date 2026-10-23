"""Weekly recency iteration; previously examined periods, not a new holdout."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .learned_returns import FEATURES, evaluate, load, nonoverlap
from .managed_trend import epoch


def fit_week(rows,kind,cutoff):
    cal_start=cutoff-14*86400
    train=rows[(rows.available_at>=cutoff-104*86400)&(rows.available_at<cal_start)&
               (rows.label_end48<cal_start)&rows.label48.notna()]
    calibration=nonoverlap(rows[(rows.available_at>=cal_start)&(rows.label_end48<cutoff)&rows.label48.notna()],48)
    if len(train)<1000 or len(calibration)<30:
        return None,{"qualified":False,"reason":"insufficient training/calibration"}
    model=(make_pipeline(StandardScaler(),Ridge(alpha=10)) if kind=="ridge" else
           HistGradientBoostingRegressor(max_iter=100,learning_rate=.05,max_leaf_nodes=7,min_samples_leaf=80,
               l2_regularization=10,random_state=44,early_stopping=False))
    with threadpool_limits(limits=2):
        model.fit(train[FEATURES],train.label48)
        predicted=model.predict(calibration[FEATURES])
    target=calibration.label48.to_numpy()
    penalty=max(0,float(np.mean(predicted-target)))
    entries=predicted-penalty>=.003
    qualified=int(entries.sum())>=5 and float(target[entries].mean())>0
    audit={"qualified":bool(qualified),"fit_cutoff":cutoff,"training_samples":len(train),
           "calibration_samples":len(calibration),"training_labels_end":int(train.label_end48.max()),
           "calibration_start":cal_start,"calibration_labels_end":int(calibration.label_end48.max()),
           "positive_overprediction_penalty":penalty,"qualified_calibration_entries":int(entries.sum()),
           "calibration_entry_mean_net":float(target[entries].mean()) if entries.any() else None}
    assert audit["training_labels_end"]<cal_start and audit["calibration_labels_end"]<cutoff
    return model,audit


def predict(rows,kind,start,end,folder):
    week=pd.Timestamp(start,unit="s",tz="UTC").normalize()
    week-=pd.Timedelta(days=week.weekday())
    predictions=[]
    fits=[]
    (folder/"models").mkdir(exist_ok=True)
    while week.timestamp()<end:
        cutoff=int(week.timestamp())
        following=week+pd.Timedelta(days=7)
        current=rows[(rows.available_at>=max(start,cutoff))&(rows.available_at<min(end,int(following.timestamp())))].copy()
        model,audit=fit_week(rows,kind,cutoff)
        if model is not None:
            path=folder/"models"/f"{kind}_{start}_{week.strftime('%Y%m%d')}.joblib"
            joblib.dump(model,path,compress=3)
            audit["model_file"]=path.name
            audit["model_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
            with threadpool_limits(limits=2):
                current["forecast_net"]=model.predict(current[FEATURES])-audit["positive_overprediction_penalty"]
        else:
            current["forecast_net"]=np.nan
        current["fit_cutoff"]=cutoff
        current["calibration_passed"]=audit["qualified"]
        predictions.append(current[["symbol","available_at","forecast_net","fit_cutoff","calibration_passed"]])
        fits.append(audit)
        week=following
    return pd.concat(predictions,ignore_index=True),fits


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    p.add_argument("--phase",choices=("validation","diagnostic"),required=True)
    args=p.parse_args()
    folder=args.data/"adaptive_returns"
    folder.mkdir(exist_ok=True)
    spec_path=Path(__file__).with_name("adaptive_return_experiment.json")
    spec=json.loads(spec_path.read_text())
    hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
            ("adaptive_returns.py","adaptive_return_experiment.json","learned_returns.py","backtest_candidates.py")}
    hashes["manifest"]=hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest()
    frozen=folder/"frozen.json"
    if frozen.exists():
        if json.loads(frozen.read_text())["hashes"]!=hashes:
            raise ValueError("Frozen source/data changed")
    elif args.phase=="validation":
        frozen.write_text(json.dumps({"at":datetime.now(timezone.utc).isoformat(),"spec":spec,"hashes":hashes},indent=2)+"\n")
    else:
        raise ValueError("Validation must freeze the experiment")
    target=folder/f"{args.phase}.json"
    if target.exists():
        raise ValueError("Phase already evaluated")
    frames,meta,manifest,rows=load(args.data)
    records=[]
    if args.phase=="validation":
        periods=[("validation",spec["validation"])]
        kinds=spec["models"]
    else:
        selection=json.loads((folder/"validation.json").read_text())["selected"]
        kinds=[selection] if selection else ["hist_gradient_boosting"]
        periods=[("diagnostic",spec["diagnostic"]),("recent_previously_seen",spec["recent"])]
    for phase,period in periods:
        start,end=map(epoch,period)
        for kind in kinds:
            forecasts,fits=predict(rows,kind,start,end,folder)
            forecasts.to_csv(folder/f"{phase}_{kind}_forecasts.csv.gz",index=False,compression="gzip")
            r=evaluate(frames,meta,manifest,forecasts,kind,48,start,end)
            r.update(phase=phase,weekly_fits=fits)
            records.append(r)
            print(json.dumps({"phase":phase,"model":kind,"base":{k:r["base"][k] for k in
                  ("net_pnl","trades","wins","win_rate_pct","profit_factor","max_drawdown_pct")},"stress_net":r["stress"]["net_pnl"]}),flush=True)
    result={"records":records,"real_orders":0,"exploratory_previously_seen_periods":True}
    if args.phase=="validation":
        eligible=[r for r in records if r["base"]["trades"]>=10 and r["base"]["net_pnl"]>0 and r["stress"]["net_pnl"]>0
                  and max(r["base"]["max_drawdown_pct"],r["stress"]["max_drawdown_pct"])<2]
        eligible.sort(key=lambda r:(-r["stress"]["net_pnl"],-r["base"]["net_pnl"]))
        result["selected"]=eligible[0]["model"] if eligible else None
    target.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")


if __name__=="__main__":
    main()
