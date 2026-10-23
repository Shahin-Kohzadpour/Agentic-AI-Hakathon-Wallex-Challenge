"""Render the sealed historical experiment, with independent ledger checks."""
import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def date(t):
    return datetime.fromtimestamp(t,timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ledger_check(result):
    cash=result["initial"]
    total_fees=0.
    previous_exit=-1
    for trade in result["trades_detail"]:
        q=trade["quantity"]
        assert trade["signal_available_at"] <= trade["opened_epoch"]
        assert previous_exit <= trade["opened_epoch"] <= trade["closed_epoch"]
        spent=q*trade["entry_price"]+trade["entry_fee"]
        received=q*trade["exit_price"]-trade["exit_fee"]
        assert abs(spent-trade["spent"])<1e-8
        assert spent<=100+1e-8 and spent<=cash+1e-8
        assert trade["planned_net_risk"] <= cash*.001+1e-8
        assert abs(received-spent-trade["net_pnl"])<1e-8
        total_fees+=trade["entry_fee"]+trade["exit_fee"]
        cash+=received-spent
        previous_exit=trade["closed_epoch"]
    assert abs(cash-result["final"])<1e-7
    assert abs(total_fees-result["fees"])<1e-7
    return {"cash_reconciles":True,"fees_reconcile":True,"risk_and_spend_caps":True,
            "no_future_signal_or_overlapping_position":True,"round_trips":len(result["trades_detail"])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data",type=Path)
    args=parser.parse_args()
    folder=args.data/"evaluation"
    result=json.loads((folder/"holdout.json").read_text())
    h=result["holdout"]
    checks={name:ledger_check(result[name]) for name in ("holdout","cost_stress","first_ten_holdout")}
    (folder/"ledger_check.json").write_text(json.dumps(checks,indent=2)+"\n")
    for name in ("holdout","cost_stress","first_ten_holdout"):
        trades=result[name]["trades_detail"]
        if trades:
            with (folder/f"{name}_trades.csv").open("w",newline="") as handle:
                writer=csv.DictWriter(handle,fieldnames=list(trades[0]))
                writer.writeheader()
                writer.writerows(trades)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig,(ax,dd)=plt.subplots(2,1,figsize=(12,7),sharex=True,gridspec_kw={"height_ratios":[3,1]},layout="constrained")
    for name,label in (("holdout","Selected strategy"),("cost_stress","Double spread/slippage")):
        trace=result[name]["equity"]
        x=[datetime.fromtimestamp(p["t"],timezone.utc) for p in trace]
        ax.plot(x,[p["equity"] for p in trace],label=label,lw=1.7)
        dd.plot(x,[-p["drawdown"]*100 for p in trace],label=label,lw=1.3)
    ax.axhline(1000,color="gray",ls="--",label="Cash")
    ax.set_title(f"Historical Wallex paper test · {h['candidate']} · {h['timeframe_seconds']//60}m\nAssumed fills and costs; zero real orders")
    ax.set_ylabel("Liquidation equity (USDT)")
    ax.legend()
    dd.set_ylabel("Drawdown %")
    dd.xaxis.set_major_formatter(mdates.DateFormatter("%b %d",tz=timezone.utc))
    dd.set_xlabel("UTC")
    for axis in (ax,dd):
        axis.grid(alpha=.2)
    fig.savefig(folder/"holdout_equity.png",dpi=150)
    plt.close(fig)
    lines=["# Indicator strategy experiment", "", "Historical evaluation; not a new prospective live session. No real orders.","",
           f"Selected for research using validation: **{h['candidate']}**, {h['timeframe_seconds']//60}-minute entries.",
           f"Passed original promotion criteria: **{result['selection'].get('promotion_criteria_passed',True)}**. Live strategy remains disabled.",
           f"Final holdout: {date(h['start'])} to {date(h['end'])}.","",
           "| Evaluation | Round trips | Net wins | Win rate | Final USDT | Net USDT | Observed drawdown |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for name,label in (("holdout","Full holdout"),("first_ten_holdout","First ten or fewer"),("cost_stress","Double spread/slippage")):
        r=result[name]
        wr=f"{r['win_rate_pct']:.1f}%" if r['win_rate_pct'] is not None else "N/A"
        lines.append(f"| {label} | {r['trades']} | {r['wins']} | {wr} | {r['final']:.4f} | {r['net_pnl']:+.4f} | {r['max_drawdown_pct']:.3f}% |")
    lines.extend(["",f"Holdout fees: {h['fees']:.4f} USDT. Profit factor: {h['profit_factor']}. Average net trade: {h['average_trade']} USDT.",
                  f"Cash benchmark net: 0.00 USDT. 100-USDT BTC holding reference net: {result['buy_hold']['net_pnl']:+.4f} USDT (different exposure and risk).", "",
                  "## Every validation candidate", "", "Validation was reused during development; only the final holdout is untouched by candidate selection.","",
                  "| Candidate | Minutes | Trades | Wins | Net USDT | Selected |", "|---|---:|---:|---:|---:|---|"])
    for r in result["selection"]["validation_results"]:
        chosen=r["candidate"]==h["candidate"] and r["timeframe_seconds"]==h["timeframe_seconds"]
        lines.append(f"| {r['candidate']} | {r['timeframe_seconds']//60} | {r['trades']} | {r['wins']} | {r['net_pnl']:+.4f} | {'yes' if chosen else ''} |")
    lines.extend(["","## Limits",""]+[f"- {x}" for x in result["limitations"]]+[
        "- The original three-pair eight-candidate validation failed and is preserved in the previous capture directory.",
        "- Twelve eligible present-day markets create possible survivorship bias; current spreads are proxies for past spreads.",
        "- Long indicators use observed candles; no synthetic candles fill gaps. Signals require recent contiguous data.",
        "- A small historical sample cannot establish a reliable future win rate.","",
        "Independent ledger checks: `ledger_check.json`. Full results and selection: `holdout.json`, `selection.json`.",
        "Per-trade CSVs: `holdout_trades.csv`, `first_ten_holdout_trades.csv`, `cost_stress_trades.csv`.","",
        "![Holdout equity](holdout_equity.png)",""])
    (folder/"RESULTS.md").write_text("\n".join(lines))
    print(json.dumps({"report":str(folder/"RESULTS.md"),"ledger_checks":checks},indent=2))


if __name__ == "__main__":
    main()
