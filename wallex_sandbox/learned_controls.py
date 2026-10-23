"""Matched non-ML controls: same liquidity, horizon, size, stops and costs."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .backtest_candidates import Simulation, simulate
from .learned_returns import load
from .managed_trend import epoch
from .report_candidates import ledger_check


def control_frames(frames,h,end):
    markets={}
    opportunities={}
    for symbol,f in frames.items():
        x=f.copy()
        x["atr"]=x.daily_atr
        x["matched_momentum"]=False
        eligible=(x.feature_ready&(x.available_at%14400==0)&(x.ret168>2*x.cost_return)&
                  (x.ret672>0)&(x.ema200_gap>0)&(x.available_at+h*3600<=end))
        for i,row in x[eligible].iterrows():
            score=(row.ret168-row.cost_return)/(2*row.daily_atr_fraction+row.cost_return)
            opportunities.setdefault(int(row.available_at),[]).append((float(score),symbol,i))
        markets[symbol]=x
    for choices in opportunities.values():
        _,s,i=sorted(choices,key=lambda x:(-x[0],x[1]))[0]
        markets[s].loc[i,"matched_momentum"]=True
    keep=["t","o","h","l","c","v","available_at","atr","matched_momentum"]
    return {s:f[keep] for s,f in markets.items()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    folder=args.data/"learned_returns"
    if (folder/"matched_controls.json").exists():
        raise ValueError("Controls already evaluated")
    spec=json.loads(Path(__file__).with_name("learned_return_experiment.json").read_text())
    (folder/"controls_frozen.json").write_text(json.dumps({"at":datetime.now(timezone.utc).isoformat(),
        "source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "description":__doc__,"rule":"positive 7/28-day momentum, price above EMA200, 7-day move above twice cost; rank cost-adjusted momentum/planned risk; 4h decisions; both 24h and48h reported",
        "timing_note":"Controls added after ML validation, before ML check output inspected; not selected on test returns"},indent=2)+"\n")
    frames,meta,manifest,_=load(args.data)
    spreads={s:r["spread_snapshot_bps"]/2 for s,r in manifest["symbols"].items()}
    records=[]
    for phase,key in (("validation","selection_period"),("historical_check","check_period"),("recent_previously_seen","recent_diagnostic")):
        start,end=map(epoch,spec[key])
        for h in (24,48):
            market=control_frames(frames,h,end)
            market={s:f[(f.t>=start-3600)&(f.t<end)] for s,f in market.items()}
            r={"phase":phase,"horizon_hours":h}
            for key,stress in (("base",False),("stress",True)):
                config=Simulation(stop_atr=2,max_hold_bars=h,cooldown_bars=0,disable_profit_target=True,
                    half_spread_bps=15 if stress else 7.5,slippage_bps=2 if stress else 1,
                    symbol_half_spread_bps={s:v*(2 if stress else 1) for s,v in spreads.items()})
                r[key]=simulate(market,meta,"matched_momentum",start,end,3600,config,regime_exit=False)
                r[key+"_audit"]=ledger_check(r[key])
            records.append(r)
            print(json.dumps({"phase":phase,"horizon_hours":h,"base_net":r["base"]["net_pnl"],
                              "stress_net":r["stress"]["net_pnl"],"trades":r["base"]["trades"],"wins":r["base"]["wins"]}),flush=True)
    (folder/"matched_controls.json").write_text(json.dumps({"records":records,"real_orders":0},indent=2,allow_nan=False)+"\n")


if __name__=="__main__":
    main()
