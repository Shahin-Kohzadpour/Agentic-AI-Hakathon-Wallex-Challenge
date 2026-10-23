"""Prospective paper trading using public Wallex prices, a learned ranker and Condor risk checks.

The ONLY remote operations implemented here are public HTTP GET requests. There
is no credential loader, order API, private account API, or live broker.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import aiohttp
import numpy as np

from condor.agents.risk import RiskEngine, RiskLimits, RiskState, auto_approve_with_risk_check

from .sandbox import OPTIONS

ORIGIN = "https://api.wallex.ir"
CONTROLLER = "wallex_live_public_paper"
CONNECTOR = "wallex_public_paper"
PUBLIC_PATHS = frozenset({"/v1/depth", "/v1/markets", "/hector/web/v1/markets", "/v1/udf/history"})
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
FEATURES = ("return_1", "return_3", "return_8", "return_16", "trend_16", "volatility_16", "volume_ratio", "candle_range")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def d(value) -> Decimal:
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Non-finite amount")
    return value


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, default=str, allow_nan=False)


@dataclass(frozen=True)
class Config:
    initial_cash: str = "1000"
    target_trades: int = 10
    holding_seconds: int = 900
    poll_seconds: int = 5
    per_trade_fraction: str = "0.10"
    fee_rate: str = "0.002"
    extra_slippage_bps: str = "1"
    max_spread_bps: str = "15"
    max_book_age_seconds: int = 15
    max_candle_age_seconds: int = 180
    stop_fraction: str = "0.01"
    target_fraction: str = "0.008"
    max_runtime_seconds: int = 2400
    max_visible_depth_fraction: str = "0.05"
    require_positive_edge: bool = True
    minimum_net_edge: str = "0.001"
    cooldown_seconds: int = 900
    risk_fraction: str = "0.001"
    selection_policy: str = "cost-aware validated entries; abstain without evidence; trade count is a ceiling"

    def __post_init__(self):
        if d(self.initial_cash) <= 0 or self.target_trades < 1:
            raise ValueError("Invalid budget or trade count")
        if self.holding_seconds < 10 or self.poll_seconds < 2 or self.max_runtime_seconds < 60:
            raise ValueError("Invalid live-session timing")
        if not 0 <= d(self.fee_rate) < 1 or not 0 < d(self.per_trade_fraction) <= 1:
            raise ValueError("Invalid fraction")
        if self.require_positive_edge and (self.holding_seconds < 60 or self.holding_seconds % 60):
            raise ValueError("Validated horizon must be a positive whole number of minutes")
        if (d(self.minimum_net_edge) <= 0 or not 0 < d(self.stop_fraction) < 1
            or not 0 < d(self.risk_fraction) <= 1 or self.cooldown_seconds < 0):
            raise ValueError("Invalid edge or risk limits")


class Run:
    def __init__(self, directory: Path, config: Config):
        self.directory, self.config = directory, config
        directory.mkdir(parents=True, exist_ok=True)
        import fcntl
        self.lock = (directory/"writer.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(directory/"trace.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at TEXT,kind TEXT,payload TEXT);"
                             "CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT);")
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        if row:
            self.state = json.loads(row[0])
            if self.state["config"] != asdict(config):
                raise ValueError("Resume configuration differs from the recorded session")
        else:
            self.state = {"mode":"LIVE_PUBLIC_DATA_PAPER_ONLY", "config":asdict(config), "created_at":utc(),
                          "started_epoch":time.time(), "status":"initializing", "cash_usdt":config.initial_cash,
                          "position":None, "trades":[], "fees_usdt":"0", "realized_pnl_usdt":"0",
                          "peak_equity_usdt":config.initial_cash, "max_drawdown_pct":"0", "data_errors":0,
                          "broker":"local simulated taker fills", "model":"local ridge ranker on real past candles",
                          "credentials_used":False,"real_orders_submitted":0,"quote_currency":"USDT",
                          "dollar_conversion":"1 USDT treated as 1 USD; no depeg adjustment",
                          "fee_source":"Assumed 0.2% each side; account tier and fee asset NOT verified",
                          "condor_source_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()}
            self.event("session_created",self.state,save=True)

    def event(self, kind: str, payload: dict, *, save=False):
        with self.db:
            self.db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)", (utc(),kind,encoded(payload)))
            if save:
                self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (encoded(self.state),))

    def status(self, **extra):
        record = {"at":utc(), "status":self.state["status"], "completed_round_trips":len(self.state["trades"]),
                  "target":self.config.target_trades, "cash_usdt":self.state["cash_usdt"],
                  "realized_pnl_usdt":self.state["realized_pnl_usdt"], "fees_usdt":self.state["fees_usdt"],
                  "position":self.state["position"], "data_errors":self.state["data_errors"], **extra}
        temporary = self.directory/"status.tmp"
        temporary.write_text(json.dumps(record,indent=2,default=str)+"\n")
        temporary.replace(self.directory/"status.json")
        return record

    def close(self):
        self.db.close()
        self.lock.close()


class PublicFeed:
    def __init__(self, session: aiohttp.ClientSession, run: Run):
        self.session, self.run = session, run
        self.requests = 0

    async def get(self, path: str, params: dict | None = None) -> dict:
        if path not in PUBLIC_PATHS:
            raise ValueError("Only reviewed public market-data paths are allowed")
        started = time.time()
        self.requests += 1
        # Explicit GET; no authorization headers, cookie jar, private methods or redirects.
        async with self.session.get(ORIGIN+path, params=params, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"Public GET returned HTTP {response.status}")
            body = await response.read()
            value = json.loads(body,parse_float=str)
            if value.get("success") is False:
                raise ValueError("Public market-data request reported failure")
            self.run.event("public_get", {"method":"GET","path":path,"params":params,
                          "started_epoch":started,"received_epoch":time.time(),
                          "http_date":response.headers.get("Date"),"cache_age":response.headers.get("Age"),
                          "body_sha256":hashlib.sha256(body).hexdigest(),"response":value})
            age = response.headers.get("Age")
            if age is not None and float(age) > 15 and path == "/v1/depth":
                raise ValueError("Cached order book is too old")
            return value

    async def book(self, symbol: str) -> dict:
        result = (await self.get("/v1/depth",{"symbol":symbol}))["result"]
        bids = sorted(((d(x["price"]),d(x["quantity"])) for x in result["bid"]),reverse=True)
        asks = sorted((d(x["price"]),d(x["quantity"])) for x in result["ask"])
        if not bids or not asks or any(p <= 0 or q <= 0 for p,q in bids+asks) or bids[0][0] >= asks[0][0]:
            raise ValueError("Empty, invalid, crossed or locked public book")
        book = {"symbol":symbol,"received_epoch":time.time(),"bids":bids,"asks":asks}
        self.run.event("book",book)
        return book

    async def candles(self, symbol: str, count: int = 400) -> list[dict]:
        end = int(time.time())//60*60
        data = await self.get("/v1/udf/history",{"symbol":symbol,"resolution":1,"from":end-count*60,"to":end})
        if data.get("s") != "ok":
            raise ValueError("No candle history available")
        keys=("t","o","h","l","c","v")
        if len({len(data.get(k,[])) for k in keys}) != 1:
            raise ValueError("Misaligned candle arrays")
        rows=[]
        for t,o,h,l,c,v in zip(*(data[k] for k in keys)):
            t=int(t)
            if t+60 > end:
                continue
            row={"t":t,"o":float(o),"h":float(h),"l":float(l),"c":float(c),"v":float(v)}
            if any(not math.isfinite(row[k]) for k in ("o","h","l","c","v")) or min(row[k] for k in ("o","h","l","c")) <= 0 or row["v"] < 0:
                raise ValueError("Invalid historical candle")
            rows.append(row)
        rows=sorted({r["t"]:r for r in rows}.values(),key=lambda r:r["t"])
        if not rows or time.time()-(rows[-1]["t"]+60) > self.run.config.max_candle_age_seconds:
            raise ValueError("Latest completed candle is stale")
        return rows


def feature(rows: list[dict], i: int) -> list[float] | None:
    if i < 32 or any(rows[j]["t"]-rows[j-1]["t"] != 60 for j in range(i-31,i+1)):
        return None
    close=rows[i]["c"]
    past=np.array([r["c"] for r in rows[i-16:i+1]],dtype=float)
    volume=np.mean([r["v"] for r in rows[i-16:i]])
    return [close/rows[i-k]["c"]-1 for k in (1,3,8,16)]+[
        close/float(np.mean(past))-1,float(np.std(np.diff(np.log(past)))),
        min(rows[i]["v"]/float(volume)-1,10) if volume else 0,
        (rows[i]["h"]-rows[i]["l"])/close]


def forecast(rows: list[dict]) -> dict:
    x,y=[],[]
    label_times=[]
    for i in range(32,len(rows)-1):
        values=feature(rows,i)
        if values is None or rows[i+1]["t"]-rows[i]["t"] != 60:
            continue
        x.append(values)
        y.append(rows[i+1]["c"]/rows[i]["c"]-1)
        label_times.append(rows[i+1]["t"]+60)
    current=feature(rows,len(rows)-1)
    if len(x)<100 or current is None:
        raise ValueError("Insufficient complete historical observations for learned ranking")
    matrix=np.array(x,dtype=float)
    target=np.array(y,dtype=float)
    mean=matrix.mean(axis=0)
    scale=np.maximum(matrix.std(axis=0),1e-8)
    z=np.column_stack([np.ones(len(matrix)),(matrix-mean)/scale])
    penalty=np.eye(z.shape[1])*0.1
    penalty[0,0]=0
    coefficients=np.linalg.solve(z.T@z/len(z)+penalty,z.T@target/len(z))
    standardized=np.array([1.0]+list((np.array(current)-mean)/scale))
    predicted=float(standardized@coefficients)
    if not math.isfinite(predicted):
        raise ValueError("Non-finite learned forecast")
    return {"forecast_gross_return":predicted,"training_samples":len(x),"training_labels_available_by":max(label_times),
            "last_feature_closed_at":rows[-1]["t"]+60,"features":dict(zip(FEATURES,current)),
            "means":mean.tolist(),"scales":scale.tolist(),"coefficients":coefficients.tolist(),
            "training_data_hash":hashlib.sha256(encoded(rows).encode()).hexdigest(),
            "training_residual_std":float(np.sqrt(np.mean((target-z@coefficients)**2))),
            "model":"ridge regression; eight closed-candle features; fixed alpha=0.1"}


def mid(book: dict) -> Decimal:
    return (book["bids"][0][0]+book["asks"][0][0])/2


def spread_bps(book: dict) -> Decimal:
    return (book["asks"][0][0]-book["bids"][0][0])/mid(book)*10000


def vwap(book: dict, side: str, quantity: Decimal) -> Decimal:
    if side not in {"BUY","SELL"} or quantity <= 0:
        raise ValueError("Invalid simulated market order")
    remaining,cost=quantity,Decimal(0)
    for price,available in book["asks"] if side == "BUY" else book["bids"]:
        taken=min(remaining,available)
        cost+=taken*price
        remaining-=taken
        if remaining == 0:
            return cost/quantity
    raise ValueError("Visible book cannot support this simulated market fill")


class PaperPriceClient:
    def __init__(self, book: dict):
        self.book=book
        self.market_data=self

    async def get_prices(self,connector_name,trading_pairs):
        name=trading_pairs if isinstance(trading_pairs,str) else trading_pairs[0]
        return {"prices":{name:float(self.book["asks"][0][0])}}


class Session:
    def __init__(self,run:Run,feed:PublicFeed,symbols=SYMBOLS):
        self.run,self.feed,self.config=run,feed,run.config
        self.fee=d(self.config.fee_rate)
        self.slip=d(self.config.extra_slippage_bps)/10000
        self.risk=RiskEngine(RiskLimits(max_position_size_quote=float(d(self.config.initial_cash)*d(self.config.per_trade_fraction)),
                                       max_open_executors=1,max_drawdown_pct=2,shutdown_drawdown_pct=4,max_leverage=1))
        self.meta={}
        self.stopping=False
        self.candle_cache={}
        self.symbols=tuple(symbols)

    async def setup(self):
        current,legacy=await asyncio.gather(self.feed.get("/hector/web/v1/markets"),self.feed.get("/v1/markets"))
        public={r["symbol"]:r for r in current["result"]["markets"]}
        legacy=legacy["result"]["symbols"]
        excluded=set(json.loads(Path(__file__).with_name("excluded-markets.json").read_text())["excluded_pairs"])
        for symbol in self.symbols:
            if symbol not in public or public[symbol].get("is_spot") is not True or symbol[:-4]+"/USDT" in excluded:
                continue
            precision=public[symbol]["amount_precision"]
            if type(precision) is not int or not 0 <= precision <= 18:
                continue
            self.meta[symbol]={"quantity_precision":precision,"min_notional":str(legacy[symbol]["minNotional"]),
                               "min_quantity":str(legacy[symbol]["minQty"])}
        if not self.meta:
            raise ValueError("No eligible public market metadata")
        self.run.state["metadata"]=self.meta
        self.run.state["model"]=("purged calibrated ridge with out-of-sample cost gate" if self.config.require_positive_edge
                                  else "exploratory one-minute ridge; no edge gate")
        self.run.state["status"]="running"
        self.run.event("metadata_ready",self.meta,save=True)

    def observe(self,book:dict|None=None):
        state=self.run.state
        equity=d(state["cash_usdt"])
        p=state["position"]
        if p:
            if book is None or p["symbol"] != book["symbol"]:
                raise ValueError("Fresh book required to mark an open position")
            equity+=d(p["quantity"])*book["bids"][0][0]
        peak=max(d(state["peak_equity_usdt"]),equity)
        drawdown=(peak-equity)/peak*100
        state["peak_equity_usdt"]=str(peak)
        state["max_drawdown_pct"]=str(max(d(state["max_drawdown_pct"]),drawdown))
        self.run.event("equity",{"equity_usdt":str(equity),"drawdown_pct":str(drawdown),
                                  "mid":str(mid(book)) if book else None,"symbol":book["symbol"] if book else None},save=True)
        self.run.status(equity_usdt=str(equity))
        return equity,drawdown

    def fresh(self,book):
        if not 0 <= time.time()-book["received_epoch"] <= self.config.max_book_age_seconds:
            raise ValueError("Order book is stale at the simulated execution time")

    def liquidation_return(self,book):
        p=self.run.state["position"]
        if not p or p["symbol"] != book["symbol"]:
            raise ValueError("Matching open position required")
        self.fresh(book)
        proceeds=vwap(book,"SELL",d(p["quantity"]))*(1-self.slip)*(1-self.fee)
        return proceeds/(d(p["entry_price"])*(1+self.fee))-1

    async def select(self) -> dict | None:
        from .strategy import validated_forecast, entry_rejections
        async def one(symbol):
            async def history():
                minute=int(time.time())//60
                cached=self.candle_cache.get(symbol)
                if cached and cached[0] == minute:
                    return cached[1]
                rows=await self.feed.candles(symbol,1440 if self.config.require_positive_edge else 400)
                self.candle_cache[symbol]=(minute,rows)
                return rows
            rows,book=await asyncio.gather(history(),self.feed.book(symbol))
            spread=spread_bps(book)
            # Reserve an exit spread as wide as the configured entry limit.
            cost=2*self.fee+(spread+d(self.config.max_spread_bps))/20000+2*self.slip
            model=(validated_forecast(rows,self.config.holding_seconds//60,float(cost),float(d(self.config.minimum_net_edge)))
                   if self.config.require_positive_edge else forecast(rows))
            if model["training_labels_available_by"] > time.time():
                raise ValueError("Training labels are from the future")
            if spread > d(self.config.max_spread_bps):
                raise ValueError("Spread exceeds the live paper limit")
            score=d(model.get("forecast_lower_return",model["forecast_gross_return"]))-cost
            reasons=entry_rejections(model,float(cost),float(d(self.config.minimum_net_edge))) if self.config.require_positive_edge else []
            return {"symbol":symbol,"model":model,"selection_score_net":str(score),"spread_bps":str(spread),
                    "estimated_cost_return":str(cost),"entry_rejections":reasons}
        results=await asyncio.gather(*(one(s) for s in self.meta),return_exceptions=True)
        choices=[]
        for symbol,result in zip(self.meta,results):
            if isinstance(result,BaseException):
                self.error("candidate",result,symbol)
            else:
                choices.append(result)
        if not choices:
            raise ValueError("No valid fresh candidate this cycle")
        choices.sort(key=lambda r:(-d(r["selection_score_net"]),r["symbol"]))
        eligible=[c for c in choices if not c["entry_rejections"]]
        selected=eligible[0] if eligible else None
        if selected:
            selected["selection_reason"]="Highest eligible conservative forecast after costs"
        self.run.event("model_decision",{"candidates":choices,"selected":selected["symbol"] if selected else None,
                         "positive_net_edge_required":self.config.require_positive_edge,
                         "reason":"eligible opportunity" if selected else "abstain: no validated positive net edge"})
        if selected is None:
            self.run.state["abstentions"]=self.run.state.get("abstentions",0)+1
            self.run.status(last_decision="abstain_no_validated_edge")
        return selected

    def error(self,phase,error,symbol=None):
        self.run.state["data_errors"]+=1
        self.run.event("error",{"phase":phase,"symbol":symbol,"error":str(error)},save=True)
        self.run.status()
        print(encoded({"event":"data_error","phase":phase,"symbol":symbol,"error":str(error)}),flush=True)

    async def enter(self,decision:dict):
        symbol=decision["symbol"]
        book=await self.feed.book(symbol)  # Re-read after inference; do not fill against the selection snapshot.
        self.fresh(book)
        if spread_bps(book) > d(self.config.max_spread_bps):
            raise ValueError("Spread widened before simulated entry")
        equity,drawdown=self.observe()
        if drawdown > 2:
            raise ValueError("Paper account reached drawdown limit")
        budget=min(equity*d(self.config.per_trade_fraction),d(self.run.state["cash_usdt"]),
                   d(self.config.initial_cash)*d(self.config.per_trade_fraction))
        if self.config.require_positive_edge:
            from .strategy import entry_rejections
            if self.run.state["position"] is not None:
                raise ValueError("A paper position is already open")
            if time.time()-self.run.state.get("last_exit_epoch",0) < self.config.cooldown_seconds:
                raise ValueError("Paper entry is still in cooldown")
            if self.run.state["started_epoch"]+self.config.max_runtime_seconds-time.time() < self.config.holding_seconds+30:
                raise ValueError("Not enough observation time for the forecast horizon")
            if decision["model"].get("horizon_minutes") != self.config.holding_seconds//60:
                raise ValueError("Forecast horizon does not match holding period")
            if time.time()-decision["model"]["last_feature_closed_at"] > self.config.max_candle_age_seconds:
                raise ValueError("Signal became stale before entry")
            budget=min(budget,equity*d(self.config.risk_fraction)/d(self.config.stop_fraction))
        precision=self.meta[symbol]["quantity_precision"]
        quantum=Decimal(1).scaleb(-precision)
        quantity=(budget/((1+self.fee)*book["asks"][0][0]*(1+self.slip))).quantize(quantum,rounding=ROUND_DOWN)
        for _ in range(4):
            price=vwap(book,"BUY",quantity)*(1+self.slip)
            smaller=(budget/((1+self.fee)*price)).quantize(quantum,rounding=ROUND_DOWN)
            quantity=min(quantity,smaller)
        price=vwap(book,"BUY",quantity)*(1+self.slip)
        notional=quantity*price
        if self.config.require_positive_edge:
            # Recheck the actual proposed size on fresh depth, including both fees,
            # both slippages and a reserve for exit spread deterioration.
            liquidation=vwap(book,"SELL",quantity)*(1-self.slip)*(1-self.fee)
            entry_cost=price*(1+self.fee)
            current_cost=1-liquidation/entry_cost
            reserved_cost=2*self.fee+(spread_bps(book)+d(self.config.max_spread_bps))/20000+2*self.slip
            cost=max(current_cost,reserved_cost)
            reasons=entry_rejections(decision["model"],float(cost),float(d(self.config.minimum_net_edge)))
            if reasons:
                self.run.event("entry_rejected",{"symbol":symbol,"reasons":reasons,"fresh_cost":str(cost)})
                raise ValueError("Fresh executable costs fail the positive-edge gate")
        visible=sum((p*q for p,q in book["asks"]),Decimal(0))
        if (quantity < d(self.meta[symbol]["min_quantity"]) or notional < d(self.meta[symbol]["min_notional"])
            or notional > visible*d(self.config.max_visible_depth_fraction)):
            raise ValueError("Entry violates quantity/notional/visible-depth limits")
        risk_state=RiskState(total_exposure=0,executor_count=0,drawdown_pct=float(drawdown))
        call={"tool":"create_order_executor","input":{"controller_id":CONTROLLER,"connector_name":CONNECTOR,
              "trading_pair":symbol[:-4]+"-USDT","amount":str(quantity),"leverage":1}}
        callback=auto_approve_with_risk_check(self.risk,risk_state,agent_id=CONTROLLER,price_client=PaperPriceClient(book))
        permission=await callback(call,OPTIONS)
        if permission["outcome"]["outcome"] != "selected":
            raise ValueError("Condor refused paper entry: "+permission.get("reason","unknown reason"))
        self.fresh(book)
        fee=notional*self.fee
        spent=notional+fee
        if spent > budget or spent > d(self.run.state["cash_usdt"]):
            raise ValueError("Paper entry exceeds all-in spend budget")
        index=len(self.run.state["trades"])+1
        position={"trade":index,"symbol":symbol,"quantity":str(quantity),"entry_price":str(price),
                  "entry_mid":str(mid(book)),"entry_notional":str(notional),"entry_fee":str(fee),
                  "entry_spread_bps":str(spread_bps(book)),"opened_at":utc(),"opened_epoch":time.time(),
                  "book_received_epoch":book["received_epoch"],"decision":decision,"condor_permission":permission,
                  "executor_id":f"paper_trade_{index}"}
        self.run.state["cash_usdt"]=str(d(self.run.state["cash_usdt"])-spent)
        self.run.state["fees_usdt"]=str(d(self.run.state["fees_usdt"])+fee)
        self.run.state["position"]=position
        self.run.event("paper_buy",position,save=True)
        self.observe(book)
        print(encoded({"event":"paper_buy","trade":index,"symbol":symbol,"price":str(price),"notional_usdt":str(notional),
                       "opened_at":position["opened_at"],"forecast":decision["model"]["forecast_gross_return"]}),flush=True)

    async def exit(self,book:dict,reason:str):
        p=self.run.state["position"]
        quantity=d(p["quantity"])
        self.fresh(book)
        price=vwap(book,"SELL",quantity)*(1-self.slip)
        callback=auto_approve_with_risk_check(self.risk,RiskState(),agent_id=CONTROLLER,
                                            executor_owners={p["executor_id"]:CONTROLLER})
        permission=await callback({"tool":"stop_executor","input":{"executor_id":p["executor_id"],"keep_position":False}},OPTIONS)
        if permission["outcome"]["outcome"] != "selected":
            raise ValueError("Condor refused the owned paper exit")
        self.fresh(book)
        notional=quantity*price
        fee=notional*self.fee
        gross=notional-d(p["entry_notional"])
        net=gross-d(p["entry_fee"])-fee
        trade={k:p[k] for k in ("trade","symbol","quantity","opened_at","entry_price","entry_mid","entry_spread_bps","entry_fee")}
        trade.update({"closed_at":utc(),"held_seconds":round(time.time()-p["opened_epoch"],3),"exit_price":str(price),
                      "exit_mid":str(mid(book)),"exit_spread_bps":str(spread_bps(book)),"exit_fee":str(fee),
                      "gross_pnl_usdt":str(gross),"net_pnl_usdt":str(net),"exit_reason":reason,
                      "signal_forecast":p["decision"]["model"]["forecast_gross_return"],
                      "selected_estimated_net_return":p["decision"]["selection_score_net"],"simulated_fill":True})
        self.run.state["cash_usdt"]=str(d(self.run.state["cash_usdt"])+notional-fee)
        self.run.state["fees_usdt"]=str(d(self.run.state["fees_usdt"])+fee)
        self.run.state["realized_pnl_usdt"]=str(d(self.run.state["realized_pnl_usdt"])+net)
        self.run.state["trades"].append(trade)
        self.run.state["last_exit_epoch"]=time.time()
        self.run.state["position"]=None
        self.run.event("paper_sell",trade,save=True)
        self.observe()
        print(encoded({"event":"round_trip_complete",**trade,"cash_usdt":self.run.state["cash_usdt"]}),flush=True)

    async def execute(self):
        await self.setup()
        print(encoded({"event":"started","run_dir":str(self.run.directory),"budget_usdt":self.config.initial_cash,
                       "target_round_trips":self.config.target_trades,"holding_seconds":self.config.holding_seconds}),flush=True)
        while len(self.run.state["trades"]) < self.config.target_trades:
            elapsed=time.time()-self.run.state["started_epoch"]
            if elapsed >= self.config.max_runtime_seconds:
                self.stopping=True
            try:
                p=self.run.state["position"]
                if p:
                    book=await self.feed.book(p["symbol"])
                    _,drawdown=self.observe(book)
                    reference=d(p["entry_price"])
                    age=time.time()-p["opened_epoch"]
                    bid=book["bids"][0][0]
                    net_return=self.liquidation_return(book)
                    reason=None
                    if self.stopping:
                        reason="session_wind_down"
                    elif drawdown > 2:
                        reason="account_drawdown"
                    elif (net_return <= -d(self.config.stop_fraction) if self.config.require_positive_edge
                          else bid <= reference*(1-d(self.config.stop_fraction))):
                        reason="stop_loss"
                    elif (net_return >= d(self.config.target_fraction) if self.config.require_positive_edge
                          else bid >= reference*(1+d(self.config.target_fraction))):
                        reason="take_profit"
                    elif age >= self.config.holding_seconds:
                        reason="timed_exit"
                    if reason:
                        await self.exit(book,reason)
                elif self.stopping:
                    break
                else:
                    remaining=self.config.max_runtime_seconds-elapsed
                    cooling=time.time()-self.run.state.get("last_exit_epoch",0) < self.config.cooldown_seconds
                    if self.config.require_positive_edge and remaining < self.config.holding_seconds+30:
                        self.stopping=True
                    elif not cooling:
                        decision=await self.select()
                        if decision is not None:
                            await self.enter(decision)
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError,RuntimeError) as exc:
                self.error("live_cycle",exc)
            if self.run.state["position"] is None and self.stopping:
                break
            if len(self.run.state["trades"]) < self.config.target_trades:
                await asyncio.sleep(self.config.poll_seconds)
        self.run.state["finished_at"]=utc()
        self.run.state["status"]=("complete" if self.run.state["position"] is None and
                                  (self.config.require_positive_edge or len(self.run.state["trades"]) == self.config.target_trades)
                                  else "incomplete")
        self.run.state["public_get_requests"]=self.feed.requests
        self.run.event("session_finished",self.run.state,save=True)
        self.run.status()
        return export(self.run)


def export(run:Run) -> dict:
    state=run.state
    trades=state["trades"]
    initial=d(run.config.initial_cash)
    final=d(state["cash_usdt"])
    fees=d(state["fees_usdt"])
    gross=sum((d(t["gross_pnl_usdt"]) for t in trades),Decimal(0))
    net=sum((d(t["net_pnl_usdt"]) for t in trades),Decimal(0))
    if state["position"] is None and abs(final-initial-net) > Decimal("0.00000001"):
        raise ValueError("Paper ledger does not reconcile")
    result={"status":state["status"],"mode":state["mode"],"started_at":state["created_at"],"finished_at":state.get("finished_at"),
            "initial_balance_usdt":str(initial),"final_cash_usdt":str(final),"gross_pnl_usdt":str(gross),
            "fees_usdt":str(fees),"net_pnl_usdt":str(net),"account_return_pct":str((final/initial-1)*100),
            "completed_round_trips":len(trades),"simulated_fills":2*len(trades),"target_round_trips":run.config.target_trades,
            "winning_round_trips":sum(d(t["net_pnl_usdt"])>0 for t in trades),
            "max_drawdown_pct":state["max_drawdown_pct"],"open_position":state["position"],
            "dollar_conversion":state["dollar_conversion"],"fee_assumption":state["fee_source"],
            "extra_slippage_bps_each_side":run.config.extra_slippage_bps,"data_errors":state["data_errors"],
            "public_get_requests":state.get("public_get_requests"),"credentials_used":False,"real_orders_submitted":0,
            "decision_policy":run.config.selection_policy,"positive_net_edge_required":run.config.require_positive_edge,
            "abstentions":state.get("abstentions",0),
            "model":state["model"],"condor_commit":state["condor_source_commit"],"trades":trades,
            "limits":["Live prospective data; orders and fills simulated, not exchange-confirmed.",
                      "No profitable edge is established by a short paper session or historical screening.",
                      "Fees are assumed and book snapshots cannot model queue changes or guarantee fillability.",
                      "USDT is treated as USD; no depeg or financing adjustment."]}
    (run.directory/"results.json").write_text(json.dumps(result,indent=2)+"\n")
    if trades:
        with (run.directory/"trades.csv").open("w",newline="") as handle:
            writer=csv.DictWriter(handle,fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    lines=["# Live-data paper session", "", f"Status: {result['status']}. Completed round trips: {len(trades)}.","",
           f"Started: {result['started_at']}. Finished: {result['finished_at']}.","",
           "All fills are local simulations using real public Wallex books; no exchange account or real orders.","",
           f"Initial: {initial:.2f} USDT. Final cash: {final:.4f} USDT. Net P&L: {net:+.4f} USDT.",
           f"Gross P&L before assumed fees: {gross:+.4f} USDT. Fees: {fees:.4f} USDT.",
           f"Maximum observed account drawdown: {d(state['max_drawdown_pct']):.4f}%.","",
           f"USDT is a dollar proxy. Assumed fee: {run.config.fee_rate} each side; slippage: {run.config.extra_slippage_bps} bps per side.",
           f"Decision policy: {run.config.selection_policy}. Positive net edge required: {run.config.require_positive_edge}.",
           f"Holding horizon: {run.config.holding_seconds}s. Abstentions: {state.get('abstentions',0)}.",
           "Paper fills and historical validation do not establish future profitability.","",
           "| # | Market | Open (UTC) | Close (UTC) | Entry | Exit | Gross USDT | Fees USDT | Net USDT | Exit |",
           "|---:|---|---|---|---:|---:|---:|---:|---:|---|"]
    for t in trades:
        lines.append(f"| {t['trade']} | {t['symbol']} | {t['opened_at'][11:19]} | {t['closed_at'][11:19]} | {d(t['entry_price']):.5f} | {d(t['exit_price']):.5f} | {d(t['gross_pnl_usdt']):+.4f} | {d(t['entry_fee'])+d(t['exit_fee']):.4f} | {d(t['net_pnl_usdt']):+.4f} | {t['exit_reason']} |")
    lines.extend(["","Artifacts: `trades.csv`, `results.json`, `status.json`, and `trace.sqlite3` (raw GET responses, model coefficients/inputs, quotes, decisions, fills, and equity observations).",""])
    (run.directory/"RESULTS.md").write_text("\n".join(lines))
    return result


async def start(args):
    config=Config(target_trades=args.trades,holding_seconds=args.hold_seconds,fee_rate=args.fee,
                  max_runtime_seconds=args.max_minutes*60,require_positive_edge=not args.exploratory,
                  cooldown_seconds=0 if args.exploratory else 900,
                  selection_policy=("fixed-count exploratory paper probes; negative edge permitted" if args.exploratory
                                    else Config.selection_policy))
    directory=args.resume if args.resume else Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("livepaper_%Y%m%dT%H%M%SZ")
    run=Run(directory,config)
    if run.state["status"] == "complete":
        print(encoded(export(run)),flush=True)
        run.close()
        return
    timeout=aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout,connector=aiohttp.TCPConnector(limit=4),cookie_jar=aiohttp.DummyCookieJar(),
                                        headers={"User-Agent":"Condor-Wallex-LivePaper/0.1","Accept":"application/json","Cache-Control":"no-cache"},trust_env=True) as http:
            session=Session(run,PublicFeed(http,run))
            loop=asyncio.get_running_loop()
            for signum in (signal.SIGINT,signal.SIGTERM):
                loop.add_signal_handler(signum,lambda:setattr(session,"stopping",True))
            result=await session.execute()
            print(encoded({"event":"FINISHED",**{k:v for k,v in result.items() if k != "trades"},"run_dir":str(directory)}),flush=True)
    except BaseException as exc:
        run.state["status"]="failed"
        run.event("fatal_error",{"type":type(exc).__name__,"error":str(exc)},save=True)
        run.status(error=str(exc))
        raise
    finally:
        run.close()


def main():
    parser=argparse.ArgumentParser(description="GET-only real-market data with simulated USDT trades; never submits exchange orders")
    parser.add_argument("--trades",type=int,default=10)
    parser.add_argument("--hold-seconds",type=int,default=900)
    parser.add_argument("--fee",default="0.002")
    parser.add_argument("--max-minutes",type=int,default=40)
    parser.add_argument("--resume",type=Path)
    parser.add_argument("--exploratory",action="store_true",help="Explicitly bypass validation for execution experiments only")
    args=parser.parse_args()
    # Disable implicit dotenv loading and remove inherited model/exchange credentials in this child process.
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    for key in list(os.environ):
        if any(s in key.upper() for s in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(start(args))


if __name__ == "__main__":
    main()
