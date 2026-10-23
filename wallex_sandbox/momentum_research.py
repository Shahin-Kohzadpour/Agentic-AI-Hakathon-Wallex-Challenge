"""Lower-turnover, fixed-rule historical experiment; never real orders."""
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
from .report_candidates import ledger_check


def prepare(directory,hours):
    manifest=json.loads((directory/"manifest.json").read_text())
    frames,meta={},{}
    for symbol,r in manifest["symbols"].items():
        path=directory/r["3600"]["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest()!=r["3600"]["sha256"]:
            raise ValueError("History hash mismatch")
        rows=json.loads(path.read_text())
        f=indicators(pd.DataFrame(rows))
        f["available_at"]=f.t+3600
        f["mom7"]=f.c/f.c.shift(168)-1
        f["mom28"]=f.c/f.c.shift(672)-1
        f["vol7"]=np.log(f.c).diff().rolling(168).std()*np.sqrt(168)
        # Fixed-duration lookbacks require their exact hourly observation times.
        f["momentum_ready"]=f.t.diff().eq(3600).rolling(672).sum().eq(672)
        daily=aggregate(rows,3600,86400)
        daily=indicators(pd.DataFrame(daily))
        daily["available_at"]=daily.t+86400
        f=pd.merge_asof(f,daily[["available_at","atr"]].rename(columns={"atr":"daily_atr"}),
                        on="available_at",direction="backward",tolerance=172799)
        f["atr"]=f.daily_atr
        cost=.004+2*(max(7.5,r.get("spread_snapshot_bps",15)/2)+1)/10000
        f["momentum_score"]=(f.mom7-cost)/f.vol7
        scheduled=(f.available_at%(hours*3600)==0)
        # After an isolated data gap, a 28-day momentum can still be computed by
        # timestamp rather than by pretending 672 observed bars span 28 days.
        close_at=dict(zip(f.t.astype(int),f.c.astype(float)))
        f["mom7"]=[c/close_at[t-168*3600]-1 if t-168*3600 in close_at else np.nan for t,c in zip(f.t.astype(int),f.c)]
        f["mom28"]=[c/close_at[t-672*3600]-1 if t-672*3600 in close_at else np.nan for t,c in zip(f.t.astype(int),f.c)]
        f["momentum_score"]=(f.mom7-cost)/f.vol7
        recent=f.t.diff().eq(3600).rolling(24).sum().eq(24)
        f["eligible"]=(scheduled&recent&(f.mom7>2*cost)&(f.mom28>0)&(f.c>f.ema200)&f.atr.notna()&f.vol7.gt(0))
        f["momentum_rotation"]=False
        frames[symbol]=f
        meta[symbol]=r["metadata"]
    # Freeze cross-sectional choices using only features available at that close.
    opportunities={}
    for symbol,f in frames.items():
        for i,row in f[f.eligible].iterrows():
            opportunities.setdefault(int(row.available_at),[]).append((float(row.momentum_score),symbol,i))
    for choices in opportunities.values():
        _,symbol,i=sorted(choices,key=lambda x:(-x[0],x[1]))[0]
        frames[symbol].loc[i,"momentum_rotation"]=True
    return frames,meta,manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    spec=Path(__file__).with_name("momentum_experiment.json")
    folder=args.data/"momentum_exploratory"
    folder.mkdir(exist_ok=True)
    if (folder/"frozen.json").exists():
        raise ValueError("Experiment already frozen; do not repeatedly search this result")
    frozen={"at":datetime.now(timezone.utc).isoformat(),"spec":json.loads(spec.read_text()),
            "spec_sha256":hashlib.sha256(spec.read_bytes()).hexdigest(),
            "source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "engine_sha256":hashlib.sha256(Path(__file__).with_name("backtest_candidates.py").read_bytes()).hexdigest(),
            "manifest_sha256":hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest()}
    (folder/"frozen.json").write_text(json.dumps(frozen,indent=2)+"\n")
    results=[]
    for hours in frozen["spec"]["variants"]:
        frames,meta,manifest=prepare(args.data,hours)
        spread={s:r.get("spread_snapshot_bps",15)/2 for s,r in manifest["symbols"].items()}
        config=Simulation(stop_atr=2,max_hold_bars=hours,cooldown_bars=0,symbol_half_spread_bps=spread)
        start=manifest["end"]-90*86400
        end=manifest["end"]
        result=simulate(frames,meta,"momentum_rotation",start,end,3600,config,regime_exit=False)
        stress=simulate(frames,meta,"momentum_rotation",start,end,3600,
                        Simulation(**{**asdict(config),"half_spread_bps":15,"slippage_bps":2,
                                      "symbol_half_spread_bps":{s:v*2 for s,v in spread.items()}}),regime_exit=False)
        first_ten=simulate(frames,meta,"momentum_rotation",start,end,3600,config,trade_limit=10,regime_exit=False)
        record={"holding_hours":hours,"result":result,"stress":stress,"first_ten":first_ten,
                "buy_hold":buy_hold(frames,meta,start,end,config),
                "ledger_checks":{k:ledger_check(r) for k,r in (("result",result),("stress",stress),("first_ten",first_ten))}}
        results.append(record)
        (folder/f"variant_{hours}.json").write_text(json.dumps(record,indent=2,allow_nan=False)+"\n")
        print(json.dumps({"hours":hours,"result":compact(result),"stress":compact(stress),"first_ten":compact(first_ten)},allow_nan=False),flush=True)
    (folder/"results.json").write_text(json.dumps({"frozen":frozen,"exploratory_not_untouched_holdout":True,
         "real_orders":0,"results":results},indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
