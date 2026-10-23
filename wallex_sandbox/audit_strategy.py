"""Read-only live signal audit; never calls paper entry/exit or exchange orders."""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .live_paper import Config, PublicFeed, Run, Session, d, utc


async def audit(args):
    directory=Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("strategy_audit_%Y%m%dT%H%M%SZ")
    run=Run(directory,Config())
    old=json.loads(args.baseline.read_text())
    old_trades=old["trades"]
    # This tests just the new cost gate on the old decisions, not the entire
    # revised model and not a prospective or independently validated return.
    rejected=sum(d(t["selected_estimated_net_return"]) < d(run.config.minimum_net_edge) for t in old_trades)
    summary={"audit_started_at":utc(),"mode":"READ_ONLY_SIGNAL_AUDIT_NOT_TRADING_PERFORMANCE",
             "baseline_file":str(args.baseline),"baseline_sha256":hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
             "old_round_trips":len(old_trades),"old_net_pnl_usdt":old["net_pnl_usdt"],
             "old_decisions_rejected_by_cost_gate":rejected,
             "baseline_comparison_caveat":"Retrospective veto of old decisions; not a new earned return.",
             "fee_rate_each_side":run.config.fee_rate,"horizon_minutes":15,"scans":[],
             "real_orders":0,"paper_fills":0,"credentials_used":False}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                    connector=aiohttp.TCPConnector(limit=4),cookie_jar=aiohttp.DummyCookieJar(),
                    headers={"User-Agent":"Condor-Wallex-PaperAudit/0.2","Cache-Control":"no-cache"},trust_env=True) as http:
            session=Session(run,PublicFeed(http,run))
            await session.setup()
            for index in range(args.scans):
                try:
                    selected=await session.select()
                    event=run.db.execute("SELECT payload FROM events WHERE kind='model_decision' ORDER BY seq DESC LIMIT 1").fetchone()
                    decision=json.loads(event[0]) if event else {}
                    scan={"at":utc(),"selected":selected["symbol"] if selected else None,
                          "candidates":[{"symbol":c["symbol"],"lower_forecast_return":c["model"]["forecast_lower_return"],
                          "gross_forecast_return":c["model"]["forecast_gross_return"],
                          "estimated_net_return":c["selection_score_net"],"cost_return":c["estimated_cost_return"],
                          "rejections":c["entry_rejections"],"validation":c["model"]["validation"]}
                          for c in decision.get("candidates",[])]}
                except (ValueError,RuntimeError,aiohttp.ClientError,asyncio.TimeoutError) as exc:
                    scan={"at":utc(),"error":str(exc),"selected":None}
                summary["scans"].append(scan)
                print(json.dumps({"scan":index+1,**scan}),flush=True)
                if index+1 < args.scans:
                    await asyncio.sleep(args.interval)
            summary["public_get_requests"]=session.feed.requests
        summary["audit_finished_at"]=utc()
        summary["candidate_errors"]=run.state["data_errors"]
        summary["qualified_live_signals"]=sum(s["selected"] is not None for s in summary["scans"])
        summary["cash_usdt"]="1000"
        summary["cash_caveat"]="No entries were executed in this signal audit; cash is not a measured strategy return."
        (directory/"audit.json").write_text(json.dumps(summary,indent=2)+"\n")
        run.state["status"]="read_only_audit_complete"
        run.event("audit_finished",summary,save=True)
        run.status()
        print(json.dumps({"report":str(directory/"audit.json"),"qualified_live_signals":summary["qualified_live_signals"]}),flush=True)
    finally:
        run.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scans",type=int,default=3)
    parser.add_argument("--interval",type=int,default=30)
    parser.add_argument("--baseline",type=Path,default=Path("wallex_sandbox/artifacts/livepaper_20261023T062023Z/results.json"))
    args=parser.parse_args()
    if args.scans < 1 or args.interval < 0:
        parser.error("Invalid scan count/interval")
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    for key in list(os.environ):
        if any(s in key.upper() for s in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(audit(args))


if __name__ == "__main__":
    main()
