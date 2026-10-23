"""Bounded concurrent public hourly history capture; no credentials or orders."""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .collect_research_data import normalize
from .live_paper import Config, PublicFeed, Run, utc


async def collect(args):
    metadata=json.loads((args.metadata/"manifest.json").read_text())
    start=int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp())
    end=metadata["end"]
    folder=Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("long_history_%Y%m%dT%H%M%SZ")
    run=Run(folder,Config())
    manifest={"created_at":utc(),"start":start,"end":end,"interval_seconds":3600,"symbols":{},
              "metadata_source":str(args.metadata/"manifest.json"),
              "metadata_sha256":hashlib.sha256((args.metadata/"manifest.json").read_bytes()).hexdigest(),
              "spread_source":"Previously captured current public spreads, frozen for all experiments; not historical spreads",
              "credentials_used":False,"real_orders":0}
    limit=asyncio.Semaphore(3)
    done=0
    total=len(range(start,end,28*86400))*len(metadata["symbols"])
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45),
                    connector=aiohttp.TCPConnector(limit=3),cookie_jar=aiohttp.DummyCookieJar(),trust_env=True,
                    headers={"User-Agent":"Wallex-Public-PaperResearch/0.4"}) as http:
            feed=PublicFeed(http,run)
            async def chunk(symbol,left):
                nonlocal done
                right=min(end,left+28*86400)
                failure=None
                for attempt in range(3):
                    try:
                        async with limit:
                            payload=await feed.get("/v1/udf/history",{"symbol":symbol,"resolution":60,"from":left,"to":right})
                            rows=normalize(payload,left,right,3600)
                        done+=1
                        if done%24 == 0:
                            print(json.dumps({"chunks_done":done,"total":total}),flush=True)
                        return rows,None
                    except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,RuntimeError) as exc:
                        failure={"from":left,"to":right,"error":str(exc)}
                        await asyncio.sleep(attempt+1)
                done+=1
                return [],failure
            async def market(symbol,record):
                batches=await asyncio.gather(*(chunk(symbol,left) for left in range(start,end,28*86400)))
                found={}
                failures=[]
                for rows,failure in batches:
                    if failure:
                        failures.append(failure)
                    for row in rows:
                        if row["t"] in found and found[row["t"]]!=row:
                            raise ValueError("Conflicting closed historical candles")
                        found[row["t"]]=row
                rows=[found[t] for t in sorted(found)]
                path=folder/f"{symbol}_3600s.json"
                path.write_text(json.dumps(rows,separators=(",",":")))
                gaps=sum(b["t"]-a["t"]!=3600 for a,b in zip(rows,rows[1:]))
                r={"metadata":record["metadata"],"spread_snapshot_bps":record["spread_snapshot_bps"],
                   "spread_snapshot_epoch":record["spread_snapshot_epoch"],
                   "3600":{"file":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                           "rows":len(rows),"first":rows[0]["t"] if rows else None,"last":rows[-1]["t"] if rows else None,
                           "gaps":gaps,"failed_chunks":failures}}
                manifest["symbols"][symbol]=r
                (folder/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
                print(json.dumps({"symbol":symbol,**r["3600"]}),flush=True)
            await asyncio.gather(*(market(s,r) for s,r in metadata["symbols"].items()))
            manifest["public_get_requests"]=feed.requests
        manifest["finished_at"]=utc()
        (folder/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
        run.state["status"]="long_history_capture_complete"
        run.event("long_history_capture_complete",manifest,save=True)
        run.status()
        print(json.dumps({"directory":str(folder),"status":"complete"}),flush=True)
    finally:
        run.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--metadata",type=Path,required=True)
    p.add_argument("--start",default="2024-05-01")
    args=p.parse_args()
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    for key in list(os.environ):
        if any(x in key.upper() for x in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
