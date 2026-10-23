"""Small trainable ridge forecaster; fitting and scaling use training observations only."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import fmean, pstdev

from .core import Candle, Settings, digest

FEATURE_VERSION = "closed-candle-v1"
FEATURE_NAMES = ("return_1", "return_4", "return_16", "return_32", "trend_16", "volatility_16",
                 "volume_surprise_16", "range_fraction", "utc_hour_sin", "utc_hour_cos")


def features(candles: list[Candle], index: int, settings: Settings) -> tuple[float, ...] | None:
    if index < settings.feature_window:
        return None
    window = candles[index-settings.feature_window:index+1]
    if any(b.timestamp-a.timestamp != settings.interval for a, b in zip(window, window[1:])):
        return None
    c = candles[index]
    closes = [float(row.close) for row in candles[index-16:index+1]]
    returns = [math.log(b/a) for a, b in zip(closes, closes[1:])]
    volumes = [float(row.volume) for row in candles[index-16:index]]
    mean_volume = fmean(volumes)
    hour = ((c.timestamp + settings.interval) % 86400) / 86400 * 2 * math.pi
    output = tuple(float(c.close/candles[index-k].close-1) for k in (1, 4, 16, 32)) + (
        float(c.close)/fmean(closes)-1, pstdev(returns),
        min(float(c.volume)/mean_volume-1, 10) if mean_volume else 0,
        float((c.high-c.low)/c.close), math.sin(hour), math.cos(hour))
    if not all(math.isfinite(x) for x in output):
        raise ValueError("Non-finite model features")
    return output


@dataclass(frozen=True)
class Sample:
    symbol: str
    decision_at: int
    label_available_at: int
    x: tuple[float, ...]
    y: float


def samples(symbol: str, candles: list[Candle], settings: Settings) -> list[Sample]:
    output = []
    h = settings.horizon_bars
    for i in range(settings.feature_window, len(candles)-h):
        x = features(candles, i, settings)
        future = candles[i:i+h+1]
        if x is None or any(b.timestamp-a.timestamp != settings.interval for a, b in zip(future, future[1:])):
            continue
        y = float(candles[i+h].close / candles[i+1].open - 1)
        if not math.isfinite(y):
            raise ValueError("Non-finite training label")
        output.append(Sample(symbol, candles[i].timestamp+settings.interval,
                             candles[i+h].timestamp+settings.interval, x, y))
    return output


def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Pivoted elimination on the small regularized normal equations."""
    size = len(vector)
    rows = [row[:] + [y] for row, y in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(rows[row][col]))
        rows[col], rows[pivot] = rows[pivot], rows[col]
        if abs(rows[col][col]) < 1e-14:
            raise ValueError("Singular model fit")
        scale = rows[col][col]
        rows[col] = [v/scale for v in rows[col]]
        for row in range(size):
            if row == col:
                continue
            scale = rows[row][col]
            rows[row] = [a-scale*b for a, b in zip(rows[row], rows[col])]
    return [row[-1] for row in rows]


@dataclass(frozen=True)
class RidgeModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    trained_until: int
    training_count: int
    training_hash: str
    settings_hash: str
    residual_std: float
    feature_version: str = FEATURE_VERSION

    def __post_init__(self):
        n = len(FEATURE_NAMES)
        if len(self.means) != n or len(self.scales) != n or len(self.coefficients) != n+1:
            raise ValueError("Model feature dimensions differ")
        if self.feature_version != FEATURE_VERSION or any(x <= 0 for x in self.scales):
            raise ValueError("Unsupported model feature version or scales")
        if not all(math.isfinite(x) for x in (*self.means, *self.scales, *self.coefficients, self.residual_std)):
            raise ValueError("Non-finite model artifact")

    @property
    def id(self) -> str:
        return digest(asdict(self))

    def predict(self, x: tuple[float, ...], decision_at: int) -> float:
        if decision_at < self.trained_until:
            raise ValueError("Model uses training labels unavailable at decision time")
        if len(x) != len(self.means) or not all(math.isfinite(v) for v in x):
            raise ValueError("Invalid feature vector")
        return self.coefficients[0] + sum(w*(v-m)/s for w, v, m, s in
                                         zip(self.coefficients[1:], x, self.means, self.scales))

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "model_id": self.id, **asdict(self)}, indent=2, allow_nan=False)+"\n")

    @classmethod
    def load(cls, path: str | Path) -> RidgeModel:
        value = json.loads(Path(path).read_text())
        if value.pop("schema_version") != 1:
            raise ValueError("Unsupported model schema")
        expected = value.pop("model_id")
        for key in ("means", "scales", "coefficients"):
            value[key] = tuple(value[key])
        model = cls(**value)
        if expected != model.id:
            raise ValueError("Model artifact checksum mismatch")
        return model


def fit(rows: list[Sample], settings: Settings) -> RidgeModel:
    if len(rows) < 100:
        raise ValueError("At least 100 chronological training samples are required")
    n = len(FEATURE_NAMES)
    means = tuple(fmean(row.x[j] for row in rows) for j in range(n))
    scales = tuple(max(pstdev(row.x[j] for row in rows), 1e-8) for j in range(n))
    matrix = [[0.0]*(n+1) for _ in range(n+1)]
    vector = [0.0]*(n+1)
    for row in rows:
        x = (1.0,) + tuple((v-m)/s for v, m, s in zip(row.x, means, scales))
        for j in range(n+1):
            vector[j] += x[j]*row.y/len(rows)
            for k in range(n+1):
                matrix[j][k] += x[j]*x[k]/len(rows)
    for j in range(1, n+1):
        matrix[j][j] += settings.ridge_penalty
    coefficients = tuple(solve(matrix, vector))
    trained_until = max(row.label_available_at for row in rows)
    interim = RidgeModel(means, scales, coefficients, trained_until, len(rows), digest([asdict(row) for row in rows]), settings.id, 0.0)
    # Training residuals are diagnostics, not a calibrated confidence interval.
    residual = math.sqrt(fmean((row.y-interim.predict(row.x, trained_until))**2 for row in rows))
    return RidgeModel(means, scales, coefficients, trained_until, len(rows), interim.training_hash, settings.id, residual)
