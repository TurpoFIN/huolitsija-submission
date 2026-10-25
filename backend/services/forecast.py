"""Demand and revenue forecasting from history: exponential smoothing in pure Python (no numpy dependency).

    fc = forecast_series(values, horizon=12, season=52)

Smoothing candidate, chosen by how much history there is:
  - Holt-Winters additive (level + trend + season), when the series covers at least two full seasons;
  - damped additive trend (Holt with damping), otherwise;
  - no forecast when there are fewer than MIN_POINTS points (the caller shows "not enough history").
Smoothing parameters are fitted by grid search on one-step-ahead squared error. The 80 % prediction interval comes from the
one-step residuals, widened per step with the standard ETS(A,Ad,N)/(A,Ad,A) variance multipliers. Every result carries a
rolling-origin backtest (MAPE, sMAPE, MAE) against a naive baseline (seasonal-naive when a season exists), so a reader can
see whether the model beats "same as last period" on this data. Serve the baseline when it has lower MAE.
The selection backtest is not an independent accuracy test. Bands are nominal, not calibrated coverage.
"""
from __future__ import annotations

import math

MIN_POINTS = 8
MIN_TRAIN = 6  # a backtest fold refits on at least this many points
Z80 = 1.2815515655446004  # two-sided 80 % normal quantile

_ALPHAS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8)
_BETAS = (0.01, 0.05, 0.1, 0.2, 0.3)
_GAMMAS = (0.05, 0.1, 0.2, 0.3)
_PHIS = (0.8, 0.9, 0.98, 1.0)


def _damp_sum(phi: float, h: int) -> float:
    """phi + phi^2 + ... + phi^h."""
    return sum(phi ** i for i in range(1, h + 1))


def _fit_holt(y: list[float], alpha: float, beta: float, phi: float) -> tuple[float, list[float], float, float]:
    """Damped additive trend. Returns (sse, one-step residuals, last level, last trend)."""
    level = y[0]
    trend = (y[min(3, len(y) - 1)] - y[0]) / max(1, min(3, len(y) - 1))
    sse, resid = 0.0, []
    for t in range(1, len(y)):
        pred = level + phi * trend
        e = y[t] - pred
        resid.append(e)
        sse += e * e
        new_level = alpha * y[t] + (1 - alpha) * (level + phi * trend)
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        level = new_level
    return sse, resid, level, trend


def _fit_hw(y: list[float], m: int, alpha: float, beta: float, gamma: float, phi: float):
    """Holt-Winters additive with damped trend. Returns (sse, residuals, level, trend, seasonals[m] aligned to the next step)."""
    first = sum(y[:m]) / m
    second = sum(y[m:2 * m]) / m
    trend = (second - first) / m
    # seasonal indices from the first two seasons with the linear trend removed (centred on each season's mean)
    season = [((y[i] - (first + (i - (m - 1) / 2) * trend)) + (y[i + m] - (second + (i - (m - 1) / 2) * trend))) / 2
              for i in range(m)]
    level = first + (m - 1) / 2 * trend
    sse, resid = 0.0, []
    for t in range(m, len(y)):
        s = season[t % m]
        pred = level + phi * trend + s
        e = y[t] - pred
        resid.append(e)
        sse += e * e
        new_level = alpha * (y[t] - s) + (1 - alpha) * (level + phi * trend)
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        season[t % m] = gamma * (y[t] - new_level) + (1 - gamma) * s
        level = new_level
    n = len(y)
    aligned = [season[(n + i) % m] for i in range(m)]
    return sse, resid, level, trend, aligned


def _fit(y: list[float], season: int | None) -> dict:
    """Best parameters by grid search. Picks Holt-Winters only with two full seasons of history."""
    use_hw = bool(season and season >= 2 and len(y) >= 2 * season + 2)
    best = None
    if use_hw:
        for a in _ALPHAS:
            for b in _BETAS:
                for g in _GAMMAS:
                    for p in _PHIS:
                        sse, resid, level, trend, seas = _fit_hw(y, season, a, b, g, p)
                        if best is None or sse < best["sse"]:
                            best = {"method": "holt_winters_additive", "alpha": a, "beta": b, "gamma": g, "phi": p, "sse": sse,
                                    "resid": resid, "level": level, "trend": trend, "season": seas, "m": season}
    else:
        for a in _ALPHAS:
            for b in _BETAS:
                for p in _PHIS:
                    sse, resid, level, trend = _fit_holt(y, a, b, p)
                    if best is None or sse < best["sse"]:
                        best = {"method": "damped_trend", "alpha": a, "beta": b, "gamma": None, "phi": p, "sse": sse,
                                "resid": resid, "level": level, "trend": trend, "season": None, "m": None}
    return best


def _point(model: dict, h: int) -> float:
    v = model["level"] + _damp_sum(model["phi"], h) * model["trend"]
    if model["season"]:
        v += model["season"][(h - 1) % model["m"]]
    return v


def _variance_multiplier(model: dict, h: int) -> float:
    """1 + sum_{j=1}^{h-1} c_j^2 with c_j = alpha(1 + beta * damp_sum(phi, j)) [+ gamma at seasonal lags]."""
    a, b, p = model["alpha"], model["beta"], model["phi"]
    g, m = model["gamma"] or 0.0, model["m"] or 0
    total = 1.0
    for j in range(1, h):
        c = a * (1 + b * _damp_sum(p, j))
        if m and j % m == 0:
            c += g * (1 - a)
        total += c * c
    return total


