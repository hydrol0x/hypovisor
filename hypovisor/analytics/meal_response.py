"""Meal response fingerprint: what glucose did in the 6 hours after a meal, with insulin context."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Bolus, Meal, MealResponse
from ..timeutil import utc_to_local
from .glucose import Series, interpolate_at, load_series
from .insulin import basal_rate_at, bolus_iob_at, load_boluses

PRE_MIN, POST_MIN = 30, 360
BOLUS_WINDOW = (-60, 30)  # minutes relative to meal in which a bolus is considered "for" the meal


def _iauc(ts: np.ndarray, vals: np.ndarray, baseline: float, t0: datetime, horizon_min: int) -> float | None:
    m = (
        (ts >= np.datetime64(t0, "s"))
        & (ts <= np.datetime64(t0 + timedelta(minutes=horizon_min), "s"))
        & ~np.isnan(vals)
    )
    if m.sum() < 2:
        return None
    t = (ts[m] - np.datetime64(t0, "s")) / np.timedelta64(1, "m")
    v = np.clip(vals[m] - baseline, 0, None)
    return float(np.trapezoid(v, t))


def compute_response(
    session: Session, meal: Meal, series: Series | None = None, boluses: list[Bolus] | None = None
) -> MealResponse:
    t0 = meal.ts
    start, end = t0 - timedelta(minutes=PRE_MIN), t0 + timedelta(minutes=POST_MIN)
    if series is None:
        series = load_series(session, start, end)
    else:
        series = series.between(start, end)
    if boluses is None:
        boluses = load_boluses(session, t0 - timedelta(minutes=settings.dia_minutes + 60), end)

    resp = meal.response or MealResponse(meal_id=meal.id)
    resp.computed_at = datetime.utcnow()
    resp.local_hour = utc_to_local(t0, meal.utc_offset_min).hour

    post = series.between(t0, end)
    resp.n_readings = int((~np.isnan(post.values)).sum())
    resp.coverage = round(resp.n_readings / (POST_MIN / 5), 3)

    # Insulin context (independent of CGM coverage)
    meal_boluses = [
        b for b in boluses if BOLUS_WINDOW[0] <= (b.ts - t0).total_seconds() / 60 <= BOLUS_WINDOW[1]
    ]
    resp.bolus_units = round(sum(b.units_total for b in meal_boluses), 2) if meal_boluses else None
    resp.prebolus_min = (
        round((t0 - min(b.ts for b in meal_boluses)).total_seconds() / 60, 1) if meal_boluses else None
    )
    carbs = [b.carbs_entered for b in meal_boluses if b.carbs_entered]
    resp.carbs_entered_on_pump = round(sum(carbs), 1) if carbs else None
    prior = [b for b in boluses if b.ts < t0 - timedelta(minutes=abs(BOLUS_WINDOW[0]))]
    resp.iob_at_meal = bolus_iob_at(prior, t0)
    resp.basal_rate_at_meal = basal_rate_at(session, t0)
    resp.insulin_6h = round(
        sum(b.units_total for b in boluses if t0 - timedelta(minutes=60) <= b.ts <= end), 2
    )

    baseline = interpolate_at(series, t0)
    if baseline is None:
        pre = series.between(start, t0)
        baseline = (
            float(np.nanmean(pre.values[-3:])) if len(pre) and not np.all(np.isnan(pre.values[-3:])) else None
        )
    resp.baseline_mgdl = round(baseline, 1) if baseline is not None else None
    b15 = interpolate_at(series, t0 - timedelta(minutes=15))
    resp.baseline_trend = (
        round((baseline - b15) / 15, 2) if baseline is not None and b15 is not None else None
    )

    if baseline is None or resp.n_readings < 6:
        resp.score = None
        resp.score_reason = "insufficient CGM data"
        return resp

    vals, ts = post.values, post.ts
    valid = ~np.isnan(vals)
    peak_i = int(np.nanargmax(vals))
    resp.peak_mgdl = float(vals[peak_i])
    resp.time_to_peak_min = float((ts[peak_i] - np.datetime64(t0, "s")) / np.timedelta64(1, "m"))
    resp.min_mgdl = float(np.nanmin(vals))
    for h in (1, 2, 3, 4):
        v = interpolate_at(series, t0 + timedelta(hours=h))
        setattr(resp, f"delta_{h}h", round(v - baseline, 1) if v is not None else None)
    resp.iauc_2h = _iauc(ts, vals, baseline, t0, 120)
    resp.iauc_4h = _iauc(ts, vals, baseline, t0, 240)

    minutes = (ts - np.datetime64(t0, "s")) / np.timedelta64(1, "m")
    lo, hi = settings.target_low, settings.target_high

    def tir(a: float, b: float) -> float | None:
        m = (minutes >= a) & (minutes <= b) & valid
        if m.sum() == 0:
            return None
        v = vals[m]
        return round(100 * float(((v >= lo) & (v <= hi)).mean()), 1)

    resp.tir_0_2h, resp.tir_2_6h, resp.tir_0_6h = tir(0, 120), tir(120, 360), tir(0, 360)
    resp.low_event = bool(np.any(vals[valid] < lo))
    late = (minutes >= 120) & (minutes <= 300) & valid
    v2 = interpolate_at(series, t0 + timedelta(hours=2))
    resp.late_rise = bool(late.sum() and v2 is not None and float(np.nanmax(vals[late])) - v2 > 40)

    # Scoring: green = stayed in range and no low; red = low, or >250, or mostly out of range; else amber.
    if baseline < lo:
        resp.score = None
        resp.score_reason = f"started low ({baseline:.0f}); treated as hypo treatment, not scored"
        return resp
    reasons = []
    if resp.low_event:
        reasons.append(f"low ({resp.min_mgdl:.0f})")
    if resp.peak_mgdl > 250:
        reasons.append(f"peak {resp.peak_mgdl:.0f}")
    tir02 = resp.tir_0_2h if resp.tir_0_2h is not None else 0
    tir06 = resp.tir_0_6h if resp.tir_0_6h is not None else 0
    if resp.low_event or resp.peak_mgdl > 250 or tir06 < 50:
        resp.score = "red"
        if tir06 < 50:
            reasons.append(f"TIR {tir06:.0f}% over 6h")
    elif tir02 >= 80 and tir06 >= 80 and resp.peak_mgdl <= hi + 20:
        resp.score = "green"
        reasons.append(f"peak {resp.peak_mgdl:.0f}, TIR {tir06:.0f}%")
    else:
        resp.score = "amber"
        reasons.append(f"peak {resp.peak_mgdl:.0f}, TIR {tir06:.0f}%")
        if resp.late_rise:
            reasons.append("late rise 2-5h")
    resp.score_reason = "; ".join(reasons)[:200]
    return resp


def recompute_all(session: Session, since: datetime | None = None) -> int:
    q = select(Meal)
    if since:
        q = q.where(Meal.ts >= since)
    meals = list(session.scalars(q.order_by(Meal.ts)))
    if not meals:
        return 0
    start = min(m.ts for m in meals) - timedelta(minutes=PRE_MIN + settings.dia_minutes + 60)
    end = max(m.ts for m in meals) + timedelta(minutes=POST_MIN)
    series = load_series(session, start, end)
    boluses = load_boluses(session, start, end)
    n = 0
    for meal in meals:
        resp = compute_response(session, meal, series, boluses)
        if meal.response is None:
            meal.response = resp
        n += 1
    session.flush()
    return n


def response_curve(session: Session, meal: Meal) -> list[dict]:
    """Glucose points relative to the meal for charting."""
    s = load_series(session, meal.ts - timedelta(minutes=PRE_MIN), meal.ts + timedelta(minutes=POST_MIN))
    out = []
    for t, v in zip(s.ts, s.values, strict=True):
        if np.isnan(v):
            continue
        out.append(
            {"minutes": float((t - np.datetime64(meal.ts, "s")) / np.timedelta64(1, "m")), "value": float(v)}
        )
    return out
