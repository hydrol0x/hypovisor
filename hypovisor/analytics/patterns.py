"""Pattern mining over the joined timeline: per-food outcomes, bolus history, time-of-day, lifecycle."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..models import Food, Meal, MealItem, MealResponse, PumpEvent
from ..timeutil import utc_to_local
from .glucose import Series, interpolate_at, load_series, summary_metrics
from .insulin import bolus_iob_at, load_boluses


def _med(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(median(xs), 1) if xs else None


def food_outcomes(session: Session, food_id: int) -> dict:
    """Aggregate meal responses for every meal containing this food, with the insulin context table."""
    food = session.get(Food, food_id)
    if food is None:
        return {}
    meals = list(
        session.scalars(
            select(Meal)
            .join(MealItem, MealItem.meal_id == Meal.id)
            .where(MealItem.food_id == food_id)
            .options(selectinload(Meal.response), selectinload(Meal.items))
            .order_by(Meal.ts.desc())
        ).unique()
    )
    rows = []
    for m in meals:
        r = m.response
        rows.append(
            {
                "meal_id": m.id,
                "ts": m.ts.isoformat(),
                "local": utc_to_local(m.ts, m.utc_offset_min).strftime("%Y-%m-%d %H:%M"),
                "carbs_g": m.carbs_g,
                "fat_g": m.fat_g,
                "protein_g": m.protein_g,
                "score": r.score if r else None,
                "score_reason": r.score_reason if r else None,
                "baseline": r.baseline_mgdl if r else None,
                "peak": r.peak_mgdl if r else None,
                "time_to_peak_min": r.time_to_peak_min if r else None,
                "delta_2h": r.delta_2h if r else None,
                "tir_0_6h": r.tir_0_6h if r else None,
                "low_event": r.low_event if r else None,
                "late_rise": r.late_rise if r else None,
                "bolus_units": r.bolus_units if r else None,
                "prebolus_min": r.prebolus_min if r else None,
                "iob_at_meal": r.iob_at_meal if r else None,
                "carbs_on_pump": r.carbs_entered_on_pump if r else None,
                "units_per_10g": round(10 * r.bolus_units / m.carbs_g, 2)
                if r and r.bolus_units and m.carbs_g
                else None,
            }
        )
    scored = [x for x in rows if x["score"]]
    n = len(scored)
    summary = {
        "food": {
            "id": food.id,
            "name": food.name,
            "carbs_g": food.carbs_g,
            "fat_g": food.fat_g,
            "protein_g": food.protein_g,
        },
        "n_meals": len(rows),
        "n_scored": n,
        "pct_green": round(100 * sum(x["score"] == "green" for x in scored) / n, 0) if n else None,
        "pct_red": round(100 * sum(x["score"] == "red" for x in scored) / n, 0) if n else None,
        "median_peak": _med([x["peak"] for x in scored]),
        "median_delta_2h": _med([x["delta_2h"] for x in scored]),
        "median_time_to_peak": _med([x["time_to_peak_min"] for x in scored]),
        "median_tir_6h": _med([x["tir_0_6h"] for x in scored]),
        "late_rise_rate": round(100 * sum(bool(x["late_rise"]) for x in scored) / n, 0) if n else None,
        "low_rate": round(100 * sum(bool(x["low_event"]) for x in scored) / n, 0) if n else None,
        "bolus_history": bolus_history(scored),
        "meals": rows,
    }
    return summary


def bolus_history(rows: list[dict]) -> list[dict]:
    """Group outcomes by (bolus units bucket, pre-bolus bucket) to show what has worked."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for x in rows:
        if x["bolus_units"] is None:
            key = ("no bolus", "-")
        else:
            units = round(x["bolus_units"] * 2) / 2
            pre = x["prebolus_min"] or 0
            pre_b = (
                "no pre-bolus"
                if pre < 5
                else "5-14 min"
                if pre < 15
                else "15-29 min"
                if pre < 30
                else "30+ min"
            )
            key = (f"{units:g} u", pre_b)
        groups[key].append(x)
    out = []
    for (units, pre), xs in groups.items():  # noqa: B007
        n = len(xs)
        out.append(
            {
                "bolus": units,
                "prebolus": pre,
                "n": n,
                "green": sum(x["score"] == "green" for x in xs),
                "amber": sum(x["score"] == "amber" for x in xs),
                "red": sum(x["score"] == "red" for x in xs),
                "lows": sum(bool(x["low_event"]) for x in xs),
                "median_peak": _med([x["peak"] for x in xs]),
                "median_delta_2h": _med([x["delta_2h"] for x in xs]),
                "median_iob": _med([x["iob_at_meal"] for x in xs]),
            }
        )
    out.sort(key=lambda g: (-g["n"], g["bolus"]))
    return out


