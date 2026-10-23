"""Prospective multi-venue fair-value paper session. No exchange order methods."""
import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import signal
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from condor.agents.risk import RiskEngine, RiskLimits, RiskState, auto_approve_with_risk_check

from .fair_value_core import BasisWindow, FairConfig, PaperWallet, opportunity, reference_price, validate_book
from .fair_value_feeds import FairFeeds
from .live_paper import Config, PaperPriceClient, Run, d, encoded, mid, utc
from .sandbox import OPTIONS

SYMBOLS=("BTCUSDT","ETHUSDT")
OWNER="wallex_fair_value_paper"


def fees_from_file(path,config,now):
    fees={s:d(config.assumed_fee) for s in SYMBOLS}
    source={s:"assumed" for s in SYMBOLS}
    report={"status":"not_verified","rates":{}}
    if path is not None:
        report=json.loads(path.read_text())
        at=datetime.fromisoformat(report["at"]).timestamp()
        if report.get("status") in {"verified","partial"} and 0<=now-at<=86400:
            for symbol in SYMBOLS:
                if symbol in report.get("rates",{}):
                    rate=d(report["rates"][symbol]["takerFeeRate"])
                    if not 0<=rate<=d("0.02"):
                        raise ValueError("Invalid recorded fee")
                    fees[symbol]=rate
                    source[symbol]="verified_rate_only"
    return fees,{"rates":{s:str(fees[s]) for s in SYMBOLS},"source":source,"lookup_status":report.get("status"),
                 "fee_asset_assumption":"quote charged on both sides; fee asset was not verified",
                 "credential_loaded_by_strategy":False}


def hashes():
    return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
            ("fair_value_core.py","fair_value_feeds.py","fair_value_paper.py","live_paper.py",
             "excluded-markets.json","requirements.lock.txt")}


