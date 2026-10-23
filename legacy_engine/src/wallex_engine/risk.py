from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .core import Book, Portfolio, Settings, dec, exclusions, pair, round_quantity


@dataclass
class RiskState:
    peak: Decimal
    day_start: Decimal
    day: str = ""
    drawdown_halted: bool = False
    day_halted: bool = False

    def observe(self, equity: Decimal, timestamp: int, settings: Settings):
        # Explicit UTC risk day, independent of host timezone. Event deadline retains its own offset.
        day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.day_start, self.day_halted = day, equity, False
        self.peak = max(self.peak, equity)
        if equity <= self.peak*(1-settings.max_drawdown_fraction):
            self.drawdown_halted = True
        if equity <= self.day_start*(1-settings.max_daily_loss_fraction):
            self.day_halted = True

    @property
    def halted(self) -> bool:
        return self.drawdown_halted or self.day_halted

    def record(self):
        return asdict(self)

    @classmethod
    def from_record(cls, value: dict) -> RiskState:
        return cls(dec(value["peak"]), dec(value["day_start"]), value["day"],
                   value["drawdown_halted"], value["day_halted"])


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: Decimal = Decimal(0)
    expected_net_return: Decimal = Decimal(0)


def entry(symbol: str, forecast: float, price: Decimal, portfolio: Portfolio,
          marks: dict[str, Decimal], state: RiskState, settings: Settings,
          timestamp: int, precision: int = 8, book: Book | None = None,
          unresolved_orders: bool = False, enforce_edge: bool = True) -> RiskDecision:
    def deny(reason):
        return RiskDecision(False, reason)
    if symbol not in settings.symbols or pair(symbol) in exclusions():
        return deny("market_not_eligible")
    if unresolved_orders:
        return deny("unresolved_orders")
    if state.halted:
        return deny("equity_loss_limit")
    # Initial policy intentionally permits only one concurrent position.
    if any(p.quantity for p in portfolio.positions.values()):
        return deny("initial_single_position_policy")
    if settings.deadline:
        deadline = datetime.fromisoformat(settings.deadline).timestamp()
        if timestamp >= deadline-settings.horizon_bars*settings.interval-3600:
            return deny("deadline_entry_cutoff")
    if not math.isfinite(forecast) or price <= 0 or not price.is_finite():
        return deny("invalid_score_or_price")
    spread = Decimal(0)
    if book:
        if book.symbol != symbol:
            return deny("book_symbol_mismatch")
        if not 0 <= timestamp-book.received_at <= settings.max_quote_age_seconds:
            return deny("stale_quote")
        if book.spread_bps > settings.max_spread_bps:
            return deny("spread_too_wide")
        spread = book.spread_bps/10000
    net = dec(forecast) - settings.entry_fee - settings.exit_fee - 2*settings.slippage_bps/10000 - spread
    if enforce_edge and net <= settings.extra_edge_bps/10000:
        return RiskDecision(False, "edge_below_cost_hurdle", expected_net_return=net)
    equity = portfolio.equity(marks)
    if equity <= 0:
        return deny("nonpositive_equity")
    exposure = equity-portfolio.cash
    risk_distance = settings.stop_fraction+settings.entry_fee+settings.exit_fee+2*settings.slippage_bps/10000+spread
    notional = min(equity*settings.max_position_fraction, equity*settings.max_total_fraction-exposure,
                   equity*settings.risk_per_trade_fraction/risk_distance,
                   portfolio.cash/(1+settings.entry_fee))
    if book:
        visible = sum((p*q for p, q in book.asks), Decimal(0))
        notional = min(notional, visible*settings.max_depth_fraction)
    quantity = round_quantity(max(Decimal(0), notional)/price, precision)
    if quantity*price < settings.min_notional_tmn:
        return deny("below_minimum_notional")
    return RiskDecision(True, "learned_edge_after_costs" if enforce_edge else "momentum_baseline", quantity, net)
