"""Isolated, read-only fee lookup. The paper runner never reads the secret file."""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp


def read_key(path):
    source=path.read_text().strip()
    candidates=re.findall(r"[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}",source)
    if not candidates:
        candidates=re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])",source)
    candidates=list(dict.fromkeys(candidates))
    if len(candidates)!=1:
        raise ValueError("Ambiguous credential format")
    return candidates[0]


def fee_rates(payload,symbols):
    result=payload.get("result",{})
    if not isinstance(result,dict) or payload.get("success") is False:
        raise ValueError("Unexpected fee schema")
    rates={}
    for symbol in symbols:
        row=result.get(symbol)
        if not isinstance(row,dict):
            continue
        filtered={}
        for name in ("makerFeeRate","takerFeeRate"):
            rate=Decimal(str(row[name]))
            if not rate.is_finite() or not 0<=rate<=Decimal("0.02"):
                raise ValueError("Invalid fee rate")
            filtered[name]=str(rate)
        rates[symbol]=filtered
    return rates


async def probe(key_file,symbols):
    report={"at":datetime.now(timezone.utc).isoformat(),"status":"not_verified","rates":{},
            "method":"GET","path":"/v1/account/fee","real_orders":0,
            "fee_asset":"NOT verified; paper ledger assumes fees charged in quote currency"}
    try:
        key=read_key(key_file)
        # Dedicated session: no other host/path or redirect can receive this key.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                    cookie_jar=aiohttp.DummyCookieJar(),trust_env=True) as http:
            async with http.get("https://api.wallex.ir/v1/account/fee",headers={"X-API-Key":key},allow_redirects=False) as response:
                report["http_status"]=response.status
                if response.status==200:
                    report["rates"]=fee_rates(await response.json(),symbols)
                    report["status"]="verified" if set(report["rates"])==set(symbols) else "partial"
    except Exception as exc:
        # No response body, exception message, headers, key or account volume logged.
        report["error_type"]=type(exc).__name__
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--key-file",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--symbols",nargs="+",default=["BTCUSDT","ETHUSDT"])
    args=p.parse_args()
    report=asyncio.run(probe(args.key_file,args.symbols))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))


if __name__=="__main__":
    main()
