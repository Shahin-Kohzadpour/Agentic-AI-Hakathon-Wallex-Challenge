"""Strictly past-trained monthly local models for cost-aware paper research."""
import os
os.environ.setdefault("OMP_NUM_THREADS","2")
os.environ.setdefault("OPENBLAS_NUM_THREADS","2")

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .backtest_candidates import Simulation, buy_hold, simulate
from .indicator_candidates import indicators
from .managed_trend import epoch
from .report_candidates import ledger_check

FEATURES=["ret4","ret12","ret24","ret72","ret168","ret672","ema20_gap","ema50_gap","ema200_gap",
          "rsi_scaled","atr_fraction","daily_atr_fraction","vol24","vol168","volume_ratio","range_fraction",
          "btc_ret24","btc_ret168","btc_ema200_gap","cost_return"]


def complete_daily(frame):
    if frame.t.duplicated().any():
        raise ValueError("Duplicate historical times")
    f=frame.sort_values("t").copy()
    f["bucket"]=f.t//86400*86400
    g=f.groupby("bucket",sort=True).agg(t=("t","min"),last=("t","max"),n=("t","size"),
            o=("o","first"),h=("h","max"),l=("l","min"),c=("c","last"),v=("v","sum"))
    return g[(g.n==24)&(g.t==g.index)&(g["last"]==g.index+23*3600)][["t","o","h","l","c","v"]].reset_index(drop=True)


def features(frame,cost):
    f=indicators(frame)
    f["available_at"]=f.t+3600
    close_at=dict(zip(f.t.astype(int),f.c))
    for h in (4,12,24,72,168,672):
        f[f"ret{h}"]=[c/close_at[t-h*3600]-1 if t-h*3600 in close_at else np.nan for t,c in zip(f.t.astype(int),f.c)]
    for n in (20,50,200):
        f[f"ema{n}_gap"]=f.c/f[f"ema{n}"]-1
    f["rsi_scaled"]=f.rsi/100
    f["atr_fraction"]=f.atr/f.c
    returns=np.log(f.c).diff()
    f["vol24"]=returns.rolling(24).std()
    f["vol168"]=returns.rolling(168).std()
    f["volume_ratio"]=(f.v/f.v.shift().rolling(24).mean()).clip(upper=20)
    f["range_fraction"]=(f.h-f.l)/f.c
    f["cost_return"]=cost
    day=indicators(complete_daily(frame))
    day["available_at"]=day.t+86400
    f=pd.merge_asof(f,day[["available_at","atr"]].rename(columns={"atr":"daily_atr"}),
                    on="available_at",direction="backward",tolerance=172799)
    f["daily_atr_fraction"]=f.daily_atr/f.c
    f["recent_ok"]=f.t.diff().eq(3600).rolling(24).sum().eq(24)
    return f


def add_label(f,h,friction,fee=.002):
    # Values are labels only, never predictor features or entry-availability masks.
    entry=f.o.shift(-1)*(1+friction)*(1+fee)
    exit_value=f.o.shift(-(h+1))*(1-friction)*(1-fee)
    complete=f.t.shift(-(h+1))-f.t==(h+1)*3600
    net=(exit_value/entry-1).where(complete)
    return net, f.available_at+h*3600


