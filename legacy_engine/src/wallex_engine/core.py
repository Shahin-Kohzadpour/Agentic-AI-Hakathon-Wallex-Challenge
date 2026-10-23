from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from importlib.resources import files
from functools import lru_cache
from pathlib import Path


def dec(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite monetary value")
    return number


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pair(symbol: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+(?:TMN|USDT)", symbol):
        raise ValueError("Expected an uppercase Wallex symbol such as BTCTMN")
    quote = "USDT" if symbol.endswith("USDT") else "TMN"
    base = symbol[:-len(quote)]
    if not base:
        raise ValueError("Empty base asset")
    return f"{base}/{quote}"


@lru_cache(maxsize=1)
def exclusions() -> frozenset[str]:
    raw = json.loads(files("wallex_engine").joinpath("excluded-markets.json").read_text())
    values = raw["excluded_pairs"]
    if raw["count"] != 345 or len(values) != 345 or len(set(values)) != 345:
        raise ValueError("Unexpected challenge exclusion manifest")
    return frozenset(values)


@dataclass(frozen=True)
class Candle:
    timestamp: int  # OPEN timestamp; a candle is usable only after timestamp + interval.
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        if type(self.timestamp) is not int or self.timestamp < 0:
            raise ValueError("Invalid candle timestamp")
        prices = (self.open, self.high, self.low, self.close)
        if any(not x.is_finite() or x <= 0 for x in prices):
            raise ValueError("Candle prices must be positive and finite")
        if not self.volume.is_finite() or self.volume < 0:
            raise ValueError("Invalid volume")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid OHLC range")

    def record(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_record(cls, value: dict) -> Candle:
        return cls(int(value["timestamp"]), *(dec(value[k]) for k in ("open", "high", "low", "close", "volume")))


@dataclass(frozen=True)
class Book:
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    received_at: float

    def __post_init__(self):
        pair(self.symbol)
        if not math.isfinite(self.received_at) or not self.bids or not self.asks:
            raise ValueError("Empty or invalid book")
        if any(p <= 0 or q <= 0 or not p.is_finite() or not q.is_finite()
               for p, q in self.bids + self.asks):
            raise ValueError("Invalid book level")
        if self.bids[0][0] >= self.asks[0][0]:
            raise ValueError("Crossed or locked book")
        if list(self.bids) != sorted(self.bids, reverse=True) or list(self.asks) != sorted(self.asks):
            raise ValueError("Unsorted book")

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.asks[0][0] - self.bids[0][0]) / self.mid * 10000

    def execution_price(self, side: str, quantity: Decimal) -> Decimal:
        if side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError("Invalid execution request")
        left, cost = quantity, Decimal(0)
        for price, available in self.asks if side == "BUY" else self.bids:
            taken = min(left, available)
            cost += taken * price
            left -= taken
            if left == 0:
                return cost / quantity
        raise ValueError("Insufficient visible depth")


@dataclass(frozen=True)
class Settings:
    initial_cash_tmn: Decimal = Decimal("5000000")
    entry_fee: Decimal = Decimal("0.003")
    exit_fee: Decimal = Decimal("0.003")
    slippage_bps: Decimal = Decimal("10")
    extra_edge_bps: Decimal = Decimal("20")
    resolution_minutes: int = 15
    horizon_bars: int = 4
    feature_window: int = 32
    ridge_penalty: float = 0.1
    max_position_fraction: Decimal = Decimal("0.10")
    max_total_fraction: Decimal = Decimal("0.40")
    risk_per_trade_fraction: Decimal = Decimal("0.00125")
    stop_fraction: Decimal = Decimal("0.02")
    take_profit_fraction: Decimal = Decimal("0.04")
    max_daily_loss_fraction: Decimal = Decimal("0.02")
    max_drawdown_fraction: Decimal = Decimal("0.04")
    max_quote_age_seconds: int = 10
    max_spread_bps: Decimal = Decimal("50")
    max_depth_fraction: Decimal = Decimal("0.05")
    max_candle_age_seconds: int = 120
    min_notional_tmn: Decimal = Decimal("50000")
    symbols: tuple[str, ...] = ("BTCTMN", "ETHTMN")
    deadline: str | None = None

    def __post_init__(self):
        if self.initial_cash_tmn <= 0 or self.min_notional_tmn <= 0:
            raise ValueError("Cash and minimum notional must be positive")
        fractions = (self.entry_fee, self.exit_fee, self.max_position_fraction, self.max_total_fraction,
                     self.risk_per_trade_fraction, self.stop_fraction, self.take_profit_fraction,
                     self.max_daily_loss_fraction, self.max_drawdown_fraction, self.max_depth_fraction)
        if any(not x.is_finite() or not 0 <= x < 1 for x in fractions):
            raise ValueError("Invalid fraction")
        if any(x <= 0 for x in fractions[2:]):
            raise ValueError("Risk limits must be positive")
        if self.max_position_fraction > self.max_total_fraction:
            raise ValueError("Position limit exceeds total limit")
        if self.resolution_minutes not in {1, 15, 60, 240, 480, 720}:
            raise ValueError("Unsupported candle resolution")
        if self.feature_window < 32 or self.horizon_bars < 1:
            raise ValueError("Invalid feature window or horizon")
        if not math.isfinite(self.ridge_penalty) or self.ridge_penalty <= 0:
            raise ValueError("Ridge penalty must be positive and finite")
        for value in (self.slippage_bps, self.extra_edge_bps, self.max_spread_bps):
            if not value.is_finite() or not 0 <= value < 10000:
                raise ValueError("Invalid basis-point setting")
        if min(self.max_quote_age_seconds, self.max_candle_age_seconds) <= 0:
            raise ValueError("Freshness limits must be positive")
        denied = exclusions()
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Universe must be nonempty and unique")
        for symbol in self.symbols:
            if pair(symbol) in denied or not symbol.endswith("TMN"):
                raise ValueError(f"Ineligible research symbol: {symbol}")
        if self.deadline is not None:
            parsed = datetime.fromisoformat(self.deadline)
            if parsed.tzinfo is None:
                raise ValueError("Deadline must include timezone")

    @property
    def interval(self) -> int:
        return self.resolution_minutes * 60

    @property
    def id(self) -> str:
        return digest(asdict(self))

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        data = tomllib.loads(Path(path).read_text())
        defaults = cls()
        for name in data:
            if name not in cls.__dataclass_fields__:
                raise ValueError(f"Unknown configuration setting: {name}")
            if isinstance(getattr(defaults, name), Decimal):
                data[name] = dec(data[name])
        if "symbols" in data:
            data["symbols"] = tuple(data["symbols"])
        return cls(**data)


@dataclass
class Position:
    quantity: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)


@dataclass
class Portfolio:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    realized: Decimal = Decimal(0)
    fees_tmn: Decimal = Decimal(0)

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + sum((p.quantity * marks[s] for s, p in self.positions.items() if p.quantity), Decimal(0))

    def apply_fill(self, symbol: str, side: str, quantity: Decimal, price: Decimal,
                   fee: Decimal, fee_asset: str) -> None:
        base, quote = pair(symbol).split("/")
        if quote != "TMN" or side not in {"BUY", "SELL"}:
            raise ValueError("Only TMN spot fills are supported")
        if quantity <= 0 or price <= 0 or fee < 0 or fee_asset not in {base, quote}:
            raise ValueError("Invalid fill or fee asset")
        if any(not x.is_finite() for x in (quantity, price, fee)):
            raise ValueError("Non-finite fill")
        p = self.positions.get(symbol, Position())
        if side == "BUY":
            spend = quantity * price + (fee if fee_asset == quote else 0)
            received = quantity - (fee if fee_asset == base else 0)
            if spend > self.cash or received <= 0:
                raise ValueError("Insufficient cash or excessive base fee")
            self.cash -= spend
            self.positions[symbol] = Position(p.quantity + received, p.cost + spend)
        else:
            removed = quantity + (fee if fee_asset == base else 0)
            proceeds = quantity * price - (fee if fee_asset == quote else 0)
            if removed > p.quantity or proceeds < 0:
                raise ValueError("Insufficient inventory or excessive quote fee")
            basis = p.cost * removed / p.quantity
            self.cash += proceeds
            self.realized += proceeds - basis
            self.positions[symbol] = Position(p.quantity - removed, p.cost - basis)
        self.fees_tmn += fee if fee_asset == quote else fee * price

    def record(self) -> dict:
        return {"cash": str(self.cash), "realized": str(self.realized), "fees_tmn": str(self.fees_tmn),
                "positions": {s: {"quantity": str(p.quantity), "cost": str(p.cost)} for s, p in self.positions.items()}}

    @classmethod
    def from_record(cls, value: dict) -> Portfolio:
        return cls(dec(value["cash"]), {s: Position(dec(p["quantity"]), dec(p["cost"]))
                   for s, p in value["positions"].items()}, dec(value["realized"]), dec(value["fees_tmn"]))


def round_quantity(quantity: Decimal, precision: int) -> Decimal:
    if type(precision) is not int or not 0 <= precision <= 18:
        raise ValueError("Unverified quantity precision")
    return quantity.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
