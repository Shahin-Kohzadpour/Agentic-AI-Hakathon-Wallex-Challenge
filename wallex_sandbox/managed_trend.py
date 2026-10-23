"""Causal managed-position candidates, frozen separately from old experiments."""
import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest_candidates import Simulation, aggregate, buy_hold, compact, simulate
from .indicator_candidates import indicators
from .momentum_research import prepare as momentum_prepare
from .report_candidates import ledger_check

NAMES=("baseline48","retained_regime48","trail4h72","pullback4h72")


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def rank_entries(frames):
    opportunities={}
    for symbol,f in frames.items():
        f["managed_entry"]=False
        for i,row in f[f.eligible].iterrows():
            opportunities.setdefault(int(row.available_at),[]).append((float(row.momentum_score),symbol,i))
    for choices in opportunities.values():
        _,symbol,i=sorted(choices,key=lambda x:(-x[0],x[1]))[0]
        frames[symbol].loc[i,"managed_entry"]=True


def prepare(directory):
    base,metadata,manifest=momentum_prepare(directory,48)
    four={}
    for symbol,f in base.items():
        rows=f[["t","o","h","l","c","v"]].to_dict("records")
        q=indicators(pd.DataFrame(aggregate(rows,3600,14400)))
        q["available_at"]=q.t+14400
        contiguous=q.t.diff().eq(14400).rolling(20).sum().eq(20)
        q["market_ok"]=(q.c>q.ema50)&(q.ema50>q.ema200)&(q.ema20>q.ema20.shift(3))&contiguous
        q["pullback"]=(q.l.shift()<=q.ema20.shift())&(q.c>q.c.shift())
        cols=["available_at","c","ema20","ema50","ema200","atr","market_ok","pullback"]
        four[symbol]=q[cols].rename(columns={k:"q_"+k for k in cols if k!="available_at"})
    market=four["BTCUSDT"][["available_at","q_market_ok"]].rename(columns={"q_market_ok":"btc_regime"})
    output={name:{} for name in NAMES}
    keep=["t","o","h","l","c","v","available_at","atr","trail_atr","managed_entry","exit_signal","retain_signal","regime"]
    for symbol,f in base.items():
        f=pd.merge_asof(f,four[symbol],on="available_at",direction="backward",tolerance=14399)
        f=pd.merge_asof(f,market,on="available_at",direction="backward",tolerance=14399)
        spread=manifest["symbols"][symbol]["spread_snapshot_bps"]
        cost=.004+2*(max(7.5,spread/2)+1)/10000
        close_at=dict(zip(f.t.astype(int),f.c.astype(float)))
        f["mom24"]=[c/close_at[t-86400]-1 if t-86400 in close_at else np.nan for t,c in zip(f.t.astype(int),f.c)]
        recent=f.t.diff().eq(3600).rolling(24).sum().eq(24)
        retained=(recent&f.btc_regime.eq(True)&(spread<=30)&(f.mom7>2*cost)&(f.mom28>0)&(f.c>f.ema200)&f.atr.notna())
        trend=(recent&f.btc_regime.eq(True)&(spread<=30)&(f.q_c>f.q_ema20)&(f.q_ema20>f.q_ema50)&
               (f.q_ema50>f.q_ema200)&(f.mom24>cost)&(f.mom7>0)&f.q_atr.gt(0))
        for name in NAMES:
            x=f.copy()
            x["regime"]=True
            x["trail_atr"]=x.atr
            x["retain_signal"]=False
            x["exit_signal"]=False
            x["eligible"]=False
            if name=="baseline48":
                x["managed_entry"]=x.momentum_rotation
            elif name=="retained_regime48":
                x["eligible"]=retained&(x.available_at%(48*3600)==0)
                x["retain_signal"]=retained
                x["exit_signal"]=(~x.btc_regime.eq(True))|(x.c<x.ema200)
            else:
                x["atr"]=x.q_atr
                x["trail_atr"]=x.q_atr
                x["eligible"]=trend&(x.available_at%14400==0)
                if name=="pullback4h72":
                    x["eligible"]&=x.q_pullback.eq(True)
                x["exit_signal"]=(~x.btc_regime.eq(True))|(x.q_c<x.q_ema50)
            # Ranking requires this column but the simulator never sees its value.
            x["managed_entry"]=x.momentum_rotation if name=="baseline48" else False
            output[name][symbol]=x[keep+["eligible","momentum_score"]].copy()
    for name in NAMES[1:]:
        rank_entries(output[name])
    for name in NAMES:
        output[name]={s:f[keep] for s,f in output[name].items()}
    return output,metadata,manifest