def food_leaderboard(session: Session, min_meals: int = 1) -> list[dict]:
    rows = session.execute(
        select(
            Food.id,
            Food.name,
            Food.times_eaten,
            MealResponse.score,
            MealResponse.peak_mgdl,
            MealResponse.tir_0_6h,
            MealResponse.low_event,
        )
        .join(MealItem, MealItem.food_id == Food.id)
        .join(Meal, Meal.id == MealItem.meal_id)
        .outerjoin(MealResponse, MealResponse.meal_id == Meal.id)
    ).all()
    by: dict[int, dict] = {}
    for fid, name, _times, score, peak, tir, low in rows:
        d = by.setdefault(
            fid,
            {
                "id": fid,
                "name": name,
                "n": 0,
                "green": 0,
                "amber": 0,
                "red": 0,
                "peaks": [],
                "tirs": [],
                "lows": 0,
            },
        )
        d["n"] += 1
        if score:
            d[score] += 1
        if peak is not None:
            d["peaks"].append(peak)
        if tir is not None:
            d["tirs"].append(tir)
        if low:
            d["lows"] += 1
    out = []
    for d in by.values():
        if d["n"] < min_meals:
            continue
        scored = d["green"] + d["amber"] + d["red"]
        out.append(
            {
                "id": d["id"],
                "name": d["name"],
                "n": d["n"],
                "green": d["green"],
                "amber": d["amber"],
                "red": d["red"],
                "pct_green": round(100 * d["green"] / scored) if scored else None,
                "median_peak": _med(d["peaks"]),
                "median_tir_6h": _med(d["tirs"]),
                "lows": d["lows"],
            }
        )
    out.sort(key=lambda d: (-d["n"], d["name"]))
    return out


def meal_scores_by_hour(session: Session) -> list[dict]:
    rows = session.execute(
        select(MealResponse.local_hour, MealResponse.score).where(MealResponse.score.is_not(None))
    ).all()
    buckets = {
        "breakfast (5-10)": range(5, 11),
        "lunch (11-14)": range(11, 15),
        "dinner (15-20)": range(15, 21),
        "late (21-4)": list(range(21, 24)) + list(range(0, 5)),
    }
    out = []
    for name, hours in buckets.items():
        xs = [s for h, s in rows if h in hours]
        n = len(xs)
        out.append(
            {
                "bucket": name,
                "n": n,
                "pct_green": round(100 * xs.count("green") / n) if n else None,
                "pct_red": round(100 * xs.count("red") / n) if n else None,
            }
        )
    return out


def day_of_week(series: Series) -> list[dict]:
    if len(series) == 0:
        return []
    dow = (
        series.local_ts.astype("datetime64[D]").astype(int) + 3
    ) % 7  # 1970-01-01 was a Thursday -> Monday=0
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = []
    for d in range(7):
        m = summary_metrics(series.values[dow == d])
        m["day"] = names[d]
        out.append(m)
    return out


def overnight_and_dawn(series: Series) -> dict:
    """Median glucose change 00:00-03:00 and 03:00-07:00 across nights; large positive 03-07 suggests dawn phenomenon."""
    if len(series) == 0:
        return {}
    local = series.local_ts
    days = np.unique(local.astype("datetime64[D]"))
    early, dawn = [], []
    for d in days:

        def at(h):
            t = d.astype("datetime64[s]") + np.timedelta64(h, "h")
            m = np.abs((local - t) / np.timedelta64(1, "m")) <= 7
            v = series.values[m]
            v = v[~np.isnan(v)]
            return float(v.mean()) if len(v) else None

        v0, v3, v7 = at(0), at(3), at(7)
        if v0 is not None and v3 is not None:
            early.append(v3 - v0)
        if v3 is not None and v7 is not None:
            dawn.append(v7 - v3)
    return {
        "nights": len(days),
        "median_change_00_03": _med(early),
        "median_change_03_07": _med(dawn),
        "dawn_rise_rate": round(100 * sum(x > 30 for x in dawn) / len(dawn)) if dawn else None,
    }


