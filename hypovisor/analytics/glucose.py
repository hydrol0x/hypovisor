"""Glucose series access and standard CGM metrics (TIR, mean, CV, GMI, AGP percentiles)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GlucoseReading
from ..timeutil import utc_to_local


@dataclass
class Series:
    ts: np.ndarray  # datetime64[s] naive UTC
    values: np.ndarray  # float mg/dL, NaN for Low/High
    offsets: np.ndarray  # int minutes

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def local_ts(self) -> np.ndarray:
        return self.ts + self.offsets.astype("timedelta64[m]")

    def between(self, start: datetime, end: datetime) -> Series:
        s64, e64 = np.datetime64(start, "s"), np.datetime64(end, "s")
        m = (self.ts >= s64) & (self.ts <= e64)
        return Series(self.ts[m], self.values[m], self.offsets[m])


def load_series(session: Session, start: datetime, end: datetime, preferred: str | None = None) -> Series:
    """Load readings in [start, end], merging sources: the preferred source wins; other sources only fill
    gaps where no preferred reading exists within 2.5 minutes."""
    preferred = preferred or settings.preferred_cgm_source
    rows = session.execute(
        select(
            GlucoseReading.ts,
            GlucoseReading.value_mgdl,
            GlucoseReading.utc_offset_min,
            GlucoseReading.source,
            GlucoseReading.raw_label,
        )
        .where(GlucoseReading.ts >= start, GlucoseReading.ts <= end)
        .order_by(GlucoseReading.ts)
    ).all()
    if not rows:
        return Series(np.array([], dtype="datetime64[s]"), np.array([]), np.array([], dtype=int))
    pref = [r for r in rows if r.source == preferred]
    others = [r for r in rows if r.source != preferred]
    if pref and others:
        pref_ts = np.array([np.datetime64(r.ts, "s") for r in pref])
        keep = []
        for r in others:
            t = np.datetime64(r.ts, "s")
            idx = np.searchsorted(pref_ts, t)
            near = False
            for j in (idx - 1, idx):
                if 0 <= j < len(pref_ts) and abs((pref_ts[j] - t) / np.timedelta64(1, "s")) <= 150:
                    near = True
                    break
            if not near:
                keep.append(r)
        rows = sorted(pref + keep, key=lambda r: r.ts)
    elif others and not pref:
        rows = others
    else:
        rows = pref
    ts = np.array([np.datetime64(r.ts, "s") for r in rows])
    vals = np.array([_value_or_edge(r) for r in rows], dtype=float)
    offs = np.array([r.utc_offset_min or 0 for r in rows], dtype=int)
    return Series(ts, vals, offs)


def _value_or_edge(r) -> float:
    if r.value_mgdl is not None:
        return float(r.value_mgdl)
    if r.raw_label == "Low":
        return 39.0
    if r.raw_label == "High":
        return 401.0
    return float("nan")


def summary_metrics(values: np.ndarray, low: float | None = None, high: float | None = None) -> dict:
    low = settings.target_low if low is None else low
    high = settings.target_high if high is None else high
    v = values[~np.isnan(values)]
    if len(v) == 0:
        return {"n": 0}
    mean = float(v.mean())
    sd = float(v.std(ddof=1)) if len(v) > 1 else 0.0
    return {
        "n": int(len(v)),
        "mean": round(mean, 1),
        "sd": round(sd, 1),
        "cv": round(100 * sd / mean, 1) if mean else None,
        "gmi": round(3.31 + 0.02392 * mean, 2),
        "tir": round(100 * float(((v >= low) & (v <= high)).mean()), 1),
        "tbr": round(100 * float((v < low).mean()), 1),
        "tbr_54": round(100 * float((v < 54).mean()), 1),
        "tar": round(100 * float((v > high).mean()), 1),
        "tar_250": round(100 * float((v > 250).mean()), 1),
        "min": float(v.min()),
        "max": float(v.max()),
        "median": float(np.median(v)),
    }


def daily_metrics(series: Series) -> list[dict]:
    """Per local calendar day summary."""
    if len(series) == 0:
        return []
    local = series.local_ts.astype("datetime64[D]")
    out = []
    for day in np.unique(local):
        m = local == day
        d = summary_metrics(series.values[m])
        d["date"] = str(day)
        d["coverage"] = round(100 * d["n"] / 288, 1)
        out.append(d)
    return out


def hourly_profile(series: Series) -> list[dict]:
    """AGP-style percentiles per local hour (0-23)."""
    if len(series) == 0:
        return []
    hours = series.local_ts.astype("datetime64[h]").astype(int) % 24
    out = []
    for h in range(24):
        v = series.values[(hours == h) & ~np.isnan(series.values)]
        if len(v) == 0:
            out.append({"hour": h, "n": 0})
            continue
        p = np.percentile(v, [5, 25, 50, 75, 95])
        out.append(
            {
                "hour": h,
                "n": int(len(v)),
                "p5": round(float(p[0]), 1),
                "p25": round(float(p[1]), 1),
                "p50": round(float(p[2]), 1),
                "p75": round(float(p[3]), 1),
                "p95": round(float(p[4]), 1),
                "tir": round(
                    100 * float(((v >= settings.target_low) & (v <= settings.target_high)).mean()), 1
                ),
                "tbr": round(100 * float((v < settings.target_low).mean()), 1),
            }
        )
    return out


def interpolate_at(series: Series, when: datetime, max_gap_min: float = 12.0) -> float | None:
    """Linear interpolation of glucose at `when`, or None when the nearest readings are too far apart."""
    if len(series) == 0:
        return None
    t = np.datetime64(when, "s")
    idx = int(np.searchsorted(series.ts, t))
    if idx < len(series) and series.ts[idx] == t:
        v = series.values[idx]
        return None if np.isnan(v) else float(v)
    if idx == 0 or idx >= len(series):
        j = 0 if idx == 0 else len(series) - 1
        if abs((series.ts[j] - t) / np.timedelta64(1, "m")) <= max_gap_min / 2:
            return None if np.isnan(series.values[j]) else float(series.values[j])
        return None
    t0, t1 = series.ts[idx - 1], series.ts[idx]
    if (t1 - t0) / np.timedelta64(1, "m") > max_gap_min:
        return None
    v0, v1 = series.values[idx - 1], series.values[idx]
    if np.isnan(v0) or np.isnan(v1):
        return None
    w = (t - t0) / (t1 - t0)
    return float(v0 + w * (v1 - v0))


def local_day_bounds(day: date, tz_name: str | None = None) -> tuple[datetime, datetime]:
    """Naive-UTC bounds for a local calendar day."""
    from ..timeutil import local_naive_to_utc

    start, _ = local_naive_to_utc(datetime(day.year, day.month, day.day), tz_name)
    end, _ = local_naive_to_utc(datetime(day.year, day.month, day.day) + timedelta(days=1), tz_name)
    return start, end


def local_date_of(ts: datetime, offset_min: int) -> date:
    return utc_to_local(ts, offset_min).date()
