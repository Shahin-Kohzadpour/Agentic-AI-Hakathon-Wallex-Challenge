"""Diagnostic for the other prequalified variant after the primary failed.

This explicitly follows inspection of the primary check; it is not a fresh
single-selection holdout. Rules/configuration have not been retuned.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .managed_trend import epoch, evaluate, prepare


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    folder=args.data/"managed_trend"
    target=folder/"alternate_check.json"
    if target.exists():
        raise ValueError("Alternate already checked")
    val=json.loads((folder/"validation.json").read_text())
    alternate=next(r for r in val["records"] if r["name"]=="retained_regime48")
    if not (alternate["base"]["net_pnl"]>0 and alternate["stress"]["net_pnl"]>0 and alternate["base"]["trades"]>=10):
        raise ValueError("Alternate did not pass the recorded qualification")
    spec=json.loads(Path(__file__).with_name("managed_trend_experiment.json").read_text())
    frames,meta,manifest=prepare(args.data)
    result={"phase_note":__doc__,"name":"retained_regime48","real_orders":0,"records":[]}
    for phase in ("secondary_historical_check","recent_seen_diagnostic"):
        start,end=map(epoch,spec[phase])
        r=evaluate(frames,meta,manifest,"retained_regime48",start,end)
        r["phase"]=phase
        result["records"].append(r)
        print(json.dumps({"phase":phase,"net_pnl":r["base"]["net_pnl"],"stress_net":r["stress"]["net_pnl"],
                          "trades":r["base"]["trades"],"wins":r["base"]["wins"]}),flush=True)
    target.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    main()
