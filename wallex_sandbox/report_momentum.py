"""Export all momentum study variants, including stress and failed prior tests."""
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
    folder=args.data/"momentum_exploratory"
    result=json.loads((folder/"results.json").read_text())
    checks={}
    for v in result["results"]:
        for name in ("result","stress","first_ten"):
            r=v[name]
            key=f"{v['holding_hours']}h_{name}"
            checks[key]=ledger_check(r)
            with (folder/f"{key}_trades.csv").open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=list(r["trades_detail"][0]))
                w.writeheader()
                w.writerows(r["trades_detail"])
    (folder/"ledger_checks.json").write_text(json.dumps(checks,indent=2)+"\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig,axes=plt.subplots(2,1,figsize=(12,8),sharex=True,layout="constrained")
    for ax,v in zip(axes,result["results"]):
        for key,label in (("result","Base costs"),("stress","Double spread/slippage")):
            trace=v[key]["equity"]
            x=[datetime.fromtimestamp(e["t"],timezone.utc) for e in trace]
            ax.plot(x,[e["equity"]-1000 for e in trace],label=label,lw=1.6)
        ax.axhline(0,color="gray",ls="--",label="Cash")
        ax.set_ylabel("Net equity change (USDT)")
        ax.set_title(f"{v['holding_hours']}-hour momentum · all trades · historical simulated fills")
        ax.grid(alpha=.2)
        ax.legend()
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d",tz=timezone.utc))
    axes[-1].set_xlabel("UTC · exploratory replay, not a fresh live session")
    fig.savefig(folder/"equity.png",dpi=150)
    plt.close(fig)
    lines=["# Lower-turnover momentum results", "", "**Exploratory historical backtest. No real money or exchange orders.**", "",
           "These rules were fixed before this replay, after the earlier indicator development/validation results had been inspected.",
           "This is not a new untouched holdout or a prospective live result. All variants and previous failures are retained.","",
           "| Variant | Trades | Wins | Win rate | Final USDT | Net USDT | Fees | Profit factor | Observed DD |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for v in result["results"]:
        for key,label in (("result",f"{v['holding_hours']}h full"),("stress",f"{v['holding_hours']}h stress"),("first_ten",f"{v['holding_hours']}h first ten or fewer")):
            r=v[key]
            lines.append(f"| {label} | {r['trades']} | {r['wins']} | {r['win_rate_pct']:.1f}% | {r['final']:.4f} | {r['net_pnl']:+.4f} | {r['fees']:.4f} | {r['profit_factor']:.3f} | {r['max_drawdown_pct']:.3f}% |")
    first=result["results"][0]["result"]
    lines.extend(["",f"Replay period: {date(first['start'])} through {date(first['end'])}.",
        f"First eligible actual entry: {date(first['trades_detail'][0]['opened_epoch'])}; earlier data supports indicator warmup.","",
        "## What changed", "",
        "At scheduled UTC midnights, require positive 7-day and 28-day returns and a close above the hourly EMA200.",
        "Seven-day movement must exceed twice assumed round-trip costs. Rank eligible markets by cost-adjusted",
        "seven-day return divided by recent volatility. Trade one spot-long position; hold up to 48 hours or,",
        "for the separate reference, 168 hours. Use a stop at two daily ATR and a 2R net target.","",
        "Risk stays at 0.1% of current equity (about 1 USDT initially), with a maximum 100-USDT all-in spend.",
        "Daily ATR makes many positions smaller than 100 USDT. Fees remain 0.2% each side. Historical spread",
        "uses max(15 bps round trip, current measured per-market spread), with another 1 bp adverse slippage",
        "per side. These are assumptions about past execution, not reconstructed historical books.","",
        "## Interpretation", "",
        "The 48h full replay has positive net P&L and more than zero winning trades. Its first ten trades",
        "look substantially better than the complete 28-trade series; use the full series for assessment.",
        "The 48h result turns negative under doubled execution friction. The weekly reference stays positive",
        "under that stress but cannot complete a full weekly holding period inside a three-day challenge.","",
        "Both positive full-period results were driven by August gains; September and October-to-date lost equity.",
        "For the 48h case, monthly liquidation-equity changes were approximately +2.9804, -1.1079 and -0.9903 USDT.",
        "This is evidence of regime dependence, not an established improvement for today's market.","",
        f"The 100-USDT BTC holding reference made **{result['results'][0]['buy_hold']['net_pnl']:+.4f} USDT** over the same dates,",
        "more than either momentum variant. It holds a different exposure and accepts different risk;",
        "the study does not establish superiority over passive holding.","",
        "The original live 0/10 and -5.2956 USDT result used different dates and execution observations.",
        "Comparing it with this backtest is descriptive, not evidence of improved prospective performance.",
        "The earlier research candidate failed its final seven-day holdout: 0/2 wins, -1.3770 USDT.",
        "Its [full results](../evaluation/RESULTS.md), all validation candidates and initial failed selection remain available.","",
        "Other limits: current-market universe creates survivorship bias; sparse history and missing candles;",
        "close/fill drawdown sampling misses some intrabar risk; simulated targets/stops are not guaranteed fills.",
        "Stress keeps base entry signals fixed while changing fill costs and risk-sized quantities.",
        "No LLM was used in the new signal rules. These are rule-based research candidates; no real broker was enabled.","",
        "## Artifacts", "",
        "- `results.json`: all variant results, every trade, equity trace, configuration and hashes.",
        "- `frozen.json`: both variant specifications and source/data hashes recorded before evaluation.",
        "- `*_trades.csv`: complete fills and P&L for each case.",
        "- `ledger_checks.json`: independently recomputed wallet, fees, timing, spend and planned-risk checks.","",
        "![Both variants and execution-cost stress](equity.png)",""])
    (folder/"RESULTS.md").write_text("\n".join(lines))
    print(json.dumps({"report":str(folder/"RESULTS.md"),"ledger_checks":checks},indent=2))


if __name__ == "__main__":
    main()
