"""Real exchange receipts only. Entries use the existing cost-qualified fair-value rule."""
import asyncio
import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path

from .client import ExchangeError, Wallex
from .journal import Journal
from .policy import CUTOFF, CAPITAL_TMN, decimal as D, floor, entry_allowed, validate_buy


class Engine:
    def __init__(self, api, journal, enabled=False, credit_markets=None):
        self.api, self.journal, self.enabled = api, journal, enabled
        policy = {"cutoff": CUTOFF.isoformat(), "capital_tmn": str(CAPITAL_TMN),
                  "final_liquidation_scope": "all_assets_in_selected_account"}
        if journal.get("policy", policy) != policy:
            raise ValueError("persisted_live_policy_changed_requires_review")
        journal.set("policy", policy)
        self.credit_markets = set(credit_markets or [])
        manifest = Path(__file__).resolve().parents[1] / "wallex_sandbox/excluded-markets.json"
        self.excluded = set(json.loads(manifest.read_text())["excluded_pairs"])
        self.markets, self.fees, self.balances = {}, {}, {}
        self.status = {"mode": "REAL", "connected": False, "state": "starting", "orders_enabled": enabled,
                       "cutoff_tehran": CUTOFF.isoformat(), "cutoff_utc": "2026-10-26T20:20:00+00:00",
                       "capital_cap_tmn": str(CAPITAL_TMN), "capital_cap_irr": str(CAPITAL_TMN * 10)}
        self.last_refresh = 0
        self.windows, self.pending_signals = {}, {}

    def eligible(self, symbol):
        row = self.markets.get(symbol, {})
        pair = str(row.get("base_asset")) + "/" + str(row.get("quote_asset"))
        return (row.get("is_spot") is True and pair not in self.excluded
                and (self.api.account_type != "credit" or symbol in self.credit_markets))

    async def refresh(self):
        # A complete authenticated snapshot is required before any mutation.
        balances = await self.api.balances()
        orders = await self.api.open_orders()
        if time.time() - self.last_refresh > 300 or not self.markets:
            directory = await self.api.request("GET", "/hector/web/v1/markets")
            supplemental = await self.api.request("GET", "/v1/markets")
            fees = (await self.api.request("GET", "/v1/account/fee", private=True))["result"]
            self.markets = {r["symbol"]: r for r in directory["result"]["markets"]}
            for symbol, row in self.markets.items():
                legacy = supplemental["result"]["symbols"].get(symbol, {})
                row["min_quantity"] = legacy.get("minQty")
                row["min_notional"] = legacy.get("minNotional")
            self.fees = fees
            self.last_refresh = time.time()
        self.balances = balances
        self.journal.set("account_verified", True)
        self.status.update(connected=True, account_type=self.api.account_type, balances=balances,
                           open_orders=len(orders), last_account_sync=time.time(), error=None)
        self.status["cash_irr"] = str(D(balances.get("TMN", {}).get("total", "0")) * 10
                                      + D(balances.get("IRR", {}).get("total", "0")))
        return orders

    def fee(self, symbol):
        value = D(self.fees[symbol]["takerFeeRate"])
        if not 0 <= value <= D(".02"):
            raise ValueError("unverified_account_fee")
        return value

    async def reconcile(self):
        # An uncertain POST is never submitted again automatically, even on 404.
        for intent in self.journal.pending():
            cid = intent["id"]
            try:
                order = await self.api.order(cid)
            except ExchangeError as exc:
                if exc.status == 404:
                    raise ValueError("ambiguous_order_requires_review:" + cid) from None
                raise
            if order["active"] and time.time() - intent["created"] >= 30:
                await self.api.cancel(cid)
                order = await self.api.order(cid)
            self.journal.update(cid, "acknowledged" if order["active"] else "terminal", order)
        for cid, body, purpose, order in self.journal.unapplied():
            qty, value = D(order["executedQty"]), D(order["executedSum"])
            if qty < 0 or value < 0:
                raise ValueError("invalid_exchange_execution")
            updates = {}
            if qty and purpose != "settlement":
                fills = order.get("fills", [])
                if not fills:
                    raise ValueError("missing_actual_fill_fees")
                base, quote_fee, base_fee = body["symbol"][:-4], D(0), D(0)
                for fill in fills:
                    fee = D(fill["fee"])
                    if fee < 0:
                        raise ValueError("invalid_fill_fee")
                    asset = fill["feeAsset"]
                    if asset == "USDT":
                        quote_fee += fee
                    elif asset == base:
                        base_fee += fee
                    elif fee:
                        raise ValueError("unexpected_fee_asset")
                if body["side"] == "BUY":
                    updates["position"] = {"symbol": body["symbol"], "quantity": str(qty - base_fee),
                                           "spent": str(value + quote_fee), "opened_at": time.time(),
                                           "baseline": self.journal.get("entry_context")}
                else:
                    position = self.journal.get("position")
                    if not position or body["symbol"] != position["symbol"]:
                        raise ValueError("owned_exit_accounting_mismatch")
                    used = qty + base_fee
                    remaining = D(position["quantity"]) - used
                    if remaining < -D(".00000001"):
                        raise ValueError("sold_more_than_owned")
                    cost = D(position["spent"]) * min(D(1), used / D(position["quantity"]))
                    updates["realized_pnl_usdt"] = str(D(self.journal.get("realized_pnl_usdt", "0")) + value - quote_fee - cost)
                    if remaining <= D(1).scaleb(-self.markets[body["symbol"]]["amount_precision"]):
                        updates.update(position=None, last_exit=time.time(),
                                       completed_trades=self.journal.get("completed_trades", 0) + 1)
                    else:
                        updates["position"] = {**position, "quantity": str(remaining),
                                               "spent": str(D(position["spent"]) - cost)}
            self.journal.apply(cid, updates)

    async def submit(self, body, purpose, rate=None, fee=None, context=None):
        if not self.enabled:
            raise ValueError("live_orders_not_armed")
        if not self.status.get("connected") or self.journal.pending():
            raise ValueError("account_or_orders_unreconciled")
        symbol = body["symbol"]
        if not self.eligible(symbol):
            raise ValueError("market_not_permitted")
        if self.api.account_type == "credit" and not self.credit_markets:
            raise ValueError("credit_market_permissions_unverified")
        if body["type"] != "LIMIT" or body["side"] not in {"BUY", "SELL"}:
            raise ValueError("unsupported_live_order")
        fresh_balances = await self.api.balances()
        row = self.markets[symbol]
        quantity, price = D(body["quantity"]), D(body["price"])
        if (quantity != floor(quantity, row["amount_precision"])
                or price != floor(price, row["price_precision"])
                or row["min_quantity"] is None or row["min_notional"] is None
                or quantity < D(row["min_quantity"]) or quantity * price < D(row["min_notional"])):
            raise ValueError("exchange_order_size_or_precision")
        if body["side"] == "SELL":
            base = row["base_asset"]
            if quantity > D(fresh_balances.get(base, {}).get("free", "0")):
                raise ValueError("insufficient_real_asset")
        if purpose == "entry":
            if body["side"] != "BUY" or rate is None:
                raise ValueError("invalid_entry_intent")
            validate_buy(symbol, body["quantity"], body["price"], fee, rate["asks"][0][0],
                         time.time(), rate["received_epoch"])
            if quantity * price * (1 + D(fee)) > D(fresh_balances.get("USDT", {}).get("free", "0")):
                raise ValueError("insufficient_real_usdt")
            if self.journal.get("position"):
                raise ValueError("position_already_open")
            if self.journal.get("completed_trades", 0) >= 10:
                raise ValueError("trade_ceiling")
        elif purpose == "settlement":
            if time.time() < CUTOFF.timestamp() or body["side"] != "SELL":
                raise ValueError("settlement_not_due")
        elif purpose == "exit":
            position = self.journal.get("position")
            if (body["side"] != "SELL" or not position or symbol != position["symbol"]
                    or D(body["quantity"]) > D(position["quantity"])):
                raise ValueError("owned_exit_must_sell")
        else:
            raise ValueError("unknown_order_purpose")
        if D(body["quantity"]) <= 0 or D(body["price"]) <= 0:
            raise ValueError("non_positive_order")
        if context is not None:
            self.journal.set("entry_context", context)
        cid, payload = self.journal.prepare(body, purpose)
        if purpose == "entry" and not entry_allowed(time.time()):
            self.journal.update(cid, "rejected", {"reason": "new_entries_closed"})
            raise ValueError("new_entries_closed")
        # From here on even process death is treated as an ambiguous submission.
        self.journal.update(cid, "submission_unknown")
        try:
            result = await self.api.create(payload)
            if result.get("clientOrderId") != cid:
                raise ExchangeError("created_order_identity_mismatch")
            self.journal.update(cid, "acknowledged", result)
            self.journal.event("real_order_acknowledged", {"client_id": cid, "purpose": purpose})
        except ExchangeError as exc:
            if exc.status in {400, 401, 403, 422}:
                self.journal.update(cid, "rejected", {"http_status": exc.status})
            raise

    def sell_order(self, symbol, available, book):
        row = self.markets[symbol]
        quantity_precision, price_precision = row["amount_precision"], row["price_precision"]
        # Bounded marketable LIMIT, with at most 0.5% price concession.
        limit = floor(book["bids"][0][0] * D(".995"), price_precision)
        depth = sum((q for p, q in book["bids"] if p >= limit), D(0))
        qty = floor(min(D(available), depth * D(".05")), quantity_precision)
        if (row["min_quantity"] is None or row["min_notional"] is None
                or qty < D(row["min_quantity"]) or qty * limit < D(row["min_notional"])):
            raise ValueError("residual_below_minimum_or_depth")
        if not 0 <= time.time() - book["received_epoch"] <= 8:
            raise ValueError("stale_exit_book")
        return {"symbol": symbol, "type": "LIMIT", "side": "SELL", "quantity": str(qty), "price": str(limit)}

    async def settle(self, active):
        # All assets in the explicitly selected account are in the authorized exit scope.
        self.status["state"] = "liquidating_to_irr"
        if not self.enabled:
            self.status["state"] = "settlement_disarmed"
            return
        for order in active:
            await self.api.cancel(order["clientOrderId"])
        await self.reconcile()
        if self.journal.pending() or await self.api.open_orders():
            return
        self.balances = await self.api.balances()
        residuals = {a: v for a, v in self.balances.items() if a not in {"TMN", "IRR"} and D(v["total"]) > 0}
        self.status["residuals"] = residuals
        if not residuals:
            self.status["state"] = "settled_irr"
            self.journal.set("settled_at", time.time())
            return
        blocked = {}
        for asset in sorted(residuals, key=lambda a: a == "USDT"):
            balance = residuals[asset]
            if D(balance["locked"]):
                blocked[asset] = "locked_balance"
                continue
            routes = [asset + "TMN"] + ([asset + "USDT"] if asset != "USDT" else [])
            symbol = next((s for s in routes if self.eligible(s)), None)
            if symbol is None:
                blocked[asset] = "no_permitted_settlement_route"
                continue
            try:
                body = self.sell_order(symbol, balance["free"], await self.api.book(symbol))
                await self.submit(body, "settlement")
                self.status["settlement_blockers"] = blocked
                return
            except (ValueError, KeyError) as exc:
                blocked[asset] = str(exc)
        self.status.update(state="settlement_pending_residuals", settlement_blockers=blocked)

    async def strategy(self, http):
        from wallex_sandbox.fair_value_core import BasisWindow, FairConfig, opportunity, reference_price
        from wallex_sandbox.fair_value_feeds import FairFeeds
        from wallex_sandbox.live_paper import mid
        rate = await self.api.book("USDTTMN")
        allocation = CAPITAL_TMN / rate["asks"][0][0]
        config = FairConfig(initial_cash=str(allocation), maximum_spend=str(allocation / 10))
        pnl = D(self.journal.get("realized_pnl_usdt", "0"))
        if pnl * rate["asks"][0][0] <= -CAPITAL_TMN * D(".02"):
            self.status["entry_blocker"] = "account_loss_limit"
        feeds = FairFeeds(http, self.journal)
        saved = self.journal.get("baselines", {})
        for symbol in ("BTCUSDT", "ETHUSDT"):
            if symbol not in self.windows:
                self.windows[symbol] = BasisWindow(config)
                self.windows[symbol].rows.extend((float(t), float(b)) for t, b in saved.get(symbol, []))
        candidates = []
        position = self.journal.get("position")
        for symbol in self.windows:
            if not self.eligible(symbol):
                continue
            answers = await asyncio.gather(*(feeds.book(v, symbol) for v in ("wallex", "okx", "bybit")), return_exceptions=True)
            local = answers[0]
            if not isinstance(local, dict):
                self.pending_signals.pop(symbol, None)
                continue
            fee = self.fee(symbol)
            if position and position["symbol"] == symbol:
                value = D(position["quantity"]) * local["bids"][0][0] * (1 - fee)
                net = value / D(position["spent"]) - 1
                reason = (net <= -D(".01") or time.time() - position["opened_at"] >= 7200
                          or self.status.get("entry_blocker") == "account_loss_limit")
                refs = [x for x in answers[1:] if isinstance(x, dict)]
                try:
                    ref = reference_price(local, refs, time.time(), config)
                    baseline = position["baseline"]
                    reason = reason or math.log(float(mid(local) / ref)) >= baseline["median_log_basis"] - max(.0005, baseline["sigma"])
                except (ValueError, KeyError, TypeError):
                    pass
                if reason:
                    free = D(self.balances.get(symbol[:-4], {}).get("free", "0"))
                    body = self.sell_order(symbol, min(free, D(position["quantity"])), await self.api.book(symbol))
                    await self.submit(body, "exit")
                    return
            try:
                reference = reference_price(local, [x for x in answers[1:] if isinstance(x, dict)], time.time(), config)
                baseline = self.windows[symbol].snapshot(time.time())
                row = self.markets[symbol]
                metadata = {"quantity_precision": row["amount_precision"], "min_quantity": row["min_quantity"],
                            "min_notional": row["min_notional"]}
                cash = min(D(self.balances.get("USDT", {}).get("free", "0")), allocation)
                signal = opportunity(local, reference, baseline, cash, fee, metadata, config)
                self.windows[symbol].add(time.time(), math.log(float(mid(local) / reference)))
                if not signal["eligible"]:
                    self.pending_signals.pop(symbol, None)
                    continue
                pending = self.pending_signals.setdefault(symbol, {"since": time.time(), "samples": 0})
                pending["samples"] += 1
                if time.time() - pending["since"] >= 30 and pending["samples"] >= 3:
                    candidates.append(symbol)
            except (ValueError, KeyError, TypeError):
                self.pending_signals.pop(symbol, None)
        self.journal.set("baselines", {s: list(w.rows) for s, w in self.windows.items()})
        self.status["calibration"] = {s: w.snapshot(time.time()) for s, w in self.windows.items()}
        if (position or self.journal.pending() or not entry_allowed(time.time())
                or time.time() - self.journal.get("last_exit", 0) < 900
                or self.journal.get("completed_trades", 0) >= 10
                or self.status.get("entry_blocker")):
            return
        for symbol in candidates:
            # Do not mix pre-existing user inventory into a new bot position.
            if D(self.balances.get(symbol[:-4], {}).get("total", "0")) > 0:
                continue
            # Refresh all three sources and the fiat valuation immediately before submitting.
            answers = await asyncio.gather(*(feeds.book(v, symbol) for v in ("wallex", "okx", "bybit")))
            rate = await self.api.book("USDTTMN")
            now = time.time()
            reference = reference_price(answers[0], answers[1:], now, config)
            fee = self.fee(symbol)
            row = self.markets[symbol]
            fresh_cap = CAPITAL_TMN / rate["asks"][0][0]
            fresh_config = replace(config, initial_cash=str(fresh_cap), maximum_spend=str(fresh_cap / 10))
            signal = opportunity(answers[0], reference, self.windows[symbol].snapshot(now),
                                 min(D(self.balances.get("USDT", {}).get("free", "0")), fresh_cap), fee,
                                 {"quantity_precision": row["amount_precision"], "min_quantity": row["min_quantity"],
                                  "min_notional": row["min_notional"]}, fresh_config)
            if not signal["eligible"]:
                continue
            order = signal["order"]
            body = {"symbol": symbol, "type": "LIMIT", "side": "BUY",
                    "quantity": order["quantity"], "price": str(floor(order["price"], row["price_precision"]))}
            await self.submit(body, "entry", rate=rate, fee=fee, context=signal["baseline"])
            return

    async def cycle(self, http):
        self.status.pop("entry_blocker", None)
        active = await self.refresh()
        if time.time() < CUTOFF.timestamp():
            await self.reconcile()
        self.status["position"] = self.journal.get("position")
        if time.time() >= CUTOFF.timestamp():
            await self.settle(active)
        elif not self.enabled:
            self.status["state"] = "connected_orders_disarmed"
        elif self.journal.pending():
            self.status["state"] = "reconciling_real_orders"
        elif self.api.account_type == "credit" and not self.credit_markets:
            self.status["state"] = "credit_market_permissions_required"
        else:
            self.status["state"] = "live_monitoring"
            if active:
                self.status["entry_blocker"] = "external_orders_present"
            await self.strategy(http)
        self.status["last_cycle"] = time.time()