def load(directory):
    manifest=json.loads((directory/"manifest.json").read_text())
    frames,meta={},{}
    for symbol,r in manifest["symbols"].items():
        path=directory/r["3600"]["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest()!=r["3600"]["sha256"]:
            raise ValueError("History hash mismatch")
        raw=pd.DataFrame(json.loads(path.read_text())).sort_values("t").reset_index(drop=True)
        friction=(max(7.5,r["spread_snapshot_bps"]/2)+1)/10000
        cost=1-(1-friction)*.998/((1+friction)*1.002)
        f=features(raw,cost)
        for h in (24,48):
            f[f"label{h}"],f[f"label_end{h}"]=add_label(f,h,friction)
        f["symbol"]=symbol
        f["market_allowed"]=r["spread_snapshot_bps"]<=30
        frames[symbol]=f
        meta[symbol]=r["metadata"]
    btc=frames["BTCUSDT"][["available_at","ret24","ret168","ema200_gap"]].rename(
        columns={"ret24":"btc_ret24","ret168":"btc_ret168","ema200_gap":"btc_ema200_gap"})
    for symbol,f in frames.items():
        f=pd.merge_asof(f,btc,on="available_at",direction="backward",tolerance=3599)
        f["feature_ready"]=np.isfinite(f[FEATURES]).all(axis=1)&f.recent_ok&f.market_allowed
        frames[symbol]=f
    all_rows=pd.concat([f[f.feature_ready&(f.available_at%14400==0)] for f in frames.values()],ignore_index=True)
    return frames,meta,manifest,all_rows


def nonoverlap(frame,h):
    selected=[]
    for _,part in frame.groupby("symbol",sort=True):
        available=-1
        for i,row in part.sort_values("available_at").iterrows():
            if row.available_at>=available:
                selected.append(i)
                available=row[f"label_end{h}"]
    return frame.loc[selected]


def fit_month(rows,kind,h,cutoff):
    cal_start=cutoff-30*86400
    train=rows[(rows.available_at>=cutoff-180*86400)&(rows.available_at<cal_start)&
               (rows[f"label_end{h}"]<cal_start)&rows[f"label{h}"].notna()]
    calibration=rows[(rows.available_at>=cal_start)&(rows[f"label_end{h}"]<cutoff)&rows[f"label{h}"].notna()]
    calibration=nonoverlap(calibration,h)
    if len(train)<1000 or len(calibration)<30:
        return None,{"qualified":False,"reason":"insufficient training/calibration"}
    if kind=="ridge":
        model=make_pipeline(StandardScaler(),Ridge(alpha=10))
    else:
        model=HistGradientBoostingRegressor(max_iter=100,learning_rate=.05,max_leaf_nodes=7,
                    min_samples_leaf=80,l2_regularization=10,random_state=44,early_stopping=False)
    with threadpool_limits(limits=2):
        model.fit(train[FEATURES],train[f"label{h}"])
        predicted=model.predict(calibration[FEATURES])
    target=calibration[f"label{h}"].to_numpy()
    penalty=max(0,float(np.mean(predicted-target)))
    entries=predicted-penalty>=.003
    qualified=int(entries.sum())>=5 and float(target[entries].mean())>0
    audit={"qualified":bool(qualified),"training_samples":len(train),"calibration_samples":len(calibration),
           "training_labels_end":int(train[f"label_end{h}"].max()),"calibration_start":cal_start,
           "calibration_labels_end":int(calibration[f"label_end{h}"].max()),"fit_cutoff":cutoff,
           "positive_overprediction_penalty":penalty,"qualified_calibration_entries":int(entries.sum()),
           "calibration_entry_mean_net":float(target[entries].mean()) if entries.any() else None,
           "calibration_model_mse":float(np.mean((predicted-target)**2)),"calibration_zero_mse":float(np.mean(target**2))}
    assert audit["training_labels_end"]<cal_start and audit["calibration_labels_end"]<cutoff
    return model,audit


def predict_period(rows,kind,h,start,end,folder):
    month=pd.Timestamp(start,unit="s",tz="UTC").normalize().replace(day=1)
    forecasts=[]
    audits=[]
    (folder/"models").mkdir(exist_ok=True)
    while month.timestamp()<end:
        cutoff=int(month.timestamp())
        following=month+pd.offsets.MonthBegin(1)
        stop=min(end,int(following.timestamp()))
        current=rows[(rows.available_at>=max(start,cutoff))&(rows.available_at<stop)].copy()
        model,audit=fit_month(rows,kind,h,cutoff)
        audit.update(model=kind,horizon_hours=h,month=month.isoformat())
        if model is not None:
            path=folder/"models"/f"{kind}_{h}_{month.strftime('%Y%m')}.joblib"
            joblib.dump(model,path,compress=3)
            audit["model_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
            with threadpool_limits(limits=2):
                current["forecast_net"]=model.predict(current[FEATURES])-audit["positive_overprediction_penalty"]
        else:
            current["forecast_net"]=np.nan
        current["fit_cutoff"]=cutoff
        current["calibration_passed"]=audit["qualified"]
        forecasts.append(current[["symbol","available_at","forecast_net","fit_cutoff","calibration_passed"]])
        audits.append(audit)
        month=following
    return pd.concat(forecasts,ignore_index=True),audits


def entry_frames(frames,forecasts,h,end):
    result={}
    opportunities={}
    for symbol,f in frames.items():
        prediction=forecasts[forecasts.symbol==symbol].drop(columns="symbol")
        x=f.merge(prediction,on="available_at",how="left",validate="one_to_one")
        x["atr"]=x.daily_atr
        x["learned_entry"]=False
        eligible=(x.feature_ready&x.calibration_passed.eq(True)&(x.forecast_net>=.003)&
                  (x.available_at+h*3600<=end)&(x.fit_cutoff<=x.available_at))
        for i,row in x[eligible].iterrows():
            risk=2*row.daily_atr/row.c+row.cost_return
            score=row.forecast_net/risk
            opportunities.setdefault(int(row.available_at),[]).append((float(score),symbol,i))
        result[symbol]=x
    for choices in opportunities.values():
        _,symbol,i=sorted(choices,key=lambda x:(-x[0],x[1]))[0]
        result[symbol].loc[i,"learned_entry"]=True
    keep=["t","o","h","l","c","v","available_at","atr","learned_entry"]
    return {s:f[keep] for s,f in result.items()}


def evaluate(frames,meta,manifest,forecasts,kind,h,start,end):
    markets=entry_frames(frames,forecasts,h,end)
    markets={s:f[(f.t>=start-3600)&(f.t<end)] for s,f in markets.items()}
    result={"model":kind,"horizon_hours":h}
    spreads={s:r["spread_snapshot_bps"]/2 for s,r in manifest["symbols"].items()}
    for name,stress in (("base",False),("stress",True)):
        config=Simulation(stop_atr=2,max_hold_bars=h,cooldown_bars=0,disable_profit_target=True,
               half_spread_bps=15 if stress else 7.5,slippage_bps=2 if stress else 1,
               symbol_half_spread_bps={s:v*(2 if stress else 1) for s,v in spreads.items()})
        r=simulate(markets,meta,"learned_entry",start,end,3600,config,regime_exit=False)
        result[name]=r
        result[name+"_audit"]=ledger_check(r)
    result["buy_hold_reference"]=buy_hold(markets,meta,start,end,Simulation(symbol_half_spread_bps=spreads))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    p.add_argument("--phase",choices=("validation","check"),required=True)
    args=p.parse_args()
    folder=args.data/"learned_returns"
    folder.mkdir(exist_ok=True)
    spec_path=Path(__file__).with_name("learned_return_experiment.json")
    spec=json.loads(spec_path.read_text())
    hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
           ("learned_returns.py","learned_return_experiment.json","backtest_candidates.py","indicator_candidates.py")}
    hashes["manifest"]=hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest()
    frozen=folder/"frozen.json"
    if frozen.exists():
        if json.loads(frozen.read_text())["hashes"]!=hashes:
            raise ValueError("Frozen source/data changed")
    elif args.phase=="validation":
        frozen.write_text(json.dumps({"at":datetime.now(timezone.utc).isoformat(),"hashes":hashes,"spec":spec,
                                     "sklearn_version":sklearn.__version__},indent=2)+"\n")
    else:
        raise ValueError("Validation must freeze the experiment first")
    target=folder/f"{args.phase}.json"
    if target.exists():
        raise ValueError("Phase already evaluated; cannot overwrite")
    frames,meta,manifest,rows=load(args.data)
    records=[]
    if args.phase=="validation":
        periods=[("validation",spec["selection_period"])]
        variants=[(kind,h) for kind in spec["models"] for h in spec["horizons_hours"]]
    else:
        selected=json.loads((folder/"validation.json").read_text())["selected"]
        variants=[(selected["model"],selected["horizon_hours"])] if selected else [("hist_gradient_boosting",48)]
        periods=[("historical_check",spec["check_period"]),("recent_previously_seen",spec["recent_diagnostic"])]
    for phase,period in periods:
        start,end=map(epoch,period)
        for kind,h in variants:
            forecasts,audits=predict_period(rows,kind,h,start,end,folder)
            forecasts.to_csv(folder/f"{phase}_{kind}_{h}_forecasts.csv.gz",index=False,compression="gzip")
            r=evaluate(frames,meta,manifest,forecasts,kind,h,start,end)
            r.update(phase=phase,monthly_fits=audits)
            records.append(r)
            print(json.dumps({"phase":phase,"model":kind,"horizon_hours":h,"base":{k:r["base"][k] for k in
                 ("net_pnl","trades","wins","win_rate_pct","profit_factor","max_drawdown_pct")},"stress_net":r["stress"]["net_pnl"]}),flush=True)
    result={"records":records,"real_orders":0,"not_prospective":True}
    if args.phase=="validation":
        eligible=[r for r in records if r["base"]["trades"]>=10 and r["base"]["net_pnl"]>0 and
                  r["stress"]["net_pnl"]>0 and max(r["base"]["max_drawdown_pct"],r["stress"]["max_drawdown_pct"])<2]
        eligible.sort(key=lambda r:(-r["stress"]["net_pnl"],-r["base"]["net_pnl"]))
        result["selected"]={"model":eligible[0]["model"],"horizon_hours":eligible[0]["horizon_hours"]} if eligible else None
    target.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