class FairSession:
    def __init__(self,run,config,feeds,fees,fee_info,observe_only=False):
        self.run,self.config,self.feeds=run,config,feeds
        self.fees,self.fee_info=fees,fee_info
        self.observe_only=observe_only
        self.stop=asyncio.Event()
        self.pending={}
        self.meta={}
        self.last_equity=None
        self.last_equity_epoch=None
        self.wind_down_started=None
        state=run.state
        new=asdict(config)
        if "fair_value_config" in state:
            if state["fair_value_config"]!=new or state["fair_source_hashes"]!=hashes() or state["fair_fee_info"]!=fee_info:
                raise ValueError("Cannot resume a changed strategy, source or fee schedule")
            if state["fair_observe_only"]!=observe_only:
                raise ValueError("Cannot change observation mode on resume")
        else:
            state.update(fair_value_config=new,fair_source_hashes=hashes(),fair_fee_info=fee_info,
                         fair_observe_only=observe_only,fair_wallet=PaperWallet(config).state,
                         fair_started_epoch=time.time(),fair_snapshots=0,fair_rejections={},fair_errors=0,
                         fair_status="initializing",fair_baselines={})
        self.wallet=PaperWallet(config,state["fair_wallet"])
        state["model"]="Synchronized spot reference median plus past local log-basis median/MAD"
        state["broker"]="Local simulated taker fills; Condor risk callback; custom outer loop"
        self.windows={s:BasisWindow(config) for s in SYMBOLS}
        for symbol,rows in state["fair_baselines"].items():
            self.windows[symbol].rows.extend((float(t),float(b)) for t,b in rows)
            self.windows[symbol].trim(time.time())
        self.risk=RiskEngine(RiskLimits(max_position_size_quote=float(d(config.maximum_spend)),max_open_executors=1,
                         max_drawdown_pct=2,shutdown_drawdown_pct=4,max_leverage=1))
        self.run.event("fair_session_started_or_resumed",{"source_hashes":hashes(),"fees":fee_info,
                    "observe_only":observe_only,"persistence_reset_on_start":True},save=True)
        source=run.directory/"source_snapshot"
        source.mkdir(exist_ok=True)
        for name,expected in state["fair_source_hashes"].items():
            target=source/name
            if not target.exists():
                target.write_bytes(Path(__file__).with_name(name).read_bytes())
            if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
                raise ValueError("Recorded source snapshot mismatch")

    async def setup(self):
        current,legacy=await asyncio.gather(self.feeds.get("wallex","/hector/web/v1/markets"),self.feeds.get("wallex","/v1/markets"))
        current={r["symbol"]:r for r in current[0]["result"]["markets"]}
        legacy=legacy[0]["result"]["symbols"]
        excluded=set(json.loads(Path(__file__).with_name("excluded-markets.json").read_text())["excluded_pairs"])
        for symbol in SYMBOLS:
            if symbol not in current or current[symbol].get("is_spot") is not True or symbol[:-4]+"/USDT" in excluded:
                continue
            precision=current[symbol]["amount_precision"]
            if type(precision) is not int or not 0<=precision<=18:
                raise ValueError("Invalid local market precision")
            self.meta[symbol]={"quantity_precision":precision,"min_quantity":str(legacy[symbol]["minQty"]),
                               "min_notional":str(legacy[symbol]["minNotional"])}
        if not self.meta:
            raise ValueError("No eligible Wallex spot instrument")
        p=self.wallet.state["position"]
        if p and p["symbol"] not in self.meta:
            raise ValueError("Open paper market no longer eligible; do not fabricate settlement")
        self.run.state["fair_metadata"]=self.meta
        self.run.event("fair_metadata",self.meta,save=True)

    async def snapshot(self,symbol):
        answers=await asyncio.gather(*(self.feeds.book(venue,symbol) for venue in ("wallex","okx","bybit")),return_exceptions=True)
        local=answers[0] if isinstance(answers[0],dict) else None
        refs=[x for x in answers[1:] if isinstance(x,dict)]
        errors=[type(x).__name__ for x in answers if isinstance(x,BaseException)]
        snapshot={"symbol":symbol,"local":local,"references":refs,"errors":errors}
        self.run.event("fair_quote_snapshot",snapshot)
        return snapshot

    def error(self,phase,exc,symbol=None):
        self.run.state["fair_errors"]+=1
        self.run.event("fair_data_error",{"phase":phase,"symbol":symbol,"error_type":type(exc).__name__,
                                          "reason":str(exc) if isinstance(exc,ValueError) else "market_data_unavailable"},save=True)

    def persist(self):
        self.wallet.reconcile()
        self.run.state["fair_wallet"]=self.wallet.state
        for key in ("cash_usdt","position","trades","fees_usdt","peak_equity_usdt","max_drawdown_pct"):
            self.run.state[key]=self.wallet.state[key]
        self.run.state["realized_pnl_usdt"]=str(sum((d(t["net_pnl_usdt"]) for t in self.wallet.state["trades"]),d("0")))
        self.run.state["fair_baselines"]={s:list(w.rows) for s,w in self.windows.items()}
        self.run.event("fair_checkpoint",{"snapshots":self.run.state["fair_snapshots"]},save=True)

    def status(self):
        state=self.run.state
        wallet=self.wallet.state
        record={"at":utc(),"pid":os.getpid(),"mode":"OBSERVE_ONLY" if self.observe_only else "LIVE_DATA_LOCAL_PAPER",
                "status":state["fair_status"],"started_epoch":state["fair_started_epoch"],
                "deadline_epoch":state["fair_started_epoch"]+self.config.runtime_seconds,
                "initial_cash_usdt":self.config.initial_cash,"cash_usdt":wallet["cash_usdt"],
                "last_liquidation_equity_usdt":str(self.last_equity) if self.last_equity is not None else None,
                "equity_mark_epoch":self.last_equity_epoch,"open_position":wallet["position"],
                "completed_round_trips":len(wallet["trades"]),"winning_round_trips":sum(d(t["net_pnl_usdt"])>0 for t in wallet["trades"]),
                "realized_net_usdt":str(sum((d(t["net_pnl_usdt"]) for t in wallet["trades"]),d("0"))),
                "fees_usdt":wallet["fees_usdt"],"max_drawdown_pct":wallet["max_drawdown_pct"],
                "snapshots":state["fair_snapshots"],"data_errors":state["fair_errors"],"rejections":state["fair_rejections"],
                "baselines":{s:w.snapshot(time.time()) for s,w in self.windows.items()},"fee_schedule":self.fee_info,
                "public_get_requests_this_process":self.feeds.requests,"real_orders":0,"credentials_used_by_strategy":False,
                "notes":["USDT is a dollar proxy; fills local and not exchange-confirmed.",
                         "Potential return assumes future local-basis convergence, not a demonstrated expected return.",
                         "No passive fills inferred from price touches. Fees charged in quote are an assumption.",
                         "Wallex provides receipt freshness here, not a verified exchange book-event timestamp."]}
        temporary=self.run.directory/"fair_status.tmp"
        temporary.write_text(json.dumps(record,indent=2,default=str)+"\n")
        temporary.replace(self.run.directory/"status.json")
        return record

    async def enter(self,decision):
        symbol=decision["symbol"]
        # Re-fetch all venues after selection; never fill against the older signal.
        snapshot=await self.snapshot(symbol)
        if snapshot["local"] is None:
            raise ValueError("missing_entry_book")
        now=time.time()
        reference=reference_price(snapshot["local"],snapshot["references"],now,self.config)
        fresh=opportunity(snapshot["local"],reference,self.windows[symbol].snapshot(now),
                          self.wallet.state["cash_usdt"],self.fees[symbol],self.meta[symbol],self.config)
        if not fresh["eligible"]:
            self.pending.pop(symbol,None)
            self.run.event("fair_entry_rejected_on_refresh",{"symbol":symbol,"reasons":fresh["reasons"]})
            return
        order=fresh["order"]
        callback=auto_approve_with_risk_check(self.risk,RiskState(total_exposure=0,executor_count=0,
                     drawdown_pct=float(d(self.wallet.state["max_drawdown_pct"]))),agent_id=OWNER,
                     price_client=PaperPriceClient(snapshot["local"]))
        permission=await callback({"tool":"create_order_executor","input":{"controller_id":OWNER,
             "connector_name":"wallex_public_paper","trading_pair":symbol[:-4]+"-USDT",
             "amount":order["quantity"],"leverage":1}},OPTIONS)
        if permission["outcome"]["outcome"]!="selected":
            raise ValueError("Condor paper risk refusal")
        reference_price(snapshot["local"],snapshot["references"],time.time(),self.config)
        fresh.update(symbol=symbol,signal_observed_epoch=decision["signal_observed_epoch"],
                     condor_permission=permission,entry_snapshot_epoch=now)
        self.wallet.enter(symbol,order,fresh,self.fees[symbol],now)
        self.wallet.state["position"]["executor_id"]=f"fair_paper_{len(self.wallet.state['trades'])+1}"
        self.run.event("fair_paper_buy",self.wallet.state["position"],save=True)
        self.last_equity=self.wallet.equity(snapshot["local"])
        self.last_equity_epoch=now
        self.persist()
        print(encoded({"event":"fair_paper_buy","symbol":symbol,"spent":order["spent"]}),flush=True)

    async def exit(self,reason):
        position=self.wallet.state["position"]
        book=await self.feeds.book("wallex",position["symbol"])
        validate_book(book,time.time(),self.config)
        callback=auto_approve_with_risk_check(self.risk,RiskState(),agent_id=OWNER,
                           executor_owners={position["executor_id"]:OWNER})
        permission=await callback({"tool":"stop_executor","input":{"executor_id":position["executor_id"],"keep_position":False}},OPTIONS)
        if permission["outcome"]["outcome"]!="selected":
            raise ValueError("Condor owned paper exit refused")
        validate_book(book,time.time(),self.config)
        trade=self.wallet.exit(book,reason,time.time())
        self.run.event("fair_paper_sell",trade,save=True)
        self.last_equity=self.wallet.equity()
        self.last_equity_epoch=time.time()
        self.persist()
        print(encoded({"event":"fair_round_trip",**trade}),flush=True)

    async def step(self,snapshots,stopping=False):
        # Exits need only a fresh Wallex book. Reference outages never disable stops.
        position=self.wallet.state["position"]
        if position:
            snapshot=snapshots.get(position["symbol"],{})
            book=snapshot.get("local")
            if book is None:
                book=await self.feeds.book("wallex",position["symbol"])
            now=time.time()
            try:
                validate_book(book,now,self.config)
            except ValueError:
                # Slow reference timeouts can age an otherwise successful local
                # request. Re-read Wallex independently so stops still function.
                book=await self.feeds.book("wallex",position["symbol"])
                now=time.time()
                validate_book(book,now,self.config)
            value=self.wallet.equity(book)
            self.last_equity,self.last_equity_epoch=value,now
            liquid=value-d(self.wallet.state["cash_usdt"])
            net=liquid/d(position["spent"])-1
            reason=None
            if stopping:
                reason="session_wind_down"
            elif net<=-d(self.config.stop_net_fraction):
                reason="net_stop"
            elif d(self.wallet.state["max_drawdown_pct"])>=2:
                reason="account_drawdown"
            elif now-position["opened_epoch"]>=self.config.holding_seconds:
                reason="time_limit"
            else:
                try:
                    reference=reference_price(book,snapshot.get("references",[]),now,self.config)
                    basis=math.log(float(mid(book)/reference))
                    baseline=position["baseline_at_entry"]
                    tolerance=max(float(d(self.config.exit_tolerance_bps)/10000),baseline["sigma"])
                    if basis>=baseline["median_log_basis"]-tolerance:
                        reason="local_basis_recovered"
                except ValueError:
                    pass
            if reason:
                await self.exit(reason)
        choices=[]
        for symbol,snapshot in snapshots.items():
            now=time.time()
            try:
                if snapshot["local"] is None:
                    raise ValueError("wallex_book_missing")
                reference=reference_price(snapshot["local"],snapshot["references"],now,self.config)
                baseline=self.windows[symbol].snapshot(now)
                basis=math.log(float(mid(snapshot["local"])/reference))
                if not math.isfinite(basis) or abs(basis)>.10:
                    raise ValueError("unreasonable_same_quote_basis")
                decision=opportunity(snapshot["local"],reference,baseline,self.wallet.state["cash_usdt"],
                                     self.fees[symbol],self.meta[symbol],self.config)
                decision.update(symbol=symbol,signal_observed_epoch=now)
                # Add this observation only AFTER evaluating against past history.
                self.windows[symbol].add(now,basis)
                self.run.state["fair_snapshots"]+=1
                self.run.event("fair_decision",decision)
                if decision["eligible"]:
                    pending=self.pending.setdefault(symbol,{"since":now,"samples":0})
                    pending["samples"]+=1
                    if now-pending["since"]>=self.config.persistence_seconds and pending["samples"]>=self.config.minimum_persistent_samples:
                        choices.append(decision)
                else:
                    self.pending.pop(symbol,None)
                    for reason in decision["reasons"]:
                        counters=self.run.state["fair_rejections"]
                        counters[reason]=counters.get(reason,0)+1
            except (ValueError,KeyError,TypeError) as exc:
                self.pending.pop(symbol,None)
                self.error("snapshot",exc,symbol)
        if self.wallet.state["position"] is None:
            self.last_equity,self.last_equity_epoch=self.wallet.equity(),time.time()
        remaining=self.config.runtime_seconds-(time.time()-self.run.state["fair_started_epoch"])
        blocked=[]
        if self.observe_only: blocked.append("observation_mode")
        if stopping: blocked.append("session_wind_down")
        if self.wallet.state["position"] is not None: blocked.append("position_already_open")
        if remaining<self.config.holding_seconds+30: blocked.append("insufficient_remaining_holding_window")
        if len(self.wallet.state["trades"])>=self.config.maximum_trades: blocked.append("trade_ceiling")
        if time.time()-self.wallet.state["last_exit_epoch"]<self.config.cooldown_seconds: blocked.append("cooldown")
        if d(self.wallet.state["max_drawdown_pct"])>=2: blocked.append("account_drawdown_limit")
        if not choices: blocked.append("await_persistence" if self.pending else "no_eligible_opportunity")
        if not blocked:
            choices.sort(key=lambda r:(-d(r["potential_net_return_at_normal_basis"]),r["symbol"]))
            await self.enter(choices[0])
        else:
            self.run.event("fair_no_entry",{"reasons":blocked})
        self.persist()

    async def execute(self):
        await self.setup()
        deadline=self.run.state["fair_started_epoch"]+self.config.runtime_seconds
        while True:
            stopping=self.stop.is_set() or time.time()>=deadline
            if stopping and self.wind_down_started is None:
                self.wind_down_started=time.time()
            if stopping and self.wallet.state["position"] is None:
                break
            answers=await asyncio.gather(*(self.snapshot(s) for s in self.meta))
            try:
                await self.step({r["symbol"]:r for r in answers},stopping)
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError) as exc:
                self.error("cycle",exc)
                self.persist()
            if stopping and self.wallet.state["position"] is not None and time.time()>self.wind_down_started+300:
                self.run.state["fair_status"]="incomplete_open_paper_position_missing_executable_quote"
                break
            ready=all(w.snapshot(time.time())["ready"] for w in self.windows.values())
            self.run.state["fair_status"]=("winding_down" if stopping else "observing" if self.observe_only
                                          else "paper_active" if ready else "calibrating")
            status=self.status()
            print(encoded({k:status[k] for k in ("at","status","snapshots","cash_usdt","completed_round_trips","data_errors")}),flush=True)
            if stopping:
                await asyncio.sleep(2)
            else:
                try:
                    await asyncio.wait_for(self.stop.wait(),timeout=self.config.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        if self.wallet.state["position"] is None:
            self.run.state["fair_status"]="complete_observation" if self.observe_only else "complete_paper"
        self.persist()
        result=self.status()
        result["trades"]=self.wallet.state["trades"]
        (self.run.directory/"results.json").write_text(json.dumps(result,indent=2,default=str)+"\n")
        if result["trades"]:
            with (self.run.directory/"trades.csv").open("w",newline="") as handle:
                writer=csv.DictWriter(handle,fieldnames=list(result["trades"][0]))
                writer.writeheader()
                writer.writerows(result["trades"])
        print(encoded({"event":"fair_session_finished","directory":str(self.run.directory),"status":result["status"]}),flush=True)
        return result


async def start(args):
    config=FairConfig(runtime_seconds=args.minutes*60,poll_seconds=args.poll_seconds)
    directory=args.resume or args.output or Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("fairvalue_%Y%m%dT%H%M%S%fZ")
    legacy=Config(initial_cash=config.initial_cash,target_trades=config.maximum_trades,holding_seconds=config.holding_seconds,
                  max_runtime_seconds=config.runtime_seconds)
    run=Run(directory,legacy)
    session=None
    try:
        fees,info=fees_from_file(args.fee_file,config,time.time())
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),connector=aiohttp.TCPConnector(limit=6),
                    cookie_jar=aiohttp.DummyCookieJar(),trust_env=True,headers={"Accept":"application/json",
                    "User-Agent":"Wallex-FairValue-Paper/0.1","Cache-Control":"no-cache"}) as http:
            session=FairSession(run,config,FairFeeds(http,run),fees,info,args.observe_only)
            loop=asyncio.get_running_loop()
            for signum in (signal.SIGINT,signal.SIGTERM):
                loop.add_signal_handler(signum,session.stop.set)
            await session.execute()
    except Exception as exc:
        run.state["fair_status"]="failed"
        run.event("fair_fatal_error",{"error_type":type(exc).__name__},save=True)
        if session is not None:
            session.status()
        raise
    finally:
        run.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes",type=int,default=240)
    p.add_argument("--poll-seconds",type=int,default=10)
    p.add_argument("--observe-only",action="store_true")
    p.add_argument("--fee-file",type=Path,help="Sanitized fee report only; never an API key file")
    p.add_argument("--output",type=Path)
    p.add_argument("--resume",type=Path)
    args=p.parse_args()
    if args.output and args.resume:
        p.error("Choose output or resume")
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    os.environ["NETRC"]="/dev/null"
    for key in list(os.environ):
        if any(s in key.upper() for s in ("API_KEY","API_SECRET","WALLEX","TELEGRAM_TOKEN")):
            os.environ.pop(key,None)
    asyncio.run(start(args))


if __name__=="__main__":
    main()
