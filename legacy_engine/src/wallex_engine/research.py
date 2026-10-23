from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .core import Candle, Portfolio, Settings, dec, digest, now_iso
from .model import RidgeModel, features, fit, samples
from .risk import RiskState, entry


def simulate(symbol: str, candles: list[Candle], settings: Settings, model: RidgeModel,
             start: int, end: int, *, baseline: bool = False, cost_multiplier: int = 1) -> dict:
    """Independent single-market test. Every entry executes after its feature candle closes."""
    portfolio = Portfolio(settings.initial_cash_tmn)
    state = RiskState(portfolio.cash, portfolio.cash)
    decisions, trades, equity_curve = [], [], []
    reasons = Counter()
    held = None
    fee_in, fee_out = settings.entry_fee*cost_multiplier, settings.exit_fee*cost_multiplier
    slip = settings.slippage_bps/10000*cost_multiplier
    assessed = Settings(**{**asdict(settings), "entry_fee": fee_in, "exit_fee": fee_out,
                           "slippage_bps": settings.slippage_bps*cost_multiplier})
    maximum_drawdown = Decimal(0)
    last_index = max((i for i, c in enumerate(candles) if start <= c.timestamp < end), default=-1)
    if last_index < 0:
        raise ValueError("No candles in evaluation interval")
    for i, candle in enumerate(candles):
        if not start <= candle.timestamp < end:
            continue
        open_equity = portfolio.equity({symbol: candle.open})
        state.observe(open_equity, candle.timestamp, settings)
        # An entry only depends on the previous COMPLETE contiguous feature window.
        if not held and i > 0 and candles[i-1].timestamp+settings.interval == candle.timestamp:
            x = features(candles, i-1, settings)
            if x is not None:
                forecast = model.predict(x, candle.timestamp)
                momentum = x[1] > 0 and x[4] > 0
                # Stress runs raise execution costs and the expected-return hurdle together.
                decision = entry(symbol, forecast, candle.open*(1+slip), portfolio,
                                 {symbol: candle.open}, state, assessed, candle.timestamp, enforce_edge=not baseline)
                reason = decision.reason if momentum else "momentum_filter"
                allowed = decision.allowed and momentum
                reasons[reason] += 1
                decisions.append({"decision_at": candle.timestamp, "symbol": symbol, "features": list(x),
                                  "model_id": model.id, "forecast": forecast, "allowed": allowed,
                                  "reason": reason, "quantity": str(decision.quantity) if allowed else "0"})
                if allowed:
                    price, qty = candle.open*(1+slip), decision.quantity
                    before = portfolio.realized
                    portfolio.apply_fill(symbol, "BUY", qty, price, qty*price*fee_in, "TMN")
                    held = {"entry_at": candle.timestamp, "entry_index": i, "entry_price": price,
                            "quantity": qty, "realized_before": before, "forecast": forecast}
        if held:
            # Mark-to-market adverse intrabar excursion; candle path/order is unknown.
            worst = portfolio.equity({symbol: candle.low})
            maximum_drawdown = max(maximum_drawdown, (state.peak-worst)/state.peak)
            stop = held["entry_price"]*(1-settings.stop_fraction)
            target = held["entry_price"]*(1+settings.take_profit_fraction)
            price, reason = None, None
            deadline_exit = (settings.deadline is not None and candle.timestamp >=
                             datetime.fromisoformat(settings.deadline).timestamp()-3600)
            if state.halted or deadline_exit:
                price, reason = candle.open*(1-slip), "risk_exit" if state.halted else "deadline_exit"
            elif candle.low <= stop:
                # If target and stop both touch, conservatively choose stop; gap-through exits at open.
                price, reason = min(candle.open, stop)*(1-slip), "stop"
            elif candle.high >= target:
                price, reason = target*(1-slip), "target"
            elif i-held["entry_index"]+1 >= settings.horizon_bars:
                price, reason = candle.close*(1-slip), "time_exit"
            elif i == last_index:
                price, reason = candle.close*(1-slip), "evaluation_end"
            if price is not None:
                qty = portfolio.positions[symbol].quantity
                portfolio.apply_fill(symbol, "SELL", qty, price, qty*price*fee_out, "TMN")
                trades.append({"entry_at": held["entry_at"], "exit_bar_at": candle.timestamp,
                               "entry_price": str(held["entry_price"]), "exit_price": str(price),
                               "quantity": str(qty), "reason": reason,
                               "net_pnl_tmn": str(portfolio.realized-held["realized_before"])})
                held = None
        equity = portfolio.equity({symbol: candle.close})
        state.observe(equity, candle.timestamp+settings.interval, settings)
        maximum_drawdown = max(maximum_drawdown, (state.peak-equity)/state.peak)
        equity_curve.append({"timestamp": candle.timestamp+settings.interval, "equity_tmn": str(equity)})
    if held:
        raise RuntimeError("Evaluation failed to close final position")
    return {"symbol": symbol, "policy": "momentum_without_learned_gate" if baseline else "learned_momentum",
            "mode": "backtest", "cost_multiplier": cost_multiplier, "start": start, "end": end,
            "initial_cash_tmn": str(settings.initial_cash_tmn), "final_cash_tmn": str(portfolio.cash),
            "net_pnl_tmn": str(portfolio.realized), "return_pct": str((portfolio.cash/settings.initial_cash_tmn-1)*100),
            "fees_tmn": str(portfolio.fees_tmn), "max_drawdown_pct": str(maximum_drawdown*100),
            "trade_count": len(trades), "decision_reasons": dict(reasons), "trades": trades,
            "decisions": decisions, "equity_curve": equity_curve}


