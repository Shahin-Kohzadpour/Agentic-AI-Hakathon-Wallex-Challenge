"""Report recency iteration, including the losing period and tiny recent sample."""
import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .report_candidates import ledger_check
from .research_readiness import assess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    folder=args.data/"adaptive_returns"
    validation=json.loads((folder/"validation.json").read_text())
    diagnostic=json.loads((folder/"diagnostic.json").read_text())
    controls=json.loads((args.data/"learned_returns/matched_controls.json").read_text())
    records=validation["records"]+diagnostic["records"]
    fit_count=0
    audit={}
    lines=["# Weekly learned-return iteration", "", "**Exploratory historical research on previously inspected periods. No real orders.**", "",
           "This iteration was motivated by the monthly model's lag. It retrains every Monday on 90 days",
           "of training followed by 14 separate calibration days. All other model, cost and risk settings stay fixed.","",
           "| Phase | Model | Trades | Wins | Win rate | Net USDT | Stressed net | Profit factor | Observed DD |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in records:
        name=f"{r['phase']}_{r['model']}"
        for case in ("base","stress"):
            audit[name+"_"+case]=ledger_check(r[case])
            trades=r[case]["trades_detail"]
            if trades:
                with (folder/f"{name}_{case}_trades.csv").open("w",newline="") as f:
                    writer=csv.DictWriter(f,fieldnames=list(trades[0]))
                    writer.writeheader()
                    writer.writerows(trades)
        for fit in r["weekly_fits"]:
            if "model_file" not in fit:
                continue
            assert fit["training_labels_end"]<fit["calibration_start"]
            assert fit["calibration_labels_end"]<fit["fit_cutoff"]
            assert hashlib.sha256((folder/"models"/fit["model_file"]).read_bytes()).hexdigest()==fit["model_sha256"]
            fit_count+=1
        b=r["base"]
        wr=f"{b['win_rate_pct']:.1f}%" if b['win_rate_pct'] is not None else "N/A"
        pf=f"{b['profit_factor']:.3f}" if b['profit_factor'] is not None else "N/A"
        lines.append(f"| {r['phase']} | {r['model']} | {b['trades']} | {b['wins']} | {wr} | {b['net_pnl']:+.4f} | {r['stress']['net_pnl']:+.4f} | {pf} | {b['max_drawdown_pct']:.3f}% |")
    earlier=next(r for r in diagnostic["records"] if r["phase"]=="diagnostic")
    recent=next(r for r in diagnostic["records"] if r["phase"]=="recent_previously_seen")
    selected=next(r for r in validation["records"] if r["model"]==earlier["model"])
    old=next(r for r in controls["records"] if r["phase"]=="historical_check" and r["horizon_hours"]==48)
    new=next(r for r in controls["records"] if r["phase"]=="recent_previously_seen" and r["horizon_hours"]==48)
    readiness=assess(selected,earlier,recent,old,new)
    (folder/"readiness.json").write_text(json.dumps(readiness,indent=2)+"\n")
    (folder/"audit.json").write_text(json.dumps({"wallets":audit,"weekly_fit_cutoffs_and_model_hashes_verified":fit_count},indent=2)+"\n")
    lines.extend(["", "The recent 3/3 wins are a tiny sample, **not evidence of a dependable 100% win rate**.",
        "The same strategy lost in January–June 2026. Positive validation and one small green slice do not establish a durable edge.","",
        f"Approved for a paper forward candidate: **{readiness['approved_for_paper_forward_test']}**. Real-money execution remains disabled.",""]+
        [f"- {x}" for x in readiness["rejections"]]+["", "## Matched controls", "",
        f"The 48h momentum control made {old['base']['net_pnl']:+.4f} USDT in January–June and {new['base']['net_pnl']:+.4f} USDT in the recent July–October diagnostic.",
        "It shares the liquidity filter, cost assumptions, 1,000-USDT wallet, 100-USDT entry cap and 0.1% planned account risk.","",
        "## Integrity and limits", "",
        "- Every training label ends before calibration starts; every calibration label ends before the weekly fit cutoff.",
        "- The replay predictions use past data, but the strategy architecture was adjusted after inspecting these periods. This is not a virgin holdout or prospective live result.",
        "- Current market selection/spreads proxy past conditions. Actual fee tier was not verified; fees remain 0.2% per side.",
        "- OHLC fills, gap exits, extra slippage and sampled drawdown are simulations. Historical depth impact is unavailable.",
        "- Risk was not increased to improve the headline. Doubling spread/slippage is reported for every case.",
        "- Both candidate models and all failed cases remain saved. No exchange orders, paid model API or external messages were used.","",
        f"Verified {fit_count} weekly model hashes/cutoffs and all completed trade ledgers. Predictions, serialized models, JSON traces and CSV trades are adjacent.","",
        "![Adaptive historical equity](equity.png)",""])
    (folder/"RESULTS.md").write_text("\n".join(lines))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout="constrained")
    for ax,r,title in ((axes[0],earlier,"Jan–Jun 2026: losing diagnostic"),(axes[1],recent,"Jul–Oct 2026: only three completed trades")):
        for key,label in (("base","Weekly model"),("stress","Double spread/slippage")):
            trace=r[key]["equity"]
            ax.plot([datetime.fromtimestamp(x["t"],timezone.utc) for x in trace],[x["equity"]-1000 for x in trace],label=label)
        ax.axhline(0,ls=":",color="gray",label="Cash")
        ax.set_title(title+" · exploratory simulated fills")
        ax.set_ylabel("Net equity change, USDT")
        ax.grid(alpha=.2)
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d",tz=timezone.utc))
    axes[-1].set_xlabel("UTC · not a live trading track record")
    fig.savefig(folder/"equity.png",dpi=150)
    plt.close(fig)
    print(json.dumps({"report":str(folder/"RESULTS.md"),"verified_weekly_fits":fit_count,"readiness":readiness},indent=2))


if __name__=="__main__":
    main()
