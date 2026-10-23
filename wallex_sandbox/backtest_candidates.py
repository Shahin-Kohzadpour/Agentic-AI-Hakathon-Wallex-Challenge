"""Historical, fee-aware event simulation. No future signals or exchange orders.

Historical bid/ask and queues are unavailable: spread and fills are assumptions.
All candidates share one-wallet, one-position accounting and the same costs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .indicator_candidates import CANDIDATES, signals


@dataclass(frozen=True)
class Simulation:
    initial: float=1000
    fee: float=.002
    half_spread_bps: float=7.5
    slippage_bps: float=1
    risk_fraction: float=.001
    max_spend: float=100
    stop_atr: float=1.5
    net_reward_multiple: float=2
    max_hold_bars: int=16
    cooldown_bars: int=1
    signal_exit: bool=False
    trailing_atr_multiple: float | None=None
    disable_profit_target: bool=False
    max_retained_bars: int | None=None
    symbol_half_spread_bps: dict=field(default_factory=dict)

    @property
    def friction(self):
        return (self.half_spread_bps+self.slippage_bps)/10000

    def market_friction(self,symbol):
        return (max(self.half_spread_bps,self.symbol_half_spread_bps.get(symbol,0))+self.slippage_bps)/10000


def aggregate(rows,source_step,target_step):
    if not rows:
        return []
    frame=pd.DataFrame(rows).sort_values("t")
    frame["bucket"]=frame.t//target_step*target_step
    result=[]
    for t,part in frame.groupby("bucket",sort=True):
        if part.t.tolist() != list(range(int(t),int(t)+target_step,source_step)):
            continue
        result.append({"t":int(t),"o":float(part.o.iloc[0]),"h":float(part.h.max()),
                       "l":float(part.l.min()),"c":float(part.c.iloc[-1]),"v":float(part.v.sum())})
    return result


def load_frames(directory):
    manifest=json.loads((directory/"manifest.json").read_text())
    frames={900:{},3600:{}}
    metadata={}
    for symbol,record in manifest["symbols"].items():
        metadata[symbol]=record["metadata"]
        histories={}
        for step in (60,900,3600):
            path=directory/record[str(step)]["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != record[str(step)]["sha256"]:
                raise ValueError("Historical file hash mismatch")
            histories[step]=json.loads(path.read_text())
        for step,regime in ((900,3600),(3600,14400)):
            slow=histories[3600] if regime == 3600 else aggregate(histories[3600],3600,regime)
            if not histories[step] or not slow:
                raise ValueError("Insufficient historical data")
            frames[step][symbol]=signals(pd.DataFrame(histories[step]),pd.DataFrame(slow),step,regime)
    return frames,metadata,manifest


def simulate(frames,metadata,candidate,start,end,step,config=Simulation(),trade_limit=None,regime_exit=None):
    if not frames or any(candidate not in frame.columns for frame in frames.values()):
        raise ValueError("Unknown fixed candidate")
    if regime_exit is None:
        regime_exit=candidate not in {"macd_bb_unfiltered","bb_reentry","donchian_breakout"}
    tables={s:{int(r["t"]):r for r in f.to_dict("records")} for s,f in frames.items()}
    times=sorted({t for table in tables.values() for t in table if start <= t and t+step <= end})
    cash=config.initial
    position=None
    trades=[]
    equity=[]
    last_prices={}
    next_entry=start
    rejections={"quantity_or_notional":0,"invalid_atr":0,"no_room_for_stop":0,"account_drawdown":0,
                "insufficient_net_reward":0}
    peak=config.initial
    max_drawdown=0.
    fees=0.
    last_time=None
    def close(raw_price,t,reason):
        nonlocal cash,position,fees,next_entry
        p=position
        price=raw_price*(1-p["friction"])
        proceeds=p["quantity"]*price
        fee=proceeds*config.fee
        cash+=proceeds-fee
        fees+=fee
        net=proceeds-fee-p["spent"]
        trades.append({"symbol":p["symbol"],"opened_epoch":p["opened_epoch"],"closed_epoch":t,
                       "signal_available_at":p["signal_available_at"],"entry_price":p["price"],"exit_price":price,
                       "quantity":p["quantity"],"entry_fee":p["entry_fee"],"exit_fee":fee,
                       "spent":p["spent"],"gross_pnl":proceeds-p["quantity"]*p["price"],
                       "net_pnl":net,"net_return":net/p["spent"],"reason":reason,
                       "stop_raw":p["stop"],"target_raw":p["target"],"planned_net_risk":p["planned_net_risk"]})
        position=None
        # The exit may occur anywhere in the candle; wait a complete extra bar.
        next_entry=t+step*config.cooldown_bars
    def mark(t):
        nonlocal peak,max_drawdown
        value=cash
        if position:
            value+=position["quantity"]*last_prices[position["symbol"]]*(1-position["friction"])*(1-config.fee)
        peak=max(peak,value)
        dd=(peak-value)/peak
        max_drawdown=max(max_drawdown,dd)
        equity.append({"t":t,"equity":value,"drawdown":dd})
    for t in times:
        current={s:table[t] for s,table in tables.items() if t in table}
        if position:
            symbol=position["symbol"]
            bar=current.get(symbol)
            if bar:
                gap=t-position["last_bar"]>step
                previous=tables[symbol].get(t-step)
                if previous is not None and config.trailing_atr_multiple is not None:
                    atr=previous.get("trail_atr",previous["atr"])
                    if math.isfinite(atr) and atr>0:
                        position["highest_closed_price"]=max(position["highest_closed_price"],previous["c"])
                        position["stop"]=max(position["stop"],position["highest_closed_price"]-config.trailing_atr_multiple*atr)
                age=t-position["opened_epoch"]
                retain=(config.max_retained_bars is not None and age<step*config.max_retained_bars
                        and previous is not None and bool(previous.get("retain_signal",False)))
                if gap:
                    close(bar["o"],t,"data_gap_exit")
                elif config.signal_exit and previous is not None and bool(previous.get("exit_signal",False)):
                    close(bar["o"],t,"signal_exit")
                elif age >= step*config.max_hold_bars and not retain:
                    close(bar["o"],t,"time_limit")
                elif regime_exit and previous is not None and previous["regime"] != True:
                    close(bar["o"],t,"regime_exit")
        if position is None and t >= next_entry and (trade_limit is None or len(trades)<trade_limit):
            choices=[]
            for symbol,bar in current.items():
                previous=tables[symbol].get(t-step)
                if previous is not None and previous["available_at"] <= t and bool(previous[candidate]):
                    choices.append((symbol,bar,previous))
            # Prefer lower execution friction relative to observed ATR. This is
            # known before entry, unlike future target hits or subsequent P&L.
            def priority(choice):
                symbol,bar,previous=choice
                volatility=previous["atr"]/previous["c"]
                cost=2*config.fee+2*config.market_friction(symbol)
                return (cost/volatility if volatility>0 else math.inf,symbol)
            for symbol,bar,previous in sorted(choices,key=priority):
                atr=previous["atr"]
                if not math.isfinite(atr) or atr <= 0:
                    rejections["invalid_atr"]+=1
                    continue
                if max_drawdown >= .02:
                    rejections["account_drawdown"]+=1
                    continue
                friction=config.market_friction(symbol)
                price=bar["o"]*(1+friction)
                stop=bar["o"]-config.stop_atr*atr
                if stop <= 0:
                    rejections["no_room_for_stop"]+=1
                    continue
                cost_per_unit=price*(1+config.fee)
                stop_proceeds=stop*(1-friction)*(1-config.fee)
                risk_per_unit=cost_per_unit-stop_proceeds
                budget=min(cash*.1,config.max_spend,cash*config.risk_fraction/risk_per_unit*cost_per_unit)
                precision=metadata[symbol]["quantity_precision"]
                qty=math.floor(budget/cost_per_unit*10**precision)/10**precision
                notional=qty*price
                if qty<float(metadata[symbol]["min_quantity"]) or notional<float(metadata[symbol]["min_notional"]):
                    rejections["quantity_or_notional"]+=1
                    continue
                # A 2R NET exit price, including both fees and adverse fills.
                target=(cost_per_unit+config.net_reward_multiple*risk_per_unit)/((1-friction)*(1-config.fee))
                if candidate == "bb_reentry":
                    target=float(previous["bb20_mid"])
                    reward=target*(1-friction)*(1-config.fee)-cost_per_unit
                    if reward < risk_per_unit*.75:
                        rejections["insufficient_net_reward"]+=1
                        continue
                if config.disable_profit_target:
                    target=None
                fee=notional*config.fee
                spent=notional+fee
                if spent>cash+1e-9 or spent>budget+1e-9:
                    raise ValueError("Proposed historical entry exceeds all-in spend")
                cash-=spent
                fees+=fee
                position={"symbol":symbol,"opened_epoch":t,"last_bar":t-step,"price":price,
                          "quantity":qty,"spent":spent,"entry_fee":fee,"stop":stop,"target":target,
                          "signal_available_at":int(previous["available_at"]),"planned_net_risk":qty*risk_per_unit,
                          "friction":friction,"highest_closed_price":bar["o"]}
                break
        if position:
            bar=current.get(position["symbol"])
            if bar:
                if bar["o"] <= position["stop"]:
                    close(bar["o"],t,"gap_stop")
                elif position["target"] is not None and bar["o"] >= position["target"]:
                    close(position["target"],t,"gap_target")
                elif bar["l"] <= position["stop"]:
                    close(position["stop"],t+step,"stop_loss")
                elif position["target"] is not None and bar["h"] >= position["target"]:
                    close(position["target"],t+step,"take_profit")
                else:
                    position["last_bar"]=t
        for symbol,bar in current.items():
            last_prices[symbol]=bar["c"]
        mark(t+step)
        last_time=t+step
        if trade_limit is not None and len(trades) >= trade_limit and position is None:
            break
    if position:
        close(last_prices[position["symbol"]],last_time,"period_end")
        mark(last_time)
    net=sum(t["net_pnl"] for t in trades)
    if abs(config.initial+net-cash)>1e-7:
        raise ValueError("Historical wallet ledger failed reconciliation")
    winning=sum(t["net_pnl"]>0 for t in trades)
    gains=sum(max(0,t["net_pnl"]) for t in trades)
    losses=-sum(min(0,t["net_pnl"]) for t in trades)
    return {"candidate":candidate,"timeframe_seconds":step,"start":start,"end":end,
            "observed_start":times[0] if times else None,"observed_end":last_time,
            "initial":config.initial,"final":cash,"net_pnl":net,"return_pct":net/config.initial*100,
            "trades":len(trades),"wins":winning,"win_rate_pct":winning/len(trades)*100 if trades else None,
            "profit_factor":gains/losses if losses else None,"fees":fees,
            "average_trade":net/len(trades) if trades else None,"max_drawdown_pct":max_drawdown*100,
            "selection_rejections":rejections,"fills":"historical OHLC simulation with assumed costs",
            "regime_exit_enabled":regime_exit,
            "trades_detail":trades,"equity":equity,"config":asdict(config)}


def compact(result):
    return {k:v for k,v in result.items() if k not in {"trades_detail","equity"}}


def buy_hold(frames,metadata,start,end,config):
    frame=frames["BTCUSDT"]
    bars=frame[(frame.t>=start)&(frame.t+int(frame.t.diff().median())<=end)]
    if bars.empty:
        return None
    first,last=bars.iloc[0],bars.iloc[-1]
    friction=config.market_friction("BTCUSDT")
    entry=float(first.o)*(1+friction)
    precision=metadata["BTCUSDT"]["quantity_precision"]
    qty=math.floor(config.max_spend/(entry*(1+config.fee))*10**precision)/10**precision
    spent=qty*entry*(1+config.fee)
    cash=config.initial-spent
    values=cash+qty*bars.c*(1-friction)*(1-config.fee)
    peak=values.cummax().clip(lower=config.initial)
    return {"initial":config.initial,"final":float(values.iloc[-1]),"net_pnl":float(values.iloc[-1])-config.initial,
            "max_drawdown_pct":float(((peak-values)/peak).max())*100,
            "description":"100 USDT all-in BTC holding plus cash, same assumed trading costs"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data",type=Path)
    parser.add_argument("--phase",choices=("development","validation","holdout"),required=True)
    args=parser.parse_args()
    spec_path=Path(__file__).with_name("candidate_experiment.json")
    spec=json.loads(spec_path.read_text())
    spec_hash=hashlib.sha256(spec_path.read_bytes()).hexdigest()
    source_hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ("backtest_candidates.py","indicator_candidates.py")}
    frames,meta,manifest=load_frames(args.data)
    end=manifest["end"]
    test_start=end-7*86400
    val_start=test_start-7*86400
    output=args.data/"evaluation"
    output.mkdir(exist_ok=True)
    if args.phase in {"development","validation"}:
        if args.phase == "validation" and (output/"selection.json").exists():
            raise ValueError("Selection already frozen; use a new data experiment for any revision")
        results=[]
        spread_assumptions={s:r.get("spread_snapshot_bps",15)/2 for s,r in manifest["symbols"].items()}
        config=Simulation(symbol_half_spread_bps=spread_assumptions)
        phase_start=val_start if args.phase == "validation" else end-90*86400
        phase_end=test_start if args.phase == "validation" else val_start
        for step,markets in frames.items():
            for candidate in CANDIDATES:
                candidate_config=(Simulation(symbol_half_spread_bps=spread_assumptions,stop_atr=2,max_hold_bars=24)
                                  if candidate == "bb_reentry" else
                                  Simulation(symbol_half_spread_bps=spread_assumptions,stop_atr=2.5,max_hold_bars=48)
                                  if candidate == "donchian_breakout" else config)
                result=simulate(markets,meta,candidate,phase_start,phase_end,step,candidate_config)
                (output/f"{args.phase}_{step}_{candidate}.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
                results.append(compact(result))
                print(json.dumps({"phase":args.phase,**{k:result[k] for k in
                      ("candidate","timeframe_seconds","trades","wins","net_pnl","fees","profit_factor","max_drawdown_pct")}},allow_nan=False),flush=True)
        if args.phase == "development":
            return
        eligible=[r for r in results if r["trades"]>=5 and r["net_pnl"]>0 and
                  (r["profit_factor"] is None or r["profit_factor"]>1) and r["max_drawdown_pct"]<2]
        eligible.sort(key=lambda r:(-r["net_pnl"],r["max_drawdown_pct"],r["candidate"]))
        selection={"frozen_at":datetime.now(timezone.utc).isoformat(),"spec_sha256":spec_hash,"source_hashes":source_hashes,
                   "data_manifest_sha256":hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest(),
                   "validation_start":val_start,"holdout_start":test_start,"holdout_end":end,
                   "validation_results":results,"selected":eligible[0] if eligible else None,
                   "reason":"highest qualifying validation P&L" if eligible else "no candidate passed validation",
                   "holdout_opened":False}
        (output/"selection.json").write_text(json.dumps(selection,indent=2,allow_nan=False)+"\n")
        print(json.dumps({"selection":selection["selected"]},allow_nan=False),flush=True)
    else:
        selection=json.loads((output/"selection.json").read_text())
        if selection["spec_sha256"] != spec_hash:
            raise ValueError("Experiment specification changed after selection")
        if selection["source_hashes"] != source_hashes:
            raise ValueError("Strategy or simulation code changed after selection")
        if selection["data_manifest_sha256"] != hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest():
            raise ValueError("Historical data changed after selection")
        if (output/"holdout.json").exists():
            raise ValueError("Holdout already evaluated; cannot repeatedly optimize it")
        chosen=selection["selected"]
        if not chosen:
            raise ValueError("No validated candidate: do not manufacture a holdout winner")
        step=chosen["timeframe_seconds"]
        config=Simulation(**chosen["config"])
        selected=simulate(frames[step],meta,chosen["candidate"],test_start,end,step,config)
        stress=simulate(frames[step],meta,chosen["candidate"],test_start,end,step,
                        Simulation(**{**asdict(config),"half_spread_bps":15,"slippage_bps":2,
                                   "symbol_half_spread_bps":{s:v*2 for s,v in config.symbol_half_spread_bps.items()}}))
        first_ten=simulate(frames[step],meta,chosen["candidate"],test_start,end,step,config,trade_limit=10)
        result={"selection":selection,"holdout":selected,"cost_stress":stress,"first_ten_holdout":first_ten,
                "buy_hold":buy_hold(frames[step],meta,test_start,end,config),"cash_net_pnl":0,
                "not_prospective":True,"real_orders":0,"credentials_used":False,
                "limitations":["Historical candle fills, not live-book fills or exchange-confirmed trades.",
                    "Per-market historical spread is assumed at max(15 bps round trip, current measured spread); it is not historical book data.",
                    "Extra slippage 1 bp each side; fees 0.2% each side. Stress doubles spread/slippage.",
                    "Only one seven-day final holdout; small sample, no future profitability claim.",
                    "Original 10-trade live run covers a different period; it is not a matched-period control.",
                    "Target is a planned 2R net exit, not a forecast; stops are not guaranteed execution.",
                    "Observed drawdown is sampled at candle closes and fills; intrabar equity may be worse."]}
        (output/"holdout.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
        print(json.dumps({"holdout":compact(selected),"stress":compact(stress),"first_ten":compact(first_ten),"buy_hold":result["buy_hold"]},allow_nan=False),flush=True)


if __name__ == "__main__":
    main()
