from datetime import datetime
from decimal import Decimal, ROUND_DOWN

CUTOFF = datetime.fromisoformat("2026-10-26T23:50:00+03:30")
CAPITAL_TMN = Decimal("5000000")
CAPITAL_IRR = CAPITAL_TMN * 10


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non_finite_amount")
    return number


def floor(value, precision):
    if type(precision) is not int or not 0 <= precision <= 18:
        raise ValueError("invalid_exchange_precision")
    return decimal(value).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def entry_allowed(now, holding_seconds=7200):
    # Reserve five minutes to close before the final trading cutoff.
    return now + holding_seconds + 300 < CUTOFF.timestamp()


def validate_buy(symbol, quantity, price, fee, tmn_per_usdt, now, quote_at):
    if not entry_allowed(now):
        raise ValueError("new_entries_closed")
    if symbol not in {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("entry_market_not_allowed")
    if not 0 <= now - quote_at <= 8:
        raise ValueError("stale_usdt_tmn_quote")
    quantity, price, fee, rate = map(decimal, (quantity, price, fee, tmn_per_usdt))
    if min(quantity, price, rate) <= 0 or not 0 <= fee <= Decimal(".02"):
        raise ValueError("invalid_order_amount")
    # A single position may consume at most 10% of the approved real capital.
    if quantity * price * (1 + fee) * rate > CAPITAL_TMN / 10:
        raise ValueError("real_capital_limit")