def configuration(name,manifest,stress=False):
    spread={s:r["spread_snapshot_bps"]/2*(2 if stress else 1) for s,r in manifest["symbols"].items()}
    options={"half_spread_bps":15 if stress else 7.5,"slippage_bps":2 if stress else 1,
             "symbol_half_spread_bps":spread,"stop_atr":2,"cooldown_bars":0,"max_hold_bars":48}
    if name=="retained_regime48":
        options.update(signal_exit=True,max_retained_bars=72,disable_profit_target=True)
    elif name in {"trail4h72","pullback4h72"}:
        options.update(signal_exit=True,trailing_atr_multiple=3,disable_profit_target=True,
                       max_hold_bars=72,cooldown_bars=4)
    return Simulation(**options)


def evaluate(frames,meta,manifest,name,start,end):
    # Indicators were calculated on past warmup data. The simulator only needs
    # this phase and its preceding signal row, reducing memory use substantially.
    phase={s:f[(f.t>=start-3600)&(f.t<end)] for s,f in frames[name].items()}
    result={"name":name}
    for key,stress in (("base",False),("stress",True)):
        r=simulate(phase,meta,"managed_entry",start,end,3600,configuration(name,manifest,stress),regime_exit=False)
        result[key]=r
        result[key+"_ledger_check"]=ledger_check(r)
    result["buy_hold_reference"]=buy_hold(phase,meta,start,end,configuration(name,manifest))
    return result


def fingerprint():
    names=("managed_trend.py","managed_trend_experiment.json","backtest_candidates.py",
           "momentum_research.py","indicator_candidates.py")
    return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    p.add_argument("--phase",choices=("development","validation","check"),required=True)
    args=p.parse_args()
    spec=json.loads(Path(__file__).with_name("managed_trend_experiment.json").read_text())
    folder=args.data/"managed_trend"
    folder.mkdir(exist_ok=True)
    frozen=folder/"frozen.json"
    hashes=fingerprint()
    data_hash=hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest()
    if frozen.exists():
        old=json.loads(frozen.read_text())
        if old["source_hashes"]!=hashes or old["manifest_sha256"]!=data_hash:
            raise ValueError("Source or data changed after experiment freeze")
    elif args.phase=="development":
        frozen.write_text(json.dumps({"at":datetime.now(timezone.utc).isoformat(),"spec":spec,
                         "source_hashes":hashes,"manifest_sha256":data_hash},indent=2)+"\n")
    else:
        raise ValueError("Freeze and run development first")
    if (folder/f"{args.phase}.json").exists():
        raise ValueError("This phase was already evaluated; cannot overwrite its results")
    frames,meta,manifest=prepare(args.data)
    if args.phase in {"development","validation"}:
        start,end=map(epoch,spec[args.phase])
        records=[]
        for name in NAMES:
            record=evaluate(frames,meta,manifest,name,start,end)
            records.append(record)
            print(json.dumps({"phase":args.phase,"name":name,"base":{k:record["base"][k] for k in
                  ("net_pnl","trades","wins","win_rate_pct","profit_factor","max_drawdown_pct","fees")},
                  "stress_net":record["stress"]["net_pnl"]}),flush=True)
        result={"phase":args.phase,"records":records}
        if args.phase=="validation":
            eligible=[r for r in records if r["name"]!="baseline48" and r["base"]["trades"]>=10 and
                      r["base"]["net_pnl"]>0 and r["stress"]["net_pnl"]>0 and
                      r["base"]["max_drawdown_pct"]<2 and r["stress"]["max_drawdown_pct"]<2]
            eligible.sort(key=lambda r:(-r["stress"]["net_pnl"],-r["base"]["net_pnl"]))
            result["qualified"]=eligible[0]["name"] if eligible else None
            result["predeclared_primary"]="trail4h72"
            result["frozen_at"]=datetime.now(timezone.utc).isoformat()
        (folder/f"{args.phase}.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    else:
        selection=json.loads((folder/"validation.json").read_text())
        names=list(dict.fromkeys(["baseline48","trail4h72"]+([selection["qualified"]] if selection["qualified"] else [])))
        result={"qualification":selection["qualified"],"primary":"trail4h72","earlier_uninspected":[],"recent_previously_seen":[],
                "real_orders":0,"credentials_used":False,"not_prospective":True}
        for key,period in (("earlier_uninspected","secondary_historical_check"),("recent_previously_seen","recent_seen_diagnostic")):
            start,end=map(epoch,spec[period])
            for name in names:
                record=evaluate(frames,meta,manifest,name,start,end)
                result[key].append(record)
                print(json.dumps({"phase":key,"name":name,"base":{k:record["base"][k] for k in
                      ("net_pnl","trades","wins","win_rate_pct","profit_factor","max_drawdown_pct","fees")},
                      "stress_net":record["stress"]["net_pnl"]}),flush=True)
        (folder/"check.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
