from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from .agent import LearnedAgent
from .core import Book, Settings, dec, digest, now_iso, round_quantity
from .exchange import ExchangeError, WallexPublic
from .ledger import Intent, Store
from .model import RidgeModel
from .risk import RiskState, entry


class ShadowRunner:
    """Live public data, simulated fills, durable paper account. No exchange credentials."""

    def __init__(self, client: WallexPublic, settings: Settings, model: RidgeModel, store: Store):
        self.client, self.settings, self.store = client, settings, store
        self.agent = LearnedAgent(model, settings)
        store.initialize(settings.initial_cash_tmn, digest({"settings": settings.id, "model": model.id, "mode": "shadow"}))
        store.event("session", {"mode": "shadow", "settings": asdict(settings), "settings_hash": settings.id,
                                "model_id": model.id, "agent_type": "local_learned_policy",
                                "account_permissions_verified": False})
        # Only paper orders exist here. An interrupted unfilled simulation is canceled on recovery.
        for order in store.open_orders():
            if order["status"] in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
                store.transition(order["client_id"], "CANCEL_REQUESTED")
            store.transition(order["client_id"], "CANCELED")
        self.history_cache = {}
        self.market_cache, self.market_cached_at = {}, 0.0

    def _metadata(self, now: float) -> dict:
        if now-self.market_cached_at >= 300:
            self.market_cache = {r["symbol"]: r for r in self.client.markets()}
            self.market_cached_at = now
        return self.market_cache

    def _execute(self, symbol: str, side: str, quantity: Decimal, book: Book,
                 timestamp: int, identity: str, state_updates: dict | None = None) -> bool:
        slip = self.settings.slippage_bps/10000
        price = book.execution_price(side, quantity)*(1+slip if side == "BUY" else 1-slip)
        rate = self.settings.entry_fee if side == "BUY" else self.settings.exit_fee
        client_id = "P_"+digest({"identity": identity, "symbol": symbol, "side": side, "at": timestamp})[:28].upper()
        intent = Intent(client_id, symbol, side, quantity, price, rate)
        if not self.store.prepare(intent):
            return False
        self.store.transition(client_id, "OPEN")
        self.store.fill("F_"+client_id, client_id, quantity, price, quantity*price*rate, "TMN", state_updates=state_updates)
        return True

    def poll(self) -> dict:
        settings, store = self.settings, self.store
        poll_started = time.time()
        metadata = self._metadata(poll_started)
        previous = store.get("marks", {})
        marks = {s: dec(v) for s, v in previous.items()}
        books, candidates, failures = {}, [], []
        processed = store.get("processed", {})
        expected_end = int(poll_started)//settings.interval*settings.interval
        for symbol in settings.symbols:
            try:
                if symbol not in metadata or metadata[symbol].get("is_spot") is not True:
                    raise ValueError("Configured symbol is not currently public-spot active")
                precision = metadata[symbol].get("amount_precision")
                if type(precision) is not int or not 0 <= precision <= 18:
                    raise ValueError("Missing verified public quantity precision")
                book = self.client.book(symbol)
                books[symbol], marks[symbol] = book, book.mid
                store.event("book_snapshot", {"symbol": symbol, "received_at": book.received_at,
                                              "bid": [[str(p), str(q)] for p, q in book.bids],
                                              "ask": [[str(p), str(q)] for p, q in book.asks],
                                              "freshness_basis": "HTTP receipt time; exchange event age unavailable"})
                cached = self.history_cache.get(symbol)
                if cached is None or cached[0] != expected_end:
                    candles = self.client.candles(symbol, expected_end-(settings.feature_window+3)*settings.interval,
                                                  expected_end, settings.resolution_minutes)
                    self.history_cache[symbol] = (expected_end, candles)
                    store.event("candle_window", {"symbol": symbol, "candles": [c.record() for c in candles]})
                candles = self.history_cache[symbol][1]
                if not candles:
                    raise ValueError("No closed candles returned")
                closed_at = candles[-1].timestamp+settings.interval
                if time.time()-closed_at > settings.max_candle_age_seconds:
                    # Exits still run from fresh books even when a new entry's forecast is too old.
                    continue
                if processed.get(symbol, 0) >= closed_at:
                    continue
                candidate = self.agent.analyze(symbol, candles)
                if candidate:
                    candidates.append(candidate)
            except (ExchangeError, ValueError, KeyError, TypeError) as exc:
                failure = {"symbol": symbol, "error": str(exc), "at": now_iso()}
                failures.append(failure)
                store.event("data_error", failure)
        portfolio = store.portfolio
        held_symbols = [s for s, p in portfolio.positions.items() if p.quantity]
        if any(s not in marks for s in held_symbols):
            raise ValueError("Cannot value recovered inventory without a recorded or current mark")
        observed_at = int(time.time())
        equity = portfolio.equity(marks)
        stored_risk = store.get("risk")
        state = RiskState.from_record(stored_risk) if stored_risk else RiskState(settings.initial_cash_tmn, settings.initial_cash_tmn)
        state.observe(equity, observed_at, settings)
        store.put("risk", state.record())
        store.put("marks", {s: str(v) for s, v in marks.items()})
        holding = store.get("holding", {})
        exited = False
        for symbol in held_symbols:
            if symbol not in books:
                store.event("exit_blocked", {"symbol": symbol, "reason": "fresh_book_unavailable"})
                continue
            book = books[symbol]
            if time.time()-book.received_at > settings.max_quote_age_seconds:
                store.event("exit_blocked", {"symbol": symbol, "reason": "stale_quote"})
                continue
            p = portfolio.positions[symbol]
            info = holding.get(symbol)
            if info is None:
                raise ValueError("Recovered paper position lacks atomic entry metadata")
            reference = dec(info["entry_reference_price"])
            bid = book.bids[0][0]
            reason = None
            if state.halted:
                reason = "equity_loss_limit"
            elif settings.deadline and observed_at >= datetime.fromisoformat(settings.deadline).timestamp()-3600:
                reason = "deadline_exit"
            elif bid <= reference*(1-settings.stop_fraction):
                reason = "stop"
            elif bid >= reference*(1+settings.take_profit_fraction):
                reason = "target"
            elif observed_at >= info["opened_at"]+settings.horizon_bars*settings.interval:
                reason = "time_exit"
            if reason:
                # Limit each simulated exit to a fraction of visible bid quantity; retain residual state.
                capacity = sum((q for _, q in book.bids), Decimal(0))*settings.max_depth_fraction
                quantity = round_quantity(min(p.quantity, capacity), metadata[symbol]["amount_precision"])
                if quantity <= 0:
                    store.event("exit_blocked", {"symbol": symbol, "reason": "insufficient_visible_depth_or_dust"})
                    continue
                after = dict(holding)
                if quantity == p.quantity:
                    after.pop(symbol, None)
                store.event("exit_decision", {"symbol": symbol, "reason": reason, "quantity": str(quantity),
                                               "simulated": True, "at": observed_at})
                if self._execute(symbol, "SELL", quantity, book, observed_at, reason, {"holding": after}):
                    exited = True
                    holding = after
                    portfolio = store.portfolio
        ranked, rejected = self.agent.rank(candidates)
        outcomes = []
        for rejected_candidate in rejected:
            processed[rejected_candidate["symbol"]] = rejected_candidate["decision_at"]
            store.event("agent_decision", {**rejected_candidate, "action": "ABSTAIN", "model_id": self.agent.model.id},
                        {"processed": processed})
        for candidate in ranked:
            symbol = candidate.symbol
            book = books[symbol]
            current_time = int(time.time())
            forecast = candidate.forecast
            quote = book.asks[0][0]*(1+settings.slippage_bps/10000)
            decision = entry(symbol, forecast, quote, portfolio, marks, state, settings,
                             current_time, metadata[symbol]["amount_precision"], book,
                             unresolved_orders=bool(store.open_orders()) or exited or bool(failures))
            qty = decision.quantity
            if decision.allowed:
                # Refine size using depth-weighted simulated entry price, not just the best ask.
                for _ in range(3):
                    quote = book.execution_price("BUY", qty)*(1+settings.slippage_bps/10000)
                    decision = entry(symbol, forecast, quote, portfolio, marks, state, settings,
                                     int(time.time()), metadata[symbol]["amount_precision"], book)
                    if not decision.allowed:
                        break
                    qty = min(qty, decision.quantity)
            processed[symbol] = candidate.decision_at
            outcome = {**candidate.record(), "action": "BUY" if decision.allowed else "ABSTAIN",
                       "risk": asdict(decision), "quantity": str(qty if decision.allowed else 0),
                       "model_id": self.agent.model.id, "settings_hash": settings.id,
                       "simulated": True, "account_permissions_verified": False}
            store.event("agent_decision", outcome, {"processed": processed})
            outcomes.append(outcome)
            if decision.allowed:
                info = {"opened_at": int(time.time()), "entry_reference_price": str(quote),
                        "feature_closed_at": candidate.decision_at}
                self._execute(symbol, "BUY", qty, book, info["opened_at"], candidate.input_hash,
                              {"holding": {symbol: info}})
                portfolio = store.portfolio
        equity = portfolio.equity(marks)
        state.observe(equity, int(time.time()), settings)
        snapshot = {"at": now_iso(), "mode": "shadow", "simulated": True, "equity_tmn": str(equity),
                    "cash_tmn": str(portfolio.cash), "realized_pnl_tmn": str(portfolio.realized),
                    "fees_tmn": str(portfolio.fees_tmn), "positions": portfolio.record()["positions"],
                    "equity_peak_tmn": str(state.peak), "risk_halted": state.halted,
                    "stale_marks": [s for s in held_symbols if s not in books],
                    "decisions_this_poll": len(outcomes)+len(rejected), "data_errors": failures}
        store.event("portfolio_snapshot", snapshot, {"risk": state.record(), "latest": snapshot})
        return snapshot
