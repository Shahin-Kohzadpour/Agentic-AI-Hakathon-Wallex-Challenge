"""Capture public Wallex history with raw audit trail; credentials never loaded."""
import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pandas as pd

from .live_paper import Config, PublicFeed, Run, Session, SYMBOLS, utc


def normalize(payload,start,end,step):
    if payload.get("s") == "no_data":
        return []
    if payload.get("s") != "ok":
        raise ValueError("History did not return ok or no_data")
    keys=("t","o","h","l","c","v")
    if len({len(payload.get(k,[])) for k in keys}) != 1:
        raise ValueError("Misaligned history arrays")
    out=[]
    for values in zip(*(payload[k] for k in keys)):
        t=int(values[0])
        if not start <= t or t+step > end:
            continue
        if t % step:
            raise ValueError("Candle timestamps do not align with requested interval")
        r=dict(zip(keys,[t]+[float(v) for v in values[1:]]))
        import math
        if (any(not math.isfinite(r[k]) for k in keys) or min(r[k] for k in ("o","h","l","c"))<=0
            or r["v"]<0 or r["h"]<max(r["o"],r["c"],r["l"]) or r["l"]>min(r["o"],r["c"],r["h"])):
            raise ValueError("Invalid OHLCV history")
        out.append(r)
    return out


def resample15(rows):
    if not rows:
        return []
    frame=pd.DataFrame(rows).sort_values("t")
    frame["bucket"]=frame.t//900*900
    out=[]
    for bucket,part in frame.groupby("bucket",sort=True):
        if part.t.tolist() != list(range(int(bucket),int(bucket)+900,60)):
            continue
        out.append({"t":int(bucket),"o":float(part.o.iloc[0]),"h":float(part.h.max()),
                    "l":float(part.l.min()),"c":float(part.c.iloc[-1]),"v":float(part.v.sum())})
    return out


async def collect(args):
    end=int(time.time())//3600*3600
    directory=Path(args.output) if args.output else Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("research_data_%Y%m%dT%H%M%SZ")
    run=Run(directory,Config())
    manifest={"created_at":utc(),"requested_days":args.days,"end":end,"symbols":{},
              "minute_query_resolution":15,"minute_actual_interval_seconds":60,"hour_resolution":60,
              "query_note":"15 returns minute-spaced candles on the observed endpoint; intervals are validated from timestamps, not the query label",
              "fifteen_minute_source":"strict aggregation of 15 contiguous public 1m candles",
              "real_orders":0,"credentials_used":False}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45),
                     connector=aiohttp.TCPConnector(limit=3),cookie_jar=aiohttp.DummyCookieJar(),trust_env=True,
                     headers={"User-Agent":"Condor-Wallex-PaperResearch/0.3"}) as http:
            session=Session(run,PublicFeed(http,run),symbols=args.symbols)
            await session.setup()
            for symbol in session.meta:
                market={"metadata":session.meta[symbol]}
                from .live_paper import spread_bps
                snapshot=await session.feed.book(symbol)
                market["spread_snapshot_bps"]=float(spread_bps(snapshot))
                market["spread_snapshot_epoch"]=snapshot["received_epoch"]
                for resolution,step,chunk_days in [(15,60,args.days),(60,3600,7)]:
                    found={}
                    failures=[]
                    start=end-(args.days+(12 if step == 3600 else 0))*86400
                    for left in range(start,end,chunk_days*86400):
                        right=min(end,left+chunk_days*86400)
                        for attempt in range(3):
                            try:
                                payload=await session.feed.get("/v1/udf/history",{"symbol":symbol,"resolution":resolution,"from":left,"to":right})
                                rows=normalize(payload,left,right,step)
                                for row in rows:
                                    key=row["t"]
                                    if key in found and found[key] != row:
                                        raise ValueError("Conflicting closed historical candles")
                                    found[key]=row
                                break
                            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,RuntimeError) as exc:
                                if attempt == 2:
                                    failures.append({"from":left,"to":right,"error":str(exc)})
                                else:
                                    await asyncio.sleep(1+attempt)
                        await asyncio.sleep(.1)
                    rows=[found[t] for t in sorted(found)]
                    path=directory/f"{symbol}_{step}s.json"
                    path.write_text(json.dumps(rows,separators=(",",":")))
                    gaps=sum(b["t"]-a["t"] != step for a,b in zip(rows,rows[1:]))
                    market[str(step)]={"file":path.name,"rows":len(rows),"first":rows[0]["t"] if rows else None,
                                       "last":rows[-1]["t"] if rows else None,"gaps":gaps,"failed_chunks":failures,
                                       "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
                    if step == 60:
                        aggregated=resample15(rows)
                        p=directory/f"{symbol}_900s.json"
                        p.write_text(json.dumps(aggregated,separators=(",",":")))
                        market["900"]={"file":p.name,"rows":len(aggregated),"first":aggregated[0]["t"] if aggregated else None,
                                       "last":aggregated[-1]["t"] if aggregated else None,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
                    print(json.dumps({"symbol":symbol,"interval_seconds":step,**market[str(step)]}),flush=True)
                manifest["symbols"][symbol]=market
                (directory/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
            manifest["public_get_requests"]=session.feed.requests
        manifest["finished_at"]=utc()
        (directory/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
        run.state["status"]="historical_capture_complete"
        run.event("research_data_complete",manifest,save=True)
        run.status()
        print(json.dumps({"directory":str(directory),"status":"complete"}),flush=True)
    finally:
        run.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days",type=int,default=90)
    p.add_argument("--output",type=Path)
    p.add_argument("--symbols",nargs="+",default=list(SYMBOLS))
    args=p.parse_args()
    if not 1 <= args.days <= 120:
        p.error("Choose 1 to 120 historical days")
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    for key in list(os.environ):
        if any(s in key.upper() for s in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
