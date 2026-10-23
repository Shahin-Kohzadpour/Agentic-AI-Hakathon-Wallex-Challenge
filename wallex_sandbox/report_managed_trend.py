"""Report every frozen variant and independent historical check without filtering."""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .report_candidates import date, ledger_check


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("data",type=Path)
    args=p.parse_args()
    folder=args.data/"managed_trend"
    phases={name:json.loads((folder/f"{name}.json").read_text()) for name in ("development","validation","check")}
    datasets={"development":phases["development"]["records"],"validation":phases["validation"]["records"],
              "earlier_uninspected":phases["check"]["earlier_uninspected"],
              "recent_previously_seen":phases["check"]["recent_previously_seen"]}
    audits={}
    lines=["# Managed trend study", "", "Historical, simulated fills only. No real orders, credentials or new live trades.","",
           f"Qualified on validation: **{phases['validation']['qualified']}**. Predeclared primary: **trail4h72**.","",
           "Every variant uses 1,000 USDT, at most 100 USDT all-in per entry and 0.1% planned account risk.",
           "Fees remain 0.2% each side. Stress doubles spread and additional slippage; it does not lower fees.","",
           "| Phase | Candidate | Trades | Net wins | Win rate | Net USDT | Stressed net | Profit factor | Observed DD |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for phase,records in datasets.items():
        for record in records:
            r=record["base"]
            stress=record["stress"]
            wr=f"{r['win_rate_pct']:.1f}%" if r['win_rate_pct'] is not None else "N/A"
            pf=f"{r['profit_factor']:.3f}" if r['profit_factor'] is not None else "N/A"
            lines.append(f"| {phase} | {record['name']} | {r['trades']} | {r['wins']} | {wr} | {r['net_pnl']:+.4f} | {stress['net_pnl']:+.4f} | {pf} | {r['max_drawdown_pct']:.3f}% |")
            for key in ("base","stress"):
                name=f"{phase}_{record['name']}_{key}"
                audits[name]=ledger_check(record[key])
                trades=record[key]["trades_detail"]
                if trades:
                    with (folder/f"{name}_trades.csv").open("w",newline="") as handle:
                        w=csv.DictWriter(handle,fieldnames=list(trades[0]))
                        w.writeheader()
                        w.writerows(trades)
    lines.extend(["","## Same-period references", "",
                  "The BTC reference spends 100 USDT once and holds; actual exposure and stop risk differ from the active systems.","",
                  "| Phase | Cash net USDT | 100-USDT BTC holding net USDT |", "|---|---:|---:|"])
    for phase,records in datasets.items():
        baseline=next(r for r in records if r["name"]=="baseline48")
        lines.append(f"| {phase} | 0 | {baseline['buy_hold_reference']['net_pnl']:+.4f} |")
    lines.extend(["","## Periods and interpretation", "",
        "- Development: January–June 2025. Validation/selection: July–December 2025.",
        "- Additional historical check: January–June 2026, whose raw prices had not been inspected in the earlier study.",
        "- Recent diagnostic: July 25–October 23, 2026. That period was already inspected; it is explicitly not a fresh holdout.",
        "- The strategy ideas were developed in October 2026, after seeing later market behavior. Even an uninspected earlier-price check does not reproduce a prospective experiment run before that period.",
        "- Baseline uses the same newly captured warmup history as the new variants. Its results can differ from the old +0.8822 replay, which had less warmup.",
        "- Validation qualification is fixed before the additional check. If no candidate qualifies, the predeclared primary still gets a research check; it is not relabeled qualified.","",
        "## Implemented controls", "",
        "Current spread proxy above 30 bps excludes new managed entries. BTC's completed 4h trend controls regime eligibility.",
        "The retained candidate keeps valid positions through the old 48h timeout, capped at 72h, and exits on regime loss.",
        "The primary uses completed 4h trend and ATR, a 2-ATR initial stop and a 3-ATR trailing stop updated only from prior closed hourly prices.",
        "There is no fixed profit cap. Positions exit on weakening trends or a 72h hard limit. A separate pullback variant is reported too.","",
        "## Limitations", "",
        "- Historical spreads are frozen current proxies, not historical order books. Present-day eligible markets create survivorship and selection assumptions.",
        "- Hourly OHLC fills cannot reconstruct queue priority or intrabar execution. Stop/target ambiguity remains adverse-first; gaps fill at the observed open.",
        "- Drawdown is observed at closes and fills; intrabar equity can be worse. Stops do not guarantee the planned dollar risk.",
        "- Stress fixes entry signals while changing execution costs and risk-sized quantities. It is a robustness test, not a second independent sample.",
        "- Signals are deterministic rules, not an LLM trading track record. None of these scripts can submit an exchange order.",
        "- No result guarantees a future win rate or return. Real-money trading is not enabled.","",
        "All source/data hashes and the experiment specification are in `frozen.json`. `development.json`, `validation.json` and `check.json` retain all trades and equity traces.",
        "Independent wallet, fee, timing and risk checks are in `ledger_checks.json`; every case has a trade CSV.","",
        "![Historical equity](equity.png)",""])
    (folder/"ledger_checks.json").write_text(json.dumps(audits,indent=2)+"\n")
    (folder/"RESULTS.md").write_text("\n".join(lines))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout="constrained")
    for ax,(phase,title) in zip(axes,(("earlier_uninspected","Additional historical check · Jan–Jun 2026"),
                                    ("recent_previously_seen","Previously inspected diagnostic · Jul–Oct 2026"))):
        for record in datasets[phase]:
            trace=record["base"]["equity"]
            x=[datetime.fromtimestamp(r["t"],timezone.utc) for r in trace]
            ax.plot(x,[r["equity"]-1000 for r in trace],label=record["name"])
            if record["name"]=="trail4h72":
                trace=record["stress"]["equity"]
                ax.plot([datetime.fromtimestamp(r["t"],timezone.utc) for r in trace],
                        [r["equity"]-1000 for r in trace],ls="--",label="primary · doubled friction")
        ax.axhline(0,ls=":",color="gray",label="cash")
        ax.set_title(title+" · simulated fills")
        ax.set_ylabel("Net equity change, USDT")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d",tz=timezone.utc))
        ax.grid(alpha=.2)
        ax.legend()
    axes[-1].set_xlabel("UTC · no real money")
    fig.savefig(folder/"equity.png",dpi=150)
    plt.close(fig)
    print(json.dumps({"report":str(folder/"RESULTS.md"),"ledger_cases_checked":len(audits)},indent=2))


if __name__ == "__main__":
    main()
