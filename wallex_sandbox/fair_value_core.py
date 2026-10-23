"""Paper-only fair-value logic. No network, credentials, order submission or LLM."""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN

from .live_paper import d, mid, spread_bps, vwap


@dataclass(frozen=True)
class FairConfig:
    initial_cash: str="1000"
    maximum_spend: str="100"
    risk_fraction: str="0.001"
    stop_net_fraction: str="0.01"
    assumed_fee: str="0.002"
    slippage_bps: str="1"
    minimum_net_target: str="0.002"
    minimum_reward_risk: str="1"
    maximum_spread_bps: str="30"
    maximum_impact_bps: str="10"
    visible_depth_fraction: str="0.05"
    reference_disagreement_bps: str="15"
    maximum_quote_age_seconds: int=8
    maximum_snapshot_skew_seconds: int=3
    baseline_window_seconds: int=7200
    warmup_seconds: int=900
    minimum_baseline_samples: int=90
    persistence_seconds: int=30
    minimum_persistent_samples: int=3
    maximum_basis_gap_seconds: int=120
    maximum_baseline_sigma_bps: str="20"
    uncertainty_sigma_multiple: str="2"
    exit_tolerance_bps: str="5"
    holding_seconds: int=7200
    cooldown_seconds: int=900
    poll_seconds: int=10
    runtime_seconds: int=14400
    maximum_trades: int=10

    def __post_init__(self):
        if min(d(self.initial_cash),d(self.maximum_spend),d(self.risk_fraction),d(self.stop_net_fraction))<=0:
            raise ValueError("Invalid paper risk/budget")
        if not 0<=d(self.assumed_fee)<1 or not 0<d(self.risk_fraction)<=1 or not 0<d(self.stop_net_fraction)<1:
            raise ValueError("Invalid paper fraction")
        if self.warmup_seconds<60 or self.minimum_baseline_samples<5 or self.baseline_window_seconds<self.warmup_seconds:
            raise ValueError("Invalid calibration window")
        if self.poll_seconds<2 or self.runtime_seconds<60 or self.holding_seconds<60 or self.maximum_trades<1:
            raise ValueError("Invalid paper-session timing")
        if not 0<d(self.visible_depth_fraction)<=1 or d(self.minimum_reward_risk)<0 or d(self.minimum_net_target)<=0:
            raise ValueError("Invalid cost/depth policy")


def validate_book(book,now,config):
    age=now-book["received_epoch"]
    if not 0<=age<=config.maximum_quote_age_seconds:
        raise ValueError("stale_receipt")
    if book.get("exchange_epoch") is not None and not -2<=now-book["exchange_epoch"]<=config.maximum_quote_age_seconds:
        raise ValueError("stale_or_future_exchange_timestamp")
    cache_age=book.get("cache_age",0)
    if not math.isfinite(cache_age) or not 0<=cache_age<=config.maximum_quote_age_seconds:
        raise ValueError("stale_cached_book")
    if (not book["bids"] or not book["asks"] or any(p<=0 or q<=0 for p,q in book["bids"]+book["asks"])
        or book["bids"][0][0]>=book["asks"][0][0]):
        raise ValueError("invalid_or_crossed_book")


def reference_price(local,references,now,config):
    if len(references)<2 or len({r["venue"] for r in references})!=len(references):
        raise ValueError("two_independent_reference_venues_required")
    books=[local]+references
    for book in books:
        validate_book(book,now,config)
        if book["symbol"]!=local["symbol"] or book.get("quote","USDT")!="USDT" or book.get("market_type","spot")!="spot":
            raise ValueError("reference_instrument_mismatch")
    receipts=[r["received_epoch"] for r in books]
    if max(receipts)-min(receipts)>config.maximum_snapshot_skew_seconds:
        raise ValueError("asynchronous_snapshot")
    prices=[mid(r) for r in references]
    price=sum(prices,Decimal(0))/len(prices)
    if (max(prices)-min(prices))/price*10000>d(config.reference_disagreement_bps):
        raise ValueError("reference_venues_disagree")
    return price


class BasisWindow:
    def __init__(self,config):
        self.config=config
        self.rows=deque()

    def trim(self,now):
        if self.rows and now<self.rows[-1][0]:
            self.rows.clear()
        while self.rows and self.rows[0][0]<now-self.config.baseline_window_seconds:
            self.rows.popleft()
        if self.rows and now-self.rows[-1][0]>self.config.maximum_basis_gap_seconds:
            self.rows.clear()

    def snapshot(self,now):
        self.trim(now)
        values=[x[1] for x in self.rows]
        if not values:
            return {"samples":0,"span_seconds":0,"ready":False}
        median=statistics.median(values)
        sigma=statistics.median(abs(x-median) for x in values)*1.4826
        span=self.rows[-1][0]-self.rows[0][0]
        return {"samples":len(values),"span_seconds":span,"median_log_basis":median,"sigma":sigma,
                "ready":len(values)>=self.config.minimum_baseline_samples and span>=self.config.warmup_seconds}

    def add(self,now,basis):
        if not math.isfinite(basis):
            raise ValueError("Invalid basis")
        self.trim(now)
        if self.rows and now<=self.rows[-1][0]:
            raise ValueError("Non-increasing sample time")
        # Repeated identical prices are time observations, not independent trades.
        self.rows.append((now,basis))


