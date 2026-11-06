from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..analytics.glucose import daily_metrics, load_series, local_day_bounds, summary_metrics
from ..analytics.insulin import bolus_iob_at, load_boluses
from ..db import get_session
from ..models import BasalDose, Bolus, GlucoseReading, Meal, PumpEvent
from ..timeutil import local_naive_to_utc, now_utc, utc_to_local

router = APIRouter(prefix="/api", tags=["data"])


def _series_json(series) -> list[dict]:
    return [
        {"ts": str(t), "local": str(loc), "value": None if np.isnan(v) else float(v)}
        for t, loc, v in zip(series.ts, series.local_ts, series.values, strict=True)
    ]


@router.get("/status")
def status(session: Session = Depends(get_session)):
    last_g = session.scalar(select(func.max(GlucoseReading.ts)))
    first_g = session.scalar(select(func.min(GlucoseReading.ts)))
    counts = {
        "glucose": session.scalar(select(func.count()).select_from(GlucoseReading)),
        "boluses": session.scalar(select(func.count()).select_from(Bolus)),
        "basal_doses": session.scalar(select(func.count()).select_from(BasalDose)),
        "meals": session.scalar(select(func.count()).select_from(Meal)),
        "events": session.scalar(select(func.count()).select_from(PumpEvent)),
    }
    latest = session.scalars(select(GlucoseReading).order_by(GlucoseReading.ts.desc()).limit(1)).first()
    boluses = load_boluses(session, now_utc() - timedelta(hours=8), now_utc())
    return {
        "counts": counts,
        "first_glucose": first_g.isoformat() if first_g else None,
        "last_glucose": last_g.isoformat() if last_g else None,
        "latest": {"value": latest.value_mgdl, "ts": latest.ts.isoformat(), "trend_rate": latest.trend_rate}
        if latest
        else None,
        "iob_now": bolus_iob_at(boluses, now_utc()),
    }


@router.get("/timeline")
def timeline(day: date | None = None, session: Session = Depends(get_session)):
    """Everything for one local calendar day: glucose, boluses, basal, meals, events."""
    if day is None:
        day = utc_to_local(now_utc()).date()
    start, end = local_day_bounds(day)
    series = load_series(session, start - timedelta(hours=3), end)
    boluses = load_boluses(session, start, end)
    basal = session.scalars(
        select(BasalDose).where(BasalDose.ts >= start, BasalDose.ts < end).order_by(BasalDose.ts)
    ).all()
    meals = session.scalars(select(Meal).where(Meal.ts >= start, Meal.ts < end).order_by(Meal.ts)).all()
    events = session.scalars(
        select(PumpEvent).where(PumpEvent.ts >= start, PumpEvent.ts < end).order_by(PumpEvent.ts)
    ).all()
    iob_times = [start + timedelta(minutes=15 * i) for i in range(97)]
    all_b = load_boluses(session, start - timedelta(hours=8), end)
    day_series = series.between(start, end)
    return {
        "day": day.isoformat(),
        "metrics": summary_metrics(day_series.values),
        "glucose": _series_json(day_series),
        "boluses": [
            {
                "id": b.id,
                "ts": b.ts.isoformat(),
                "local": utc_to_local(b.ts, b.utc_offset_min).isoformat(),
                "units": b.units_total,
                "kind": b.kind,
                "reason": b.reason,
                "carbs": b.carbs_entered,
                "bg": b.bg_entered,
                "extended_min": b.extended_minutes,
            }
            for b in boluses
        ],
        "basal": [
            {
                "ts": d.ts.isoformat(),
                "local": utc_to_local(d.ts, d.utc_offset_min).isoformat(),
                "rate": d.rate_uph,
                "units": d.units,
            }
            for d in basal
        ],
        "iob": [{"local": utc_to_local(t).isoformat(), "iob": bolus_iob_at(all_b, t)} for t in iob_times],
        "meals": [
            {
                "id": m.id,
                "ts": m.ts.isoformat(),
                "local": utc_to_local(m.ts, m.utc_offset_min).isoformat(),
                "description": m.description,
                "carbs_g": m.carbs_g,
                "score": m.response.score if m.response else None,
                "photo": bool(m.photo_path),
            }
            for m in meals
        ],
        "events": [
            {
                "ts": e.ts.isoformat(),
                "local": utc_to_local(e.ts, e.utc_offset_min).isoformat(),
                "kind": e.kind,
                "value": e.value,
                "notes": e.notes,
            }
            for e in events
        ],
        "basal_total_units": round(sum(d.units for d in basal), 2),
        "bolus_total_units": round(sum(b.units_total for b in boluses), 2),
    }


@router.get("/glucose")
def glucose(start: datetime, end: datetime, session: Session = Depends(get_session)):
    return _series_json(load_series(session, start, end))


@router.get("/metrics/daily")
def metrics_daily(days: int = Query(14, ge=1, le=365), session: Session = Depends(get_session)):
    end = now_utc()
    series = load_series(session, end - timedelta(days=days), end)
    return {"overall": summary_metrics(series.values), "days": daily_metrics(series)}


class EventIn(BaseModel):
    local_ts: datetime
    kind: str
    value: float | None = None
    duration_min: int | None = None
    notes: str | None = None


@router.post("/events")
def add_event(body: EventIn, session: Session = Depends(get_session)):
    ts, off = local_naive_to_utc(body.local_ts)
    e = PumpEvent(
        ts=ts,
        utc_offset_min=off,
        kind=body.kind,
        value=body.value,
        duration_min=body.duration_min,
        notes=body.notes,
        source="manual",
    )
    session.add(e)
    session.commit()
    return {"id": e.id}


@router.get("/events")
def list_events(days: int = 30, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(PumpEvent)
        .where(PumpEvent.ts >= now_utc() - timedelta(days=days))
        .order_by(PumpEvent.ts.desc())
    ).all()
    return [
        {
            "id": e.id,
            "local": utc_to_local(e.ts, e.utc_offset_min).isoformat(),
            "kind": e.kind,
            "value": e.value,
            "notes": e.notes,
            "source": e.source,
        }
        for e in rows
    ]


@router.delete("/events/{event_id}")
def delete_event(event_id: int, session: Session = Depends(get_session)):
    e = session.get(PumpEvent, event_id)
    if e:
        session.delete(e)
        session.commit()
    return {"ok": True}
