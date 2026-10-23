"""Autonomous learned policy: model forecasts affect selection and cost-based entry."""
from __future__ import annotations

from dataclasses import dataclass

from .core import Candle, Settings, digest
from .model import FEATURE_NAMES, RidgeModel, features


@dataclass(frozen=True)
class Candidate:
    symbol: str
    decision_at: int
    forecast: float
    feature_values: tuple[float, ...]
    input_hash: str

    def record(self) -> dict:
        return {"symbol": self.symbol, "decision_at": self.decision_at, "forecast_gross_return": self.forecast,
                "features": dict(zip(FEATURE_NAMES, self.feature_values)), "input_hash": self.input_hash}


class LearnedAgent:
    def __init__(self, model: RidgeModel, settings: Settings):
        if model.settings_hash != settings.id:
            raise ValueError("Model/configuration mismatch; rerun research for this configuration")
        self.model, self.settings = model, settings

    def analyze(self, symbol: str, candles: list[Candle]) -> Candidate | None:
        if not candles:
            return None
        x = features(candles, len(candles)-1, self.settings)
        if x is None:
            return None
        decision_at = candles[-1].timestamp+self.settings.interval
        prediction = self.model.predict(x, decision_at)
        return Candidate(symbol, decision_at, prediction, x, digest([c.record() for c in candles]))

    def rank(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[dict]]:
        accepted, rejected = [], []
        for c in candidates:
            if c.feature_values[1] <= 0 or c.feature_values[4] <= 0:
                rejected.append({**c.record(), "reason": "momentum_filter"})
            else:
                accepted.append(c)
        return sorted(accepted, key=lambda c: (-c.forecast, c.symbol)), rejected