def experiment(histories: dict[str, list[Candle]], settings: Settings, data_hashes: dict[str, str]) -> tuple[dict, RidgeModel]:
    if not histories or any(s not in settings.symbols for s in histories):
        raise ValueError("Dataset symbols are outside configured universe")
    all_samples = sorted((row for symbol, candles in histories.items() for row in samples(symbol, candles, settings)),
                         key=lambda row: (row.decision_at, row.symbol))
    times = sorted({row.decision_at for row in all_samples})
    if len(times) < 250:
        raise ValueError("Need at least 250 eligible decision timestamps for walk-forward evaluation")
    cutoffs = [times[int(len(times)*q)] for q in (0.50, 0.65, 0.80)]
    # Evaluation spans the actual history, not merely observations with forward labels.
    final_end = min(rows[-1].timestamp+settings.interval for rows in histories.values())
    boundaries = cutoffs + [final_end]
    folds = []
    for number, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        train = [row for row in all_samples if row.decision_at < start and row.label_available_at <= start]
        model = fit(train, settings)
        tests = []
        for symbol, candles in histories.items():
            for baseline, stress in ((False, 1), (True, 1), (False, 2)):
                tests.append(simulate(symbol, candles, settings, model, start, end,
                                      baseline=baseline, cost_multiplier=stress))
        folds.append({"fold": number, "role": "final_holdout" if number == 3 else "walk_forward_validation",
                      "training_count": len(train), "training_labels_available_by": model.trained_until,
                      "test_start": start, "test_end": end, "model_id": model.id, "results": tests})
    final_model = fit(all_samples, settings)
    report = {"schema_version": 1, "created_at": now_iso(), "mode": "backtest", "settings": asdict(settings),
              "settings_hash": settings.id, "dataset_hashes": data_hashes, "feature_version": final_model.feature_version,
              "experiment_id": digest({"settings": settings.id, "data": data_hashes}),
              "evaluation_scope": "Each market/fold is a separate account with the same initial capital; returns MUST NOT be summed.",
              "execution_assumptions": ["next-bar open plus configured slippage for entries",
                                        "stop before target when both touch; gap-through exits at adverse open",
                                        "fee and slippage values are assumptions, not verified account costs",
                                        "no historical book, queue, financing or volume-capacity model",
                                        "maximum drawdown includes conservative held-position intrabar lows"],
              "profitability_gate": "NOT_ESTABLISHED: verify actual costs, independent-trade evidence, account eligibility, and full-stack shadow behavior",
              "cash_benchmark_return_pct": "0", "folds": folds, "final_model_id": final_model.id,
              "final_model_note": "Refit on all historical labels for FUTURE shadow use; never used in earlier test folds."}
    return report, final_model