def _forecast(y: list[float], horizon: int, season: int | None, floor: float | None = 0.0) -> dict:
    model = _fit(y, season)
    resid = model["resid"]
    sigma = math.sqrt(sum(e * e for e in resid) / max(1, len(resid) - 1)) if resid else 0.0
    points = []
    for h in range(1, horizon + 1):
        mean = _point(model, h)
        half = Z80 * sigma * math.sqrt(_variance_multiplier(model, h))
        lo, hi = mean - half, mean + half
        if floor is not None:
            mean, lo, hi = max(floor, mean), max(floor, lo), max(floor, hi)
        points.append({"step": h, "mean": mean, "lo80": lo, "hi80": hi})
    return {"model": model, "sigma": sigma, "points": points}


def _errors(actual: list[float], predicted: list[float]) -> dict:
    ape = [abs(a - p) / abs(a) for a, p in zip(actual, predicted) if a]
    smape = [2 * abs(a - p) / (abs(a) + abs(p)) for a, p in zip(actual, predicted) if (abs(a) + abs(p)) > 0]
    mae = [abs(a - p) for a, p in zip(actual, predicted)]
    return {"mape_pct": round(100 * sum(ape) / len(ape), 1) if ape else None,
            "smape_pct": round(100 * sum(smape) / len(smape), 1) if smape else None,
            "mae": round(sum(mae) / len(mae), 2) if mae else None, "n": len(mae)}


def _baseline(y: list[float], horizon: int, season: int | None, floor: float | None = 0.0) -> dict:
    """Seasonal random walk, or last value; uncertainty comes from its own residuals."""
    lag = season if season and season >= 2 and len(y) >= season else 1
    residuals = [y[t] - y[t - lag] for t in range(lag, len(y))]
    sigma = math.sqrt(sum(e * e for e in residuals) / len(residuals)) if residuals else None
    points = []
    for h in range(1, horizon + 1):
        mean = y[-lag + (h - 1) % lag]
        half = Z80 * sigma * math.sqrt(1 + (h - 1) // lag) if sigma is not None else None
        lo, hi = (mean - half, mean + half) if half is not None else (None, None)
        if floor is not None:
            mean = max(floor, mean)
            lo = max(floor, lo) if lo is not None else None
            hi = max(floor, hi) if hi is not None else None
        points.append({"step": h, "mean": mean, "lo80": lo, "hi80": hi})
    return {"method": "seasonal_naive" if lag > 1 else "naive_last_value", "sigma": sigma, "points": points}


def backtest(y: list[float], season: int | None, *, horizon: int = 4, folds: int = 6) -> dict | None:
    """Rolling-origin evaluation: refit on y[:origin], forecast `horizon` steps, compare with what happened.
    Baseline = seasonal-naive (value one season back) when a season fits in the training data, else last value."""
    n = len(y)
    horizon = max(1, min(horizon, n // 4))
    origins = [o for o in range(n - horizon - folds + 1, n - horizon + 1) if o >= MIN_TRAIN]
    if not origins:
        return None
    actual, model_pred, base_pred = [], [], []
    for o in origins:
        train = y[:o]
        fc = _forecast(train, horizon, season)
        base = _baseline(train, horizon, season)
        for h in range(1, horizon + 1):
            actual.append(y[o + h - 1])
            model_pred.append(fc["points"][h - 1]["mean"])
            base_pred.append(base["points"][h - 1]["mean"])
    m, b = _errors(actual, model_pred), _errors(actual, base_pred)
    beats = (m["mae"] is not None and b["mae"] is not None and m["mae"] <= b["mae"])
    return {"origins": len(origins), "horizon": horizon, "model": m, "baseline": b,
            "baseline_method": "seasonal_naive" if season and origins[0] >= season else "naive_last_value",
            "model_beats_baseline": beats}


def forecast_series(values: list[float], *, horizon: int = 12, season: int | None = None, floor: float | None = 0.0) -> dict:
    """Forecast after the last value; missing/nonfinite observations return incomplete_history."""
    if any(v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return {"status": "incomplete_history", "points_used": len(values), "min_points": MIN_POINTS,
                "forecast": [], "method": None, "backtest": None}
    y = [float(v) for v in values]
    if len(y) < MIN_POINTS:
        return {"status": "insufficient_history", "points_used": len(y), "min_points": MIN_POINTS, "forecast": [],
                "method": None, "backtest": None}
    fc = _forecast(y, horizon, season, floor)
    model = fc["model"]
    bt = backtest(y, season, horizon=horizon)
    use_baseline = not bt or not bt["model_beats_baseline"]
    selected = _baseline(y, horizon, season, floor) if use_baseline else {**fc, "method": model["method"]}
    if bt:
        bt["selected"] = bt["baseline"] if use_baseline else bt["model"]
    sigma = selected["sigma"]
    return {
        "status": "ok", "points_used": len(y), "min_points": MIN_POINTS, "method": selected["method"],
        "params": None if use_baseline else {k: model[k] for k in ("alpha", "beta", "gamma", "phi")},
        "season_length": season if selected["method"] == "seasonal_naive" else model["m"] if not use_baseline else None,
        "residual_sigma": round(sigma, 4) if sigma is not None else None, "interval": 0.8,
        "interval_calibrated": False,
        "selection": "baseline_lower_error" if use_baseline and bt else "model_lower_error" if bt else "no_backtest",
        "validation": "limited_history" if not bt or bt["origins"] < 3 else "historical_backtest",
        "forecast": [{k: round(v, 2) if v is not None else None for k, v in p.items()} for p in selected["points"]],
        "backtest": bt,
    }
