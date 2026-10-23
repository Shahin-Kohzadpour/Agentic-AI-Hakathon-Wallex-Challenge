"""Conservative, chronologically validated entry policy for the paper runner.

This is an evidence gate, not a claim that a ridge model has a trading edge.
Historical validation uses candle returns and today's cost hurdle, not historical
order-book fills. Model and thresholds are fixed, with no parameter search.
"""
from __future__ import annotations

import hashlib
import math

import numpy as np

from .live_paper import FEATURES, encoded, feature


def _fit(x, y):
    mean, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-8)
    z = np.column_stack([np.ones(len(x)), (x-mean)/scale])
    penalty = np.eye(z.shape[1])*0.1
    penalty[0, 0] = 0
    coef = np.linalg.solve(z.T@z/len(z)+penalty, z.T@y/len(z))

    def predict(values):
        return np.column_stack([np.ones(len(values)), (values-mean)/scale])@coef

    return predict, mean, scale, coef


def validated_forecast(rows, horizon_minutes=15, cost_return=0.005,
                       minimum_net_edge=0.001):
    """Fit/purge/calibrate/validate in time order; use non-overlapping OOS labels.

    60% train, 20% calibration, 20% validation. Train labels must end before
    calibration features; calibration labels must end before validation features.
    The final refit can use every label available at decision time. It is never
    used to rewrite the recorded out-of-sample results.
    """
    if horizon_minutes < 1 or cost_return < 0 or minimum_net_edge <= 0:
        raise ValueError("Invalid horizon or cost hurdle")
    xs, ys, starts, ends = [], [], [], []
    h = horizon_minutes
    for i in range(32, len(rows)-h):
        values = feature(rows, i)
        if values is None or any(rows[j]["t"]-rows[j-1]["t"] != 60 for j in range(i+1, i+h+1)):
            continue
        xs.append(values)
        ys.append(rows[i+h]["c"]/rows[i]["c"]-1)
        starts.append(rows[i]["t"]+60)
        ends.append(rows[i+h]["t"]+60)
    if len(xs) < 900:
        raise ValueError("Need at least 900 complete labeled observations for chronological validation")
    x, y = np.asarray(xs), np.asarray(ys)
    starts, ends = np.asarray(starts), np.asarray(ends)
    first, second = starts[int(len(x)*0.6)], starts[int(len(x)*0.8)]
    train = np.where(ends < first)[0]
    # Subsampling by time makes the scored forward-return intervals disjoint.
    def disjoint(indices):
        chosen, available = [], -math.inf
        for i in indices:
            if starts[i] >= available:
                chosen.append(i)
                available = ends[i]
        return np.array(chosen, dtype=int)
    calibration = disjoint(np.where((starts >= first) & (ends < second))[0])
    validation = disjoint(np.where(starts >= second)[0])
    if len(calibration) < 10 or len(validation) < 10:
        raise ValueError("Need 10 disjoint calibration and validation intervals")
    predict, _, _, _ = _fit(x[train], y[train])
    calibration_residual = predict(x[calibration])-y[calibration]
    # Empirical 80th percentile of overprediction, not a coverage guarantee.
    margin = max(0.0, float(np.quantile(calibration_residual, 0.8, method="higher")))
    prediction = predict(x[validation])
    mse = float(np.mean((prediction-y[validation])**2))
    zero_mse = float(np.mean(y[validation]**2))
    net = y[validation]-cost_return
    entries = prediction-margin-cost_return >= minimum_net_edge
    count = int(entries.sum())
    net_sum = float(net[entries].sum())
    reasons = []
    if mse >= zero_mse:
        reasons.append("out_of_sample_model_does_not_beat_zero_return_forecast")
    if count < 3:
        reasons.append("fewer_than_three_cost_qualified_validation_entries")
    if net_sum <= 0:
        reasons.append("validation_entries_not_profitable_after_assumed_costs")
    current = feature(rows, len(rows)-1)
    if current is None:
        raise ValueError("Current features are incomplete")
    final, mean, scale, coef = _fit(x, y)
    gross = float(final(np.asarray([current]))[0])
    if not math.isfinite(gross):
        raise ValueError("Non-finite model forecast")
    return {"forecast_gross_return":gross, "forecast_lower_return":gross-margin,
            "overprediction_margin":margin, "horizon_minutes":h, "training_samples":len(x),
            "training_labels_available_by":int(ends.max()), "last_feature_closed_at":rows[-1]["t"]+60,
            "features":dict(zip(FEATURES,current)), "means":mean.tolist(), "scales":scale.tolist(),
            "coefficients":coef.tolist(), "training_data_hash":hashlib.sha256(encoded(rows).encode()).hexdigest(),
            "model":"ridge alpha=0.1; purged chronological calibration/validation; final past-only refit",
            "validation_passed":not reasons, "validation_rejections":reasons,
            "validation":{"train_samples":len(train), "calibration_samples":len(calibration),
                          "validation_samples":len(validation), "model_mse":mse, "zero_forecast_mse":zero_mse,
                          "qualified_entries":count, "net_return_sum":net_sum,
                          "cost_return_assumption":cost_return,
                          "train_labels_end":int(ends[train].max()),
                          "calibration_features_start":int(starts[calibration].min()),
                          "calibration_labels_end":int(ends[calibration].max()),
                          "validation_features_start":int(starts[validation].min()),
                          "limitations":"Small historical candle screen at current assumed costs; not book-fill P&L or proof of edge."}}


def entry_rejections(model, cost_return, minimum_net_edge):
    reasons = list(model.get("validation_rejections", []))
    if not model.get("validation_passed", False) and not reasons:
        reasons.append("missing_passed_chronological_validation")
    if model.get("forecast_lower_return", -math.inf)-cost_return < minimum_net_edge:
        reasons.append("conservative_forecast_does_not_cover_costs_and_margin")
    return reasons
