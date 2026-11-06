"""Autotune-lite: observed insulin settings from history.

These are *observations with sample sizes*, not recommendations. They answer questions like
"does my carb ratio appear to be working at lunch?" The logic follows the spirit of oref0 autotune:
attribute glucose deviations to carbs (meals), corrections, or basal (fasting, no IOB/COB).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Meal, MealResponse
from ..timeutil import utc_to_local
from .glucose import load_series
from .insulin import bolus_iob_at, load_boluses


def _med(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    return round(median(xs), 2) if xs else None


def observed_carb_ratio(session: Session, start: datetime, end: datetime) -> list[dict]:
    """For meals where glucose returned to within ±30 mg/dL of baseline by 4h with no low, the delivered
    bolus 'covered' the carbs: observed ICR = carbs / bolus. Over-covered (low) and under-covered meals
    are reported separately so the direction of error is visible per meal slot."""
    rows = session.execute(
        select(
            Meal.ts,
            Meal.utc_offset_min,
            Meal.carbs_g,
            MealResponse.bolus_units,
            MealResponse.delta_4h,
            MealResponse.low_event,
            MealResponse.iob_at_meal,
            MealResponse.carbs_entered_on_pump,
        )
        .join(MealResponse, MealResponse.meal_id == Meal.id)
        .where(Meal.ts >= start, Meal.ts <= end, MealResponse.bolus_units.is_not(None))
    ).all()
    buckets = {
        "breakfast (5-10)": range(5, 11),
        "lunch (11-14)": range(11, 15),
        "dinner (15-20)": range(15, 21),
        "late (21-4)": list(range(21, 24)) + list(range(0, 5)),
    }
    out = []
    for name, hours in buckets.items():
        covered, under, over, pump_ratios = [], 0, 0, []
        n = 0
        for ts, off, carbs, units, d4, low, _iob, carbs_pump in rows:
            carbs = carbs or carbs_pump
            if not carbs or not units or utc_to_local(ts, off).hour not in hours:
                continue
            n += 1
            if carbs_pump and units:
                pump_ratios.append(carbs_pump / units)
            if low or (d4 is not None and d4 < -30):
                over += 1
            elif d4 is not None and d4 > 30:
                under += 1
            elif d4 is not None:
                covered.append(carbs / units)
        out.append(
            {
                "bucket": name,
                "n": n,
                "n_well_covered": len(covered),
                "observed_icr": _med(covered),
                "pump_icr_used": _med(pump_ratios),
                "under_dosed": under,
                "over_dosed": over,
            }
        )
    return out


def basal_deviation_by_hour(session: Session, start: datetime, end: datetime) -> list[dict]:
    """Fasting glucose drift (mg/dL per hour) when no bolus IOB and no meal in the prior 4h, by local hour.
    Consistent positive drift suggests basal is too low at that hour; negative suggests too high."""
    series = load_series(session, start, end)
    if len(series) == 0:
        return []
    boluses = load_boluses(session, start - timedelta(hours=8), end)
    meals = session.scalars(
        select(Meal.ts).where(Meal.ts >= start - timedelta(hours=4), Meal.ts <= end)
    ).all()
    meal64 = (
        np.array([np.datetime64(m, "s") for m in meals]) if meals else np.array([], dtype="datetime64[s]")
    )
    drifts: dict[int, list[float]] = {h: [] for h in range(24)}
    # Walk hour by hour
    t = start.replace(minute=0, second=0, microsecond=0)
    while t + timedelta(hours=1) <= end:
        t1 = t + timedelta(hours=1)
        t64 = np.datetime64(t, "s")
        if len(meal64) and np.any((meal64 <= t64) & (meal64 >= t64 - np.timedelta64(4, "h"))):
            t = t1
            continue
        if bolus_iob_at(boluses, t) > 0.3:
            t = t1
            continue
        m = (series.ts >= t64) & (series.ts <= np.datetime64(t1, "s")) & ~np.isnan(series.values)
        if m.sum() < 8:
            t = t1
            continue
        v = series.values[m]
        off = int(series.offsets[m][0])
        local_hour = utc_to_local(t, off).hour
        drifts[local_hour].append(float(v[-1] - v[0]))
        t = t1
    return [{"hour": h, "n": len(x), "median_drift": _med(x)} for h, x in drifts.items()]