def sized_entry(book,cash,fee,metadata,config):
    if not 0<=fee<=d("0.02") or d(cash)<=0:
        raise ValueError("invalid_fee_or_cash")
    slip=d(config.slippage_bps)/10000
    budget=min(d(cash)*Decimal("0.1"),d(config.maximum_spend),
               d(cash)*d(config.risk_fraction)/d(config.stop_net_fraction))
    quantum=Decimal(1).scaleb(-metadata["quantity_precision"])
    quantity=(budget/((1+fee)*book["asks"][0][0]*(1+slip))).quantize(quantum,rounding=ROUND_DOWN)
    if quantity<=0:
        raise ValueError("quantity_too_small")
    for _ in range(4):
        price=vwap(book,"BUY",quantity)*(1+slip)
        quantity=min(quantity,(budget/(price*(1+fee))).quantize(quantum,rounding=ROUND_DOWN))
    price=vwap(book,"BUY",quantity)*(1+slip)
    impact=(price/(1+slip)/book["asks"][0][0]-1)*10000
    available=sum((p*q for p,q in book["asks"] if p<=book["asks"][0][0]*(1+d(config.maximum_impact_bps)/10000)),Decimal(0))
    notional=price*quantity
    if impact>d(config.maximum_impact_bps) or notional>available*d(config.visible_depth_fraction):
        raise ValueError("insufficient_nearby_depth_or_excess_impact")
    if quantity<d(metadata["min_quantity"]) or notional<d(metadata["min_notional"]):
        raise ValueError("minimum_order_limit")
    spent=notional*(1+fee)
    if spent>budget or spent>d(cash):
        raise ValueError("all_in_budget_exceeded")
    # Also require a presently visible exit route; no optimistic phantom depth.
    immediate=vwap(book,"SELL",quantity)*(1-slip)*quantity*(1-fee)
    if immediate/spent-1<=-d(config.stop_net_fraction):
        raise ValueError("costs_already_exceed_stop")
    return {"quantity":str(quantity),"price":str(price),"notional":str(notional),"fee":str(notional*fee),
            "spent":str(spent),"immediate_liquidation":str(immediate),"budget":str(budget)}


def opportunity(book,reference,baseline,cash,fee,metadata,config):
    if not baseline["ready"]:
        return {"eligible":False,"reasons":["baseline_warming_up"],"baseline":baseline}
    if baseline["sigma"]*10000>float(d(config.maximum_baseline_sigma_bps)):
        return {"eligible":False,"reasons":["local_basis_unstable"],"baseline":baseline}
    fair=reference*d(math.exp(baseline["median_log_basis"]))
    lower=reference*d(math.exp(baseline["median_log_basis"]-float(d(config.uncertainty_sigma_multiple))*baseline["sigma"]))
    observed_basis=math.log(float(mid(book)/reference))
    if not math.isfinite(observed_basis) or abs(observed_basis)>.10:
        return {"eligible":False,"reasons":["unreasonable_same_quote_basis"],"baseline":baseline}
    summary={"fair_mid":str(fair),"conservative_fair_mid":str(lower),"reference_mid":str(reference),
             "local_mid":str(mid(book)),"discount_to_normal_bps":str((fair/mid(book)-1)*10000),
             "observed_log_basis":observed_basis,"baseline":baseline}
    if spread_bps(book)>d(config.maximum_spread_bps):
        return {**summary,"eligible":False,"reasons":["wallex_spread_too_wide"]}
    try:
        order=sized_entry(book,cash,fee,metadata,config)
    except ValueError as exc:
        return {**summary,"eligible":False,"reasons":[str(exc)]}
    exit_half_spread=max(spread_bps(book),d(config.maximum_spread_bps))/20000
    projected=d(order["quantity"])*lower*(1-exit_half_spread)*(1-d(config.slippage_bps)/10000)*(1-fee)
    potential=projected/d(order["spent"])-1
    hurdle=max(d(config.minimum_net_target),d(config.minimum_reward_risk)*d(config.stop_net_fraction))
    reasons=[] if potential>=hurdle else ["insufficient_net_target_room"]
    return {**summary,"eligible":not reasons,"reasons":reasons,"order":order,
            "potential_net_return_at_normal_basis":str(potential),"required_net_return":str(hurdle),
            "target_is_not_a_forecast":True}


