"""Single untouched holdout for the best UNQUALIFIED research candidate.

Does not promote or enable a strategy. Preserves the failed validation selection.
The explicit fallback is documented rather than relabeling failed criteria.
"""
import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .backtest_candidates import Simulation, buy_hold, compact, load_frames, simulate


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    folder=args.data/"evaluation"
    selection=json.loads((folder/"selection.json").read_text())
    if selection["selected"] is not None:
        raise ValueError("Use the ordinary qualified-candidate holdout path")
    if (folder/"holdout.json").exists() or (folder/"research_selection.json").exists():
        raise ValueError("Research selection/holdout already frozen; cannot search it repeatedly")
    for name,expected in selection["source_hashes"].items():
        if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()!=expected:
            raise ValueError("Simulation changed since validation")
    if hashlib.sha256((args.data/"manifest.json").read_bytes()).hexdigest()!=selection["data_manifest_sha256"]:
        raise ValueError("Historical dataset changed")
    options=[r for r in selection["validation_results"] if r["trades"]>=3]
    if not options:
        raise ValueError("No research candidate with at least three validation trades")
    options.sort(key=lambda r:(-r["net_pnl"],r["max_drawdown_pct"],r["candidate"]))
    chosen=options[0]
    research={**selection,"selected":chosen,"promotion_criteria_passed":False,
              "reason":"Best validation net P&L among candidates with at least three trades; exploratory evaluation only, original positive-profit/minimum-five criteria failed",
              "research_frozen_at":datetime.now(timezone.utc).isoformat(),
              "research_evaluator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "live_strategy_enabled":False}
    (folder/"research_selection.json").write_text(json.dumps(research,indent=2)+"\n")
    print(json.dumps({"research_selection_frozen":compact(chosen),"qualified":False}),flush=True)
    frames,metadata,manifest=load_frames(args.data)
    start,end,step=selection["holdout_start"],selection["holdout_end"],chosen["timeframe_seconds"]
    config=Simulation(**chosen["config"])
    held=simulate(frames[step],metadata,chosen["candidate"],start,end,step,config)
    stressed=Simulation(**{**asdict(config),"half_spread_bps":15,"slippage_bps":2,
                          "symbol_half_spread_bps":{s:v*2 for s,v in config.symbol_half_spread_bps.items()}})
    stress=simulate(frames[step],metadata,chosen["candidate"],start,end,step,stressed)
    ten=simulate(frames[step],metadata,chosen["candidate"],start,end,step,config,trade_limit=10)
    result={"selection":research,"holdout":held,"cost_stress":stress,"first_ten_holdout":ten,
            "buy_hold":buy_hold(frames[step],metadata,start,end,config),"cash_net_pnl":0,
            "not_prospective":True,"real_orders":0,"credentials_used":False,
            "limitations":["Exploratory candidate FAILED the original promotion criteria; it was chosen before opening this holdout and is not enabled for live trading.",
                "Historical candle fills, not prospective live fills or exchange-confirmed trades.",
                "Historical spread assumption is max(15 bps round trip,current measured spread); historical books are unavailable.",
                "Fees 0.2% each side; additional slippage 1 bp each side. Stress doubles spread and slippage.",
                "One seven-day holdout and multiple development candidates; a positive result is not proof of a durable edge.",
                "Original live 10-trade run covers another period; the return comparison is descriptive, not matched-period evidence.",
                "Observed drawdown uses candle closes and fills; intrabar drawdown can be worse."]}
    (folder/"holdout.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps({"holdout":compact(held),"stress":compact(stress),"first_ten":compact(ten)},allow_nan=False),flush=True)


if __name__ == "__main__":
    main()