def sensitivity_by_time_of_day(session: Session, start: datetime, end: datetime) -> list[dict]:
    """Observed insulin sensitivity from correction-only boluses (no carbs entered, no meal within 2h):
    glucose drop over 3 hours per unit delivered, bucketed by local hour. Compare with the pump's ISF."""
    boluses = [
        b
        for b in load_boluses(session, start, end)
        if not b.carbs_entered and b.reason in ("correction", "unknown", "auto") and b.units_total >= 0.5
    ]
    meals = session.scalars(
        select(Meal.ts).where(Meal.ts >= start - timedelta(hours=3), Meal.ts <= end)
    ).all()
    meal_ts = (
        np.array([np.datetime64(m, "s") for m in meals]) if meals else np.array([], dtype="datetime64[s]")
    )
    series = load_series(session, start - timedelta(hours=1), end + timedelta(hours=4))
    all_boluses = load_boluses(session, start - timedelta(hours=6), end + timedelta(hours=4))
    samples = []
    for b in boluses:
        t64 = np.datetime64(b.ts, "s")
        if len(meal_ts) and np.any(np.abs((meal_ts - t64) / np.timedelta64(1, "h")) <= 2):
            continue
        # skip if another bolus with carbs lands in the window
        if any(
            o.carbs_entered and b.ts - timedelta(hours=2) <= o.ts <= b.ts + timedelta(hours=3)
            for o in all_boluses
        ):
            continue
        g0 = interpolate_at(series, b.ts)
        g3 = interpolate_at(series, b.ts + timedelta(hours=3))
        if g0 is None or g3 is None:
            continue
        local_hour = utc_to_local(b.ts, b.utc_offset_min).hour
        samples.append(
            {
                "hour": local_hour,
                "drop_per_unit": (g0 - g3) / b.units_total,
                "units": b.units_total,
                "isf": b.isf,
            }
        )
    buckets = {
        "overnight (0-5)": range(0, 6),
        "morning (6-11)": range(6, 12),
        "afternoon (12-17)": range(12, 18),
        "evening (18-23)": range(18, 24),
    }
    out = []
    for name, hours in buckets.items():
        xs = [s for s in samples if s["hour"] in hours]
        out.append(
            {
                "bucket": name,
                "n": len(xs),
                "observed_isf": _med([s["drop_per_unit"] for s in xs]),
                "pump_isf": _med([s["isf"] for s in xs]),
            }
        )
    return out


def lifecycle(session: Session, start: datetime, end: datetime, kind: str = "site_change") -> list[dict]:
    """TIR by days since the last site change / sensor start (needs those events to be logged)."""
    events = session.scalars(
        select(PumpEvent.ts)
        .where(PumpEvent.kind == kind, PumpEvent.ts >= start - timedelta(days=10), PumpEvent.ts <= end)
        .order_by(PumpEvent.ts)
    ).all()
    if not events:
        return []
    series = load_series(session, start, end)
    ev = np.array([np.datetime64(e, "s") for e in events])
    idx = np.searchsorted(ev, series.ts, side="right") - 1
    age_days = np.full(len(series), np.nan)
    ok = idx >= 0
    age_days[ok] = ((series.ts[ok] - ev[idx[ok]]) / np.timedelta64(1, "h")) / 24
    out = []
    for d in range(0, 11):
        m = (age_days >= d) & (age_days < d + 1)
        s = summary_metrics(series.values[m])
        s["day"] = d
        out.append(s)
    return out


def recent_bolus_context(session: Session, when: datetime) -> dict:
    boluses = load_boluses(session, when - timedelta(minutes=settings.dia_minutes + 60), when)
    return {"iob": bolus_iob_at(boluses, when), "last_bolus": boluses[-1].ts.isoformat() if boluses else None}