class PaperWallet:
    def __init__(self,config,state=None):
        self.config=config
        self.state=state if state is not None else {"cash_usdt":config.initial_cash,"position":None,"trades":[],
                  "fees_usdt":"0","peak_equity_usdt":config.initial_cash,"max_drawdown_pct":"0","last_exit_epoch":0}
        self.reconcile()

    def reconcile(self):
        realized=sum((d(t["net_pnl_usdt"]) for t in self.state["trades"]),d("0"))
        cost=d(self.state["position"]["spent"]) if self.state["position"] else d("0")
        if d(self.state["cash_usdt"])<0 or abs(d(self.config.initial_cash)+realized-d(self.state["cash_usdt"])-cost)>d("0.00000001"):
            raise ValueError("Paper wallet does not reconcile")
        fees=sum((d(t["entry_fee"])+d(t["exit_fee"]) for t in self.state["trades"]),d("0"))
        if self.state["position"]:
            fees+=d(self.state["position"]["entry_fee"])
        if abs(fees-d(self.state["fees_usdt"]))>d("0.00000001"):
            raise ValueError("Paper fees do not reconcile")

    def equity(self,book=None):
        value=d(self.state["cash_usdt"])
        p=self.state["position"]
        if p:
            if book is None or book["symbol"]!=p["symbol"]:
                raise ValueError("Matching exit book required")
            value+=vwap(book,"SELL",d(p["quantity"]))*d(p["quantity"])*(1-d(self.config.slippage_bps)/10000)*(1-d(p["fee_rate"]))
        peak=max(value,d(self.state["peak_equity_usdt"]))
        self.state["peak_equity_usdt"]=str(peak)
        self.state["max_drawdown_pct"]=str(max(d(self.state["max_drawdown_pct"]),(peak-value)/peak*100))
        return value

    def enter(self,symbol,order,decision,fee,now):
        fee=d(fee)
        if self.state["position"] is not None:
            raise ValueError("Only one paper position allowed")
        if d(self.state["max_drawdown_pct"])>=2:
            raise ValueError("Account drawdown limit")
        if now-self.state["last_exit_epoch"]<self.config.cooldown_seconds:
            raise ValueError("Paper cooldown")
        if len(self.state["trades"])>=self.config.maximum_trades:
            raise ValueError("Paper trade ceiling")
        spent=d(order["spent"])
        quantity,price=d(order["quantity"]),d(order["price"])
        budget=min(d(self.state["cash_usdt"])*d("0.1"),d(self.config.maximum_spend),
                   d(self.state["cash_usdt"])*d(self.config.risk_fraction)/d(self.config.stop_net_fraction))
        if (not decision.get("eligible") or min(quantity,price,spent)<=0 or not 0<=fee<=d("0.02") or spent>budget
            or abs(spent-quantity*price*(1+fee))>d("0.00000001")
            or abs(d(order["fee"])-quantity*price*fee)>d("0.00000001")):
            raise ValueError("Invalid paper spend, fee or decision")
        self.state["cash_usdt"]=str(d(self.state["cash_usdt"])-spent)
        self.state["fees_usdt"]=str(d(self.state["fees_usdt"])+d(order["fee"]))
        self.state["position"]={"symbol":symbol,"quantity":order["quantity"],"entry_price":order["price"],
             "spent":order["spent"],"entry_fee":order["fee"],"fee_rate":str(fee),"opened_epoch":now,
             "baseline_at_entry":decision["baseline"],"decision":decision}
        self.reconcile()

    def exit(self,book,reason,now):
        p=self.state["position"]
        if not p or p["symbol"]!=book["symbol"]:
            raise ValueError("Matching position required")
        validate_book(book,now,self.config)
        quantity=d(p["quantity"])
        price=vwap(book,"SELL",quantity)*(1-d(self.config.slippage_bps)/10000)
        proceeds=price*quantity
        fee=proceeds*d(p["fee_rate"])
        net=proceeds-fee-d(p["spent"])
        trade={"symbol":p["symbol"],"opened_epoch":p["opened_epoch"],"closed_epoch":now,"quantity":p["quantity"],
               "entry_price":p["entry_price"],"exit_price":str(price),"entry_fee":p["entry_fee"],"exit_fee":str(fee),
               "spent":p["spent"],"gross_pnl_usdt":str(proceeds-quantity*d(p["entry_price"])),
               "net_pnl_usdt":str(net),"exit_reason":reason,"simulated_fill":True}
        self.state["cash_usdt"]=str(d(self.state["cash_usdt"])+proceeds-fee)
        self.state["fees_usdt"]=str(d(self.state["fees_usdt"])+fee)
        self.state["trades"].append(trade)
        self.state["position"]=None
        self.state["last_exit_epoch"]=now
        self.equity()
        self.reconcile()
        return trade
