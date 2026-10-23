"""Render completed public-data paper sessions using their captured observations."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("run_dir",type=Path)
    args=parser.parse_args()
    root=args.run_dir
    report=json.loads((root/"results.json").read_text())
    with sqlite3.connect((root/"trace.sqlite3").resolve().as_uri()+"?mode=ro",uri=True) as db:
        events=[(at,kind,json.loads(payload)) for at,kind,payload in db.execute("SELECT at,kind,payload FROM events ORDER BY seq")]
    equity=[(datetime.fromisoformat(at),float(p["equity_usdt"]),float(p["drawdown_pct"])) for at,kind,p in events if kind=="equity"]
    if not equity:
        raise SystemExit("No captured equity observations")
    plt.rcParams.update({"font.size":10,"axes.spines.top":False,"axes.spines.right":False})
    fig,(ax,dd)=plt.subplots(2,1,figsize=(11,6),sharex=True,gridspec_kw={"height_ratios":[3,1]},layout="constrained")
    x=[r[0] for r in equity]
    ax.plot(x,[r[1] for r in equity],color="#2563eb",linewidth=1.7,label="Paper equity, valued at bid")
    ax.axhline(float(report["initial_balance_usdt"]),color="#6b7280",linestyle="--",linewidth=1,label="Starting balance")
    ax.set_ylabel("USDT (USD proxy)")
    ax.ticklabel_format(axis="y",style="plain",useOffset=False)
    ax.set_title(f"Wallex live-data paper account · {report['completed_round_trips']} round trips · zero real orders\n"
                 f"Net {float(report['net_pnl_usdt']):+.4f} USDT · assumed fees {float(report['fees_usdt']):.4f} USDT")
    ax.grid(alpha=.2)
    ax.legend(loc="best",frameon=False)
    dd.fill_between(x,[-r[2] for r in equity],0,color="#ef4444",alpha=.3)
    dd.set_ylabel("Drawdown %")
    dd.set_xlabel("UTC on "+x[0].strftime("%Y-%m-%d"))
    dd.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M",tz=timezone.utc))
    dd.grid(alpha=.2)
    fig.savefig(root/"equity.png",dpi=160)
    plt.close(fig)
    books={}
    for at,kind,p in events:
        if kind!="book":
            continue
        bid=float(p["bids"][0][0]); ask=float(p["asks"][0][0])
        books.setdefault(p["symbol"],[]).append((datetime.fromisoformat(at),(bid+ask)/2))
    traded=sorted({t["symbol"] for t in report["trades"]})
    fig,axes=plt.subplots(max(len(traded),1),1,figsize=(11,max(3.5,3.2*len(traded))),squeeze=False,layout="constrained")
    for symbol,ax in zip(traded,axes[:,0]):
        observations=books.get(symbol,[])
        ax.plot([p[0] for p in observations],[p[1] for p in observations],color="#475569",linewidth=1,label="Observed book midpoint")
        trades=[t for t in report["trades"] if t["symbol"]==symbol]
        ax.scatter([datetime.fromisoformat(t["opened_at"]) for t in trades],[float(t["entry_price"]) for t in trades],
                   marker="^",color="#16a34a",s=45,label="Simulated buy VWAP + slippage",zorder=3)
        ax.scatter([datetime.fromisoformat(t["closed_at"]) for t in trades],[float(t["exit_price"]) for t in trades],
                   marker="v",color="#dc2626",s=45,label="Simulated sell VWAP − slippage",zorder=3)
        for t in trades:
            ax.annotate(str(t["trade"]),(datetime.fromisoformat(t["opened_at"]),float(t["entry_price"])),xytext=(4,5),textcoords="offset points",fontsize=8)
        ax.set_title(symbol+" · real public quotes, simulated fills")
        ax.set_ylabel("USDT per coin")
        ax.set_xlabel("UTC")
        ax.ticklabel_format(axis="y",style="plain",useOffset=False)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M",tz=timezone.utc))
        ax.grid(alpha=.2)
        ax.legend(loc="best",frameon=False,fontsize=8)
    fig.savefig(root/"live_prices_and_fills.png",dpi=160)
    plt.close(fig)
    trades=report["trades"]
    fig,ax=plt.subplots(figsize=(11,4),layout="constrained")
    indices=list(range(1,len(trades)+1))
    ax.bar([i-.22 for i in indices],[float(t["gross_pnl_usdt"]) for t in trades],width=.22,label="Gross after spread/slippage",color="#60a5fa")
    ax.bar(indices,[-float(t["entry_fee"])-float(t["exit_fee"]) for t in trades],width=.22,label="Assumed fees",color="#fbbf24")
    ax.bar([i+.22 for i in indices],[float(t["net_pnl_usdt"]) for t in trades],width=.22,label="Net",color="#ef4444")
    ax.axhline(0,color="#475569",linewidth=.8)
    ax.set_xticks(indices)
    ax.set_xlabel("Completed paper round trip")
    ax.set_ylabel("USDT")
    ax.set_title("Per-trade P&L and assumed transaction costs")
    ax.grid(axis="y",alpha=.2)
    ax.legend(frameon=False)
    fig.savefig(root/"trade_pnl.png",dpi=160)
    plt.close(fig)
    report["charts"]=["equity.png","live_prices_and_fills.png","trade_pnl.png"]
    (root/"results.json").write_text(json.dumps(report,indent=2)+"\n")
    markdown=root/"RESULTS.md"
    text=markdown.read_text()
    if "![Paper equity]" not in text:
        markdown.write_text(text+"\n![Paper equity](equity.png)\n\n![Real quotes and paper fills](live_prices_and_fills.png)\n\n![Trade costs](trade_pnl.png)\n")
    print(json.dumps({"run":str(root),"charts":report["charts"]},indent=2))


if __name__ == "__main__":
    main()
