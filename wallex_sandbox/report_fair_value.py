"""Read-only snapshot of an ongoing/completed fair-value paper journal."""
import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def stamp(epoch):
    return datetime.fromtimestamp(epoch,timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory",type=Path)
    args=p.parse_args()
    status=json.loads((args.directory/"status.json").read_text())
    db=sqlite3.connect("file:"+quote(str((args.directory/"trace.sqlite3").resolve()))+"?mode=ro",uri=True)
    try:
        row=db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        state=json.loads(row[0])
        config=state["fair_value_config"]
        latest={}
        for raw, in db.execute("SELECT payload FROM events WHERE kind='fair_decision' ORDER BY seq DESC LIMIT 20"):
            decision=json.loads(raw)
            latest.setdefault(decision["symbol"],decision)
        errors={}
        for raw, in db.execute("SELECT payload FROM events WHERE kind='fair_data_error'"):
            reason=json.loads(raw)["reason"]
            errors[reason]=errors.get(reason,0)+1
    finally:
        db.close()
    lines=["# Prospective fair-value paper session", "", f"Status: **{status['status']}**. Mode: {status['mode']}.",
           f"Snapshot: {status['at']}. Deadline: {stamp(status['deadline_epoch'])}.","",
           "No exchange orders or money transfers. Wallex fills are local taker simulations using observed depth.","",
           f"Initial: {status['initial_cash_usdt']} USDT. Cash: {status['cash_usdt']} USDT.",
           f"Last liquidation equity: {status['last_liquidation_equity_usdt']} USDT; mark time: {stamp(status['equity_mark_epoch']) if status['equity_mark_epoch'] else 'not available'}.",
           f"Completed round trips: {status['completed_round_trips']}. Net winners: {status['winning_round_trips']}.",
           f"Realized net: {status['realized_net_usdt']} USDT. Fees: {status['fees_usdt']} USDT.",
           f"Open paper position: {status['open_position']['symbol'] if status['open_position'] else 'none'}.","",
           "A zero-trade balance is an observation, not proof of trading profitability. Cash is not equity when a position is open.","",
           "## Calibration and latest decisions", "",
           "| Market | Prior samples | Span seconds | Normal local premium | Robust sigma, bps | Latest rejection / eligibility |",
           "|---|---:|---:|---:|---:|---|"]
    for symbol,baseline in status["baselines"].items():
        normal=f"{math.expm1(baseline['median_log_basis'])*100:+.4f}%" if "median_log_basis" in baseline else "N/A"
        sigma=f"{baseline['sigma']*10000:.3f}" if "sigma" in baseline else "N/A"
        decision=latest.get(symbol,{})
        reason="eligible" if decision.get("eligible") else ", ".join(decision.get("reasons",["no valid snapshot"]))
        lines.append(f"| {symbol} | {baseline['samples']} | {baseline['span_seconds']:.1f} | {normal} | {sigma} | {reason} |")
    hurdle=max(float(config["minimum_net_target"]),float(config["minimum_reward_risk"])*float(config["stop_net_fraction"]))*100
    lines.extend(["",f"Calibration requires at least {config['minimum_baseline_samples']} accepted samples over {config['warmup_seconds']} seconds, using past observations only.",
                  f"Required potential net return at estimated normal basis: {hurdle:.2f}%, after fees, depth, slippage, exit-spread reserve and basis uncertainty.",
                  "That target is conditional on future convergence and is not a demonstrated expected return.","",
                  "## Costs and data quality", "",
                  f"Fee schedule: `{json.dumps(status['fee_schedule'],sort_keys=True)}`.",
                  f"Accepted market snapshots: {status['snapshots']}. Rejected data snapshots/errors: {status['data_errors']}.",
                  f"Rejection reasons: `{json.dumps(errors,sort_keys=True)}`.","",
                  "Reference prices use OKX and Bybit spot books quoted in USDT. No USD/USDT conversion is silently assumed.",
                  "Receipts must be fresh, snapshot skew bounded, and reference venues in agreement. Wallex has no verified book-event timestamp in this adapter.",
                  "A persistent local premium is normalized using the past log-basis median, not labeled a free arbitrage opportunity.",
                  "Reference outages block new entries while owned-position exits can re-read Wallex independently.","",
                  "Files: `status.json`, `trace.sqlite3`, frozen `source_snapshot/`, and `results.json`/`trades.csv` after completion when applicable.",""])
    output=args.directory/"REPORT.md"
    output.write_text("\n".join(lines))
    print(json.dumps({"report":str(output),"status":status["status"],"snapshots":status["snapshots"],
                      "trades":status["completed_round_trips"],"real_orders":0},indent=2))


if __name__=="__main__":
    main()
