"""Insulin activity model and insulin-on-board (IOB).

Uses the exponential insulin activity curve popularized by Loop/oref0 with a configurable
duration of insulin action (DIA) and peak time. Bolus IOB only by default; basal doses can be
included to estimate "net" IOB relative to a baseline, but that needs a scheduled basal profile
to compare against, which pump exports do not include, so we keep basal separate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BasalDose, Bolus


def _curve_params(dia_min: float, peak_min: float) -> tuple[float, float, float]:
    td, tp = float(dia_min), float(peak_min)
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * np.exp(-td / tau))
    return tau, a, s


def iob_fraction(
    minutes_since: np.ndarray | float, dia_min: float | None = None, peak_min: float | None = None
) -> np.ndarray:
    """Fraction of a bolus still active `minutes_since` delivery (exponential model)."""
    dia_min = dia_min or settings.dia_minutes
    peak_min = peak_min or settings.insulin_peak_minutes
    tau, a, s = _curve_params(dia_min, peak_min)
    t = np.atleast_1d(np.asarray(minutes_since, dtype=float))
    out = np.zeros_like(t)
    m = (t >= 0) & (t < dia_min)
    tt = t[m]
    out[m] = 1 - s * (1 - a) * ((tt**2 / (tau * dia_min * (1 - a)) - tt / tau - 1) * np.exp(-tt / tau) + 1)
    return np.clip(out, 0, 1)


def activity_fraction(
    minutes_since: np.ndarray | float, dia_min: float | None = None, peak_min: float | None = None
) -> np.ndarray:
    """Insulin activity (fraction of the bolus acting per minute) at `minutes_since`."""
    dia_min = dia_min or settings.dia_minutes
    peak_min = peak_min or settings.insulin_peak_minutes
    tau, a, s = _curve_params(dia_min, peak_min)
    t = np.atleast_1d(np.asarray(minutes_since, dtype=float))
    out = np.zeros_like(t)
    m = (t >= 0) & (t < dia_min)
    tt = t[m]
    out[m] = s / tau**2 * tt * (1 - tt / dia_min) * np.exp(-tt / tau)
    return out


def load_boluses(session: Session, start: datetime, end: datetime) -> list[Bolus]:
    return list(session.scalars(select(Bolus).where(Bolus.ts >= start, Bolus.ts <= end).order_by(Bolus.ts)))


def bolus_iob_at(boluses: list[Bolus], when: datetime, dia_min: float | None = None) -> float:
    """Bolus IOB at `when` from a list of boluses (extended portions are spread over their duration)."""
    dia_min = dia_min or settings.dia_minutes
    total = 0.0
    for b in boluses:
        delta = (when - b.ts).total_seconds() / 60
        if delta < 0 or delta > dia_min + b.extended_minutes:
            continue
        if b.units_normal:
            total += b.units_normal * float(iob_fraction(delta, dia_min)[0])
        if b.units_extended and b.extended_minutes:
            # model the extended part as 5-minute micro-doses
            steps = max(1, int(b.extended_minutes // 5))
            per = b.units_extended / steps
            for k in range(steps):
                d = delta - k * 5
                if d >= 0:
                    total += per * float(iob_fraction(d, dia_min)[0])
    return round(total, 3)


def iob_series(
    boluses: list[Bolus], times: list[datetime] | np.ndarray, dia_min: float | None = None
) -> np.ndarray:
    return np.array(
        [
            bolus_iob_at(boluses, t if isinstance(t, datetime) else t.astype("datetime64[s]").item(), dia_min)
            for t in times
        ]
    )


def basal_rate_at(session: Session, when: datetime) -> float | None:
    """Commanded basal rate (U/h) in effect at `when`, from the most recent basal dose row."""
    row = session.scalars(
        select(BasalDose)
        .where(BasalDose.ts <= when, BasalDose.ts >= when - timedelta(minutes=30))
        .order_by(BasalDose.ts.desc())
    ).first()
    return row.rate_uph if row else None


def basal_units_between(session: Session, start: datetime, end: datetime) -> float:
    rows = session.scalars(select(BasalDose.units).where(BasalDose.ts >= start, BasalDose.ts < end)).all()
    return round(float(sum(rows)), 3)
