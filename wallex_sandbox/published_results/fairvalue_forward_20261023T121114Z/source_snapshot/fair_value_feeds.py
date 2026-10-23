"""Allowlisted public GET adapters. No secret loader or order/account endpoints."""
import hashlib
import json
import math
import time

from .live_paper import d

PUBLIC={
    "wallex":("https://api.wallex.ir",frozenset({"/v1/depth","/v1/markets","/hector/web/v1/markets"})),
    "okx":("https://www.okx.com",frozenset({"/api/v5/market/books"})),
    "bybit":("https://api.bybit.com",frozenset({"/v5/market/orderbook"})),
}


class FairFeeds:
    def __init__(self,http,journal):
        self.http,self.journal=http,journal
        self.requests=0

    async def get(self,venue,path,params=None):
        if venue not in PUBLIC or path not in PUBLIC[venue][1]:
            raise ValueError("Only reviewed public GET paths allowed")
        started=time.time()
        self.requests+=1
        async with self.http.get(PUBLIC[venue][0]+path,params=params,allow_redirects=False) as response:
            received=time.time()
            if response.status!=200:
                self.journal.event("public_http_error",{"venue":venue,"path":path,"http_status":response.status})
                raise ValueError("public_http_status_"+str(response.status))
            raw=await response.read()
            payload=json.loads(raw,parse_float=str)
            age=float(response.headers.get("Age","0"))
            if not math.isfinite(age) or age<0:
                raise ValueError("Invalid public cache age")
            self.journal.event("fair_public_get",{"venue":venue,"method":"GET","path":path,"params":params,
                  "started_epoch":started,"received_epoch":received,"http_date":response.headers.get("Date"),
                  "cache_age":age,"body_sha256":hashlib.sha256(raw).hexdigest(),"response":payload})
            return payload,received,age

    async def book(self,venue,symbol):
        if symbol not in {"BTCUSDT","ETHUSDT"}:
            raise ValueError("Unsupported same-quote spot instrument")
        exchange=None
        if venue=="wallex":
            value,received,age=await self.get(venue,"/v1/depth",{"symbol":symbol})
            if value.get("success") is False:
                raise ValueError("Wallex public data refused")
            row=value["result"]
            bids=[(d(x["price"]),d(x["quantity"])) for x in row["bid"]]
            asks=[(d(x["price"]),d(x["quantity"])) for x in row["ask"]]
        elif venue=="okx":
            value,received,age=await self.get(venue,"/api/v5/market/books",{"instId":symbol[:-4]+"-USDT","sz":20})
            if str(value.get("code"))!="0" or len(value.get("data",[]))!=1:
                raise ValueError("OKX spot book refused")
            row=value["data"][0]
            bids=[(d(x[0]),d(x[1])) for x in row["bids"]]
            asks=[(d(x[0]),d(x[1])) for x in row["asks"]]
            exchange=int(row["ts"])/1000
        elif venue=="bybit":
            value,received,age=await self.get(venue,"/v5/market/orderbook",{"category":"spot","symbol":symbol,"limit":25})
            row=value.get("result",{})
            if value.get("retCode")!=0 or row.get("s")!=symbol:
                raise ValueError("Bybit spot book refused")
            bids=[(d(x[0]),d(x[1])) for x in row["b"]]
            asks=[(d(x[0]),d(x[1])) for x in row["a"]]
            exchange=int(row["ts"])/1000
        else:
            raise ValueError("Unknown reference venue")
        return {"venue":venue,"symbol":symbol,"quote":"USDT","market_type":"spot","received_epoch":received,
                "exchange_epoch":exchange,"cache_age":age,"bids":sorted(bids,reverse=True),"asks":sorted(asks)}
