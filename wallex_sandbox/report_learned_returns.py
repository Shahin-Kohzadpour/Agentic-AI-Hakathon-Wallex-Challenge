"""Export full learned-model outcomes, model audits and readiness rejection."""
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
    folder=args.data/"learned_returns"
    val=json.loads((folder/"validation.json").read_text())
    checks=json.loads((folder/"check.json").read_text())
    controls=json.loads((folder/"matched_controls.json").read_text())
    audits={}
    monthly=0
    lines=["# Learned-return study", "", "Local, monthly refitted models; historical simulated fills only. No exchange orders.","",
        f"Validation-selected model: **{val['selected']}**.","",
        "| Phase | Model | Hours | Trades | Wins | Win rate | Net USDT | Stressed net | Profit factor | Observed DD |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for record in val["records"]+checks["records"]:
        key=f"{record['phase']}_{record['model']}_{record['horizon_hours']}"
        for case in ("base","stress"):
            audits[key+"_"+case]=ledger_check(record[case])
            trades=record[case]["trades_detail"]
            if trades:
                with (folder/f"{key}_{case}_trades.csv").open("w",newline="") as handle:
                    writer=csv.DictWriter(handle,fieldnames=list(trades[0]))
                    writer.writeheader()
                    writer.writerows(trades)
        for fit in record["monthly_fits"]:
            if "training_labels_end" not in fit:
                continue
            assert fit["training_labels_end"]<fit["calibration_start"]
            assert fit["calibration_labels_end"]<fit["fit_cutoff"]
            path=folder/"models"/f"{record['model']}_{record['horizon_hours']}_{fit['month'][:7].replace('-','')}.joblib"
            assert hashlib.sha256(path.read_bytes()).hexdigest()==fit["model_sha256"]
            monthly+=1
        r=record["base"]
        wr=f"{r['win_rate_pct']:.1f}%" if r['win_rate_pct'] is not None else "N/A"
        pf=f"{r['profit_factor']:.3f}" if r['profit_factor'] is not None else "N/A"
        lines.append(f"| {record['phase']} | {record['model']} | {record['horizon_hours']} | {r['trades']} | {r['wins']} | {wr} | {r['net_pnl']:+.4f} | {record['stress']['net_pnl']:+.4f} | {pf} | {r['max_drawdown_pct']:.3f}% |")
    historical=next(r for r in checks["records"] if r["phase"]=="historical_check")
    recent=next(r for r in checks["records"] if r["phase"]=="recent_previously_seen")
    selected=next(r for r in val["records"] if r["model"]==historical["model"] and r["horizon_hours"]==historical["horizon_hours"])
    control_check=next(r for r in controls["records"] if r["phase"]=="historical_check" and r["horizon_hours"]==historical["horizon_hours"])
    control_recent=next(r for r in controls["records"] if r["phase"]=="recent_previously_seen" and r["horizon_hours"]==recent["horizon_hours"])
    readiness=assess(selected,historical,recent,control_check,control_recent)
    (folder/"readiness.json").write_text(json.dumps(readiness,indent=2)+"\n")
    (folder/"audit.json").write_text(json.dumps({"wallet_checks":audits,"monthly_fit_cutoffs_and_model_hashes_verified":monthly},indent=2)+"\n")
    lines.extend(["","## Matched non-ML controls", "",
                  "These use the same liquidity filter, starting wallet, size/risk caps, daily ATR stop, decision times, holding horizon and costs.",
                  "They enter positive 7/28-day momentum above EMA200, rather than using a learned forecast. All control cases are retained.","",
                  "| Phase | Hours | Trades | Wins | Net USDT | Stressed net |", "|---|---:|---:|---:|---:|---:|"])
    for r in controls["records"]:
        lines.append(f"| {r['phase']} | {r['horizon_hours']} | {r['base']['trades']} | {r['base']['wins']} | {r['base']['net_pnl']:+.4f} | {r['stress']['net_pnl']:+.4f} |")
    lines.extend(["",f"**Approved for a future paper forward test: {readiness['approved_for_paper_forward_test']}. Real money enabled: False.**",""]+
                 [f"- {x}" for x in readiness["rejections"]]+["", "## Method", "",
        "Fixed Ridge and histogram gradient boosting models, at 24h and 48h horizons. Each UTC month refits on past data only.",
        "Of the trailing 180 days, 150 are training and 30 are calibration. Forward labels crossing cutoffs are purged.",
        "Calibration intervals do not overlap within each asset; assets can still be correlated. Overlapping training labels reduce the effective sample size.",
        "The model predicts next-open to horizon-open net returns after both fees and assumed execution costs.",
        "Positive overprediction is subtracted; calibration must contain at least five cost-qualified positive-mean entries before the month can trade.",
        "There is no random training/test split or hyperparameter search. Boosting uses fixed 100 iterations, seven leaves and disabled early stopping.",
        "Candidate decisions require predicted net return >=0.3%. A zero-trade month is an abstention, not a winning trade or established trading edge.","",
        "## Limits", "",
        "- The model was selected on July–December 2025 validation. January–June 2026 and July–October 2026 are separate historical diagnostics, but their prices had already been examined using other strategies. They are not virgin holdouts.",
        "- The positive validation result did not establish a durable edge: the chosen model abstained in the first check and lost in the recent period.",
        "- Historical quote spreads use frozen present-day proxies, and the present-day universe has selection/survivorship assumptions. Actual account fees remain unverified.",
        "- Simulated OHLC fills are not exchange-confirmed. Stops/gaps can exceed planned risk and historical depth impact is unavailable.",
        "- Stress holds entry signals fixed and doubles spread/slippage. No fee discount or higher risk produced these results.",
        "- This study is a local supervised model, not a real LLM trading track record or an enabled broker.","",
        "The wallet starts at 1,000 USDT; maximum entry spend is 100 USDT/10% equity and planned account risk is 0.1%. Fees remain 0.2% each side.",
        f"Verified {monthly} monthly fit cutoffs and serialized model hashes, plus every completed trade ledger.","",
        "Artifacts: `validation.json`, `check.json`, `matched_controls.json`, `readiness.json`, `audit.json`, `models/`, gzip prediction CSVs and per-trade CSVs.","",
        "![Model and matched controls](equity.png)",""])
    (folder/"RESULTS.md").write_text("\n".join(lines))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout="constrained")
    for ax,model,control,title in ((axes[0],historical,control_check,"Jan–Jun 2026 historical check"),
                                 (axes[1],recent,control_recent,"Jul–Oct 2026 previously seen diagnostic")):
        for r,label in ((model["base"],"Learned model"),(model["stress"],"Model · doubled friction"),(control["base"],"Matched momentum control")):
            trace=r["equity"]
            ax.plot([datetime.fromtimestamp(x["t"],timezone.utc) for x in trace],[x["equity"]-1000 for x in trace],label=label)
        ax.axhline(0,color="gray",ls=":",label="Cash")
        ax.set_title(title+" · historical simulated fills")
        ax.set_ylabel("Net equity change, USDT")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d",tz=timezone.utc))
        ax.grid(alpha=.2)
        ax.legend()
    axes[-1].set_xlabel("UTC · no real orders")
    fig.savefig(folder/"equity.png",dpi=150)
    plt.close(fig)
    print(json.dumps({"report":str(folder/"RESULTS.md"),"monthly_fit_audits":monthly,"readiness":readiness},indent=2))


if __name__=="__main__":
    main()
