"""No network clients, exchange credentials, external model calls or live orders.

The decision source is scripted. Condor's production risk/permission callback is
real; the Wallex broker, prices, order fills and balance are deliberately fake.
This is an integration probe, not a production Hummingbot/Wallex connector.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from condor.agents.risk import RefusalLog, RiskEngine, RiskLimits, RiskState, auto_approve_with_risk_check

CONTROLLER = "wallex_paper_challenge"
CONNECTOR = "wallex_paper"
OPTIONS = [{"kind": "allow_once", "optionId": "paper_allow"}]


def number(value) -> Decimal:
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Non-finite monetary value")
    return value


def stable(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


@contextmanager
def offline_only():
    """Block IP connections, including child asyncio HTTP clients in this process."""
    def deny(*_args, **_kwargs):
        raise RuntimeError("NETWORK_DISABLED: offline Condor paper sandbox")
    clean_env = {k: v for k, v in os.environ.items() if not any(
        word in k.upper() for word in ("API_KEY", "API_SECRET", "TOKEN", "PASSWORD", "WALLEX"))}
    clean_env["PYTHON_DOTENV_DISABLED"] = "1"
    with patch.dict(os.environ, clean_env, clear=True), patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny), patch.object(socket, "create_connection", deny):
        yield


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    at: int


class FakeWallex:
    fee = Decimal("0.003")  # An explicit simulator assumption, not the account's fee.

    def __init__(self, path: Path):
        self.path = path
        self.quotes: dict[str, Quote] = {}
        self.state = json.loads(path.read_text()) if path.exists() else {
            "mode": "FAKE", "initial_cash_tmn": "5000000", "cash_tmn": "5000000",
            "positions": {}, "orders": {}, "realized_pnl_tmn": "0", "fees_tmn": "0", "peak_tmn": "5000000"}

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2)+"\n")
        temporary.replace(self.path)

    def quote(self, symbol: str, bid, ask=None, at: int = 1000):
        bid, ask = number(bid), number(ask if ask is not None else bid)
        if bid <= 0 or ask < bid:
            raise ValueError("Invalid fake quote")
        self.quotes[symbol] = Quote(bid, ask, at)

    def equity(self) -> Decimal:
        return number(self.state["cash_tmn"])+sum((number(p["quantity"])*self.quotes[s].bid
                   for s, p in self.state["positions"].items() if number(p["quantity"])), Decimal(0))

    def fill(self, intent_id: str, call: dict, side: str, quantity: Decimal, symbol: str, *, timeout_after_acceptance=False) -> dict:
        fingerprint = hashlib.sha256(stable(call).encode()).hexdigest()
        old = self.state["orders"].get(intent_id)
        if old:
            if old["intent_hash"] != fingerprint:
                raise ValueError("Intent ID reused with changed arguments")
            return {**old, "replayed": True}
        p = self.state["positions"].get(symbol, {"quantity": "0", "cost": "0"})
        held, cost, cash = number(p["quantity"]), number(p["cost"]), number(self.state["cash_tmn"])
        quote = self.quotes[symbol]
        price = quote.ask if side == "BUY" else quote.bid
        notional, fee = quantity*price, quantity*price*self.fee
        if quantity <= 0 or quantity != quantity.quantize(Decimal("0.00000001")):
            raise ValueError("Invalid simulator quantity/precision")
        if side == "BUY":
            if notional+fee > cash:
                raise ValueError("Insufficient fake cash including fee")
            cash -= notional+fee
            held, cost = held+quantity, cost+notional+fee
        else:
            if quantity > held:
                raise ValueError("Cannot sell more fake inventory than held")
            basis = cost*quantity/held
            cash += notional-fee
            self.state["realized_pnl_tmn"] = str(number(self.state["realized_pnl_tmn"])+notional-fee-basis)
            held, cost = held-quantity, cost-basis
        result = {"intent_id": intent_id, "intent_hash": fingerprint, "mode": "FAKE", "status": "FILLED",
                  "symbol": symbol, "side": side, "quantity": str(quantity), "price_tmn": str(price),
                  "fee_tmn": str(fee), "owner": CONTROLLER, "replayed": False}
        self.state["cash_tmn"] = str(cash)
        self.state["fees_tmn"] = str(number(self.state["fees_tmn"])+fee)
        self.state["positions"][symbol] = {"quantity": str(held), "cost": str(cost)}
        self.state["orders"][intent_id] = result
        self.persist()  # Acceptance is durable before the simulated response timeout.
        if timeout_after_acceptance:
            raise TimeoutError("FAKE: response lost after order acceptance")
        return result


class FakePriceClient:
    def __init__(self, broker: FakeWallex):
        self.broker = broker
        self.market_data = self

    async def get_prices(self, connector_name, trading_pairs):
        names = [trading_pairs] if isinstance(trading_pairs, str) else trading_pairs
        return {"prices": {s: float(self.broker.quotes[s.replace("-", "/")].ask) for s in names
                           if s.replace("-", "/") in self.broker.quotes}}


def buy(symbol="BTC/TMN", quantity="0.1", **overrides) -> dict:
    args = {"controller_id": CONTROLLER, "connector_name": CONNECTOR,
            "trading_pair": symbol.replace("/", "-"), "amount": quantity, "leverage": 1}
    args.update(overrides)
    return {"tool": "create_order_executor", "input": args}


class PaperHarness:
    def __init__(self, root: Path, *, mode="loop", initial_cash=None):
        if mode not in {"loop", "dry_run"}:
            raise ValueError("Only local loop simulation and Condor dry_run are supported")
        self.root, self.mode = root, mode
        self.broker = FakeWallex(root/"fake-wallet.json")
        if initial_cash is not None:
            self.broker.state["cash_tmn"] = str(number(initial_cash))
        denied = Path(__file__).with_name("excluded-markets.json")
        self.excluded = frozenset(json.loads(denied.read_text())["excluded_pairs"])
        self.limits = RiskLimits(max_position_size_quote=500000, max_open_executors=2,
                                 max_drawdown_pct=2, shutdown_drawdown_pct=4, max_leverage=1, max_drift_quote=10000)
        self.risk = RiskEngine(self.limits)
        self.events = []

    def record(self, result: dict) -> dict:
        self.events.append(result)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/"decisions.jsonl").open("a") as output:
            output.write(stable(result)+"\n")
        return result

    async def dispatch(self, intent_id: str, call: dict, *, now=1000, timeout_after_acceptance=False,
                       drawdown=0, book_trusted=True) -> dict:
        def deny(reason):
            return self.record({"intent_id": intent_id, "mode": "FAKE", "outcome": "DENIED", "reason": reason})
        tool, args = call.get("tool"), call.get("input")
        # The local bridge exposes only these three known tool shapes.
        if tool not in {"create_order_executor", "stop_executor", "get_market_data"} or not isinstance(args, dict):
            return deny("Tool is outside the offline bridge allowlist")
        if tool == "get_market_data":
            callback = auto_approve_with_risk_check(self.risk, RiskState(), execution_mode=self.mode)
            verdict = await callback(call, OPTIONS)
            return self.record({"intent_id": intent_id, "mode": "FAKE", "outcome": "READ_ONLY",
                                "condor_permission": verdict, "data": {s: str(q.ask) for s, q in self.broker.quotes.items()}})
        symbol = str(args.get("trading_pair", "")).replace("-", "/")
        if args.get("connector_name") != CONNECTOR:
            return deny("Only the fake Wallex connector is accepted")
        if symbol not in {"BTC/TMN", "ETH/TMN"} or symbol in self.excluded:
            return deny("Pair is excluded or outside the narrow TMN paper universe")
        old = self.broker.state["orders"].get(intent_id)
        if old:
            if old["intent_hash"] != hashlib.sha256(stable(call).encode()).hexdigest():
                return deny("Intent ID reused with changed arguments")
            return self.record({"intent_id": intent_id, "mode": "FAKE", "outcome": "RECONCILED", "fill": {**old, "replayed": True}})
        quote = self.broker.quotes.get(symbol)
        if quote is None or not 0 <= now-quote.at <= 10:
            return deny("Missing or stale fake quote")
        try:
            qty = number(args.get("amount", 0))
            if qty <= 0:
                return deny("Quantity must be positive")
        except (ValueError, ArithmeticError):
            return deny("Quantity is not finite")
        if qty*quote.ask < 50000:
            return deny("Below assumed paper minimum notional")
        class Tracker:
            def get_drawdown_pct(self):
                return drawdown
        state = self.risk.get_state(Tracker())
        state.book_trusted = book_trusted
        state.drift_reason = "Fake venue reconciliation mismatch" if not book_trusted else ""
        state.total_exposure = float(sum((number(p["quantity"])*self.broker.quotes[s].ask
                                         for s, p in self.broker.state["positions"].items()), Decimal(0)))
        state.executor_count = sum(number(p["quantity"]) > 0 for p in self.broker.state["positions"].values())
        if tool == "create_order_executor" and (state.is_blocked or state.should_shutdown):
            return deny(state.shutdown_reason or state.block_reason)
        mode = "shutdown" if state.should_shutdown else self.mode
        refusals = RefusalLog()
        owners = {args.get("executor_id"): CONTROLLER} if tool == "stop_executor" and args.get("executor_id") in self.broker.state["orders"] else {}
        callback = auto_approve_with_risk_check(self.risk, state, execution_mode=mode, agent_id=CONTROLLER,
                                              price_client=FakePriceClient(self.broker), refusals=refusals,
                                              executor_owners=owners)
        verdict = await callback(call, OPTIONS)
        if verdict["outcome"]["outcome"] != "selected":
            return deny(verdict.get("reason", "Condor permission gate refused"))
        try:
            fill = self.broker.fill(intent_id, call, "BUY" if tool == "create_order_executor" else "SELL", qty, symbol,
                                    timeout_after_acceptance=timeout_after_acceptance)
        except TimeoutError:
            return self.record({"intent_id": intent_id, "mode": "FAKE", "outcome": "UNKNOWN",
                                "reason": "Simulated timeout after acceptance; reconcile the same ID"})
        except ValueError as exc:
            return deny(str(exc))
        return self.record({"intent_id": intent_id, "mode": "FAKE", "outcome": "FILLED", "fill": fill,
                            "condor_permission": verdict})


async def demonstration(root: Path) -> dict:
    harness = PaperHarness(root)
    harness.broker.quote("BTC/TMN", "2000000")
    first = await harness.dispatch("demo_buy", buy())
    too_large = await harness.dispatch("oversized", buy(quantity="1"))
    banned = await harness.dispatch("excluded", buy("TON/TMN"))
    harness.broker.quote("BTC/TMN", "2020000", at=1001)
    close = {"tool": "stop_executor", "input": {"executor_id": "demo_buy", "connector_name": CONNECTOR,
                                                "trading_pair": "BTC-TMN", "amount": "0.1"}}
    final = await harness.dispatch("demo_sell", close, now=1001)
    return {"mode": "FAKE", "model": "scripted decisions; no LLM inference", "network": "disabled",
            "initial_cash_tmn": "5000000", "final_cash_tmn": harness.broker.state["cash_tmn"],
            "scripted_example_pnl_tmn": harness.broker.state["realized_pnl_tmn"],
            "fee_assumption": str(harness.broker.fee), "results": [first, too_large, banned, final],
            "limitation": "Synthetic scenario checks plumbing; it does not measure a strategy or profitability."}
