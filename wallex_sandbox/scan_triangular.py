"""Public scan of Wallex quote inconsistencies; no orders or simulated fills.

Top-of-book margins are upper bounds before depth, precision and latency. A
positive number is a research candidate, never an executed profit or win rate.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .live_paper import Config, PublicFeed, Run, d, utc


async def scan():
    directory=Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("triangle_scan_%Y%m%dT%H%M%SZ")
    run=Run(directory,Config())
    result={"at":utc(),"real_orders":0,"simulated_fills":0,"credentials_used":False,"quotes":[],
            "fees":{"USDT_pair":"0.002","TMN_pair":"0.0035"},
            "limitation":"Upper-bound quoted conversion margins before depth/precision/latency, not executed P&L; fees assumed"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),connector=aiohttp.TCPConnector(limit=3),
                    trust_env=True,cookie_jar=aiohttp.DummyCookieJar(),headers={"User-Agent":"Wallex-Paper-QuoteResearch/0.1"}) as http:
            feed=PublicFeed(http,run)
            metadata=await feed.get("/hector/web/v1/markets")
            markets={x["symbol"] for x in metadata["result"]["markets"] if x.get("is_spot") is True}
            banned=set(json.loads(Path(__file__).with_name("excluded-markets.json").read_text())["excluded_pairs"])
            if "USDTTMN" not in markets or "USDT/TMN" in banned:
                raise ValueError("Conversion market unavailable or excluded")
            for asset in ("BTC","ETH","SOL","BNB","XRP","DOGE","ADA","LINK","AVAX","LTC","BCH","TRX"):
                if not {asset+"USDT",asset+"TMN"}<=markets or asset+"/USDT" in banned or asset+"/TMN" in banned:
                    continue
                try:
                    fx,usdt,tmn=await asyncio.gather(feed.book("USDTTMN"),feed.book(asset+"USDT"),feed.book(asset+"TMN"))
                    receipts=[b["received_epoch"] for b in (fx,usdt,tmn)]
                    if max(receipts)-min(receipts)>5 or time.time()-min(receipts)>10:
                        raise ValueError("Quotes too asynchronous")
                    fee_tmn=d("0.0035")
                    fee_usdt=d("0.002")
                    slip=d("0.9999")**3
                    a=fx["bids"][0][0]/tmn["asks"][0][0]*usdt["bids"][0][0]*(1-fee_tmn)**2*(1-fee_usdt)*slip-1
                    b=tmn["bids"][0][0]/usdt["asks"][0][0]/fx["asks"][0][0]*(1-fee_tmn)**2*(1-fee_usdt)*slip-1
                    quote={"asset":asset,"at":utc(),"quote_skew_seconds":max(receipts)-min(receipts),
                           "usdt_tmn_asset_usdt_upper_return_pct":str(a*100),
                           "usdt_asset_tmn_usdt_upper_return_pct":str(b*100)}
                except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,RuntimeError) as exc:
                    quote={"asset":asset,"at":utc(),"error_type":type(exc).__name__,"valid":False}
                result["quotes"].append(quote)
                print(json.dumps(quote),flush=True)
            result["public_get_requests"]=feed.requests
            result["positive_upper_bound_candidates"]=sum(max(d(q["usdt_tmn_asset_usdt_upper_return_pct"]),
                 d(q["usdt_asset_tmn_usdt_upper_return_pct"]))>0 for q in result["quotes"] if "error_type" not in q)
        run.state["status"]="quote_scan_complete"
        run.event("quote_scan_complete",result,save=True)
        run.status()
        (directory/"results.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps({"report":str(directory/"results.json"),"positive_candidates":result["positive_upper_bound_candidates"]}),flush=True)
    finally:
        run.close()


def main():
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    for key in list(os.environ):
        if any(x in key.upper() for x in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(scan())


if __name__=="__main__":
    main()
