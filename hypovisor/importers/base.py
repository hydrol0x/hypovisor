from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BasalDose, Bolus, GlucoseReading, ImportLog, PumpEvent


@dataclass
class ImportResult:
    source: str
    filename: str | None = None
    glucose_added: int = 0
    glucose_skipped: int = 0
    bolus_added: int = 0
    bolus_skipped: int = 0
    basal_added: int = 0
    basal_skipped: int = 0
    events_added: int = 0
    events_skipped: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    def note_ts(self, ts: datetime) -> None:
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["first_ts"] = self.first_ts.isoformat() if self.first_ts else None
        d["last_ts"] = self.last_ts.isoformat() if self.last_ts else None
        return d

    def log(self, session: Session) -> None:
        session.add(ImportLog(source=self.source, filename=self.filename, summary=self.as_dict()))


def _existing_keys(
    session: Session, model, source: str, ts_lo: datetime, ts_hi: datetime, extra_cols=()
) -> set:
    cols = [model.ts, *extra_cols]
    rows = session.execute(
        select(*cols).where(model.source == source, model.ts >= ts_lo, model.ts <= ts_hi)
    ).all()
    return {tuple(r) for r in rows}


def upsert_glucose(session: Session, readings: Iterable[GlucoseReading], result: ImportResult) -> None:
    readings = list(readings)
    if not readings:
        return
    lo, hi = min(r.ts for r in readings), max(r.ts for r in readings)
    seen = _existing_keys(session, GlucoseReading, readings[0].source, lo, hi)
    for r in readings:
        key = (r.ts,)
        if key in seen:
            result.glucose_skipped += 1
            continue
        seen.add(key)
        session.add(r)
        result.glucose_added += 1
        result.note_ts(r.ts)


def upsert_boluses(session: Session, boluses: Iterable[Bolus], result: ImportResult) -> None:
    boluses = list(boluses)
    if not boluses:
        return
    lo, hi = min(b.ts for b in boluses), max(b.ts for b in boluses)
    seen = _existing_keys(session, Bolus, boluses[0].source, lo, hi, extra_cols=(Bolus.units_total,))
    for b in boluses:
        key = (b.ts, b.units_total)
        if key in seen:
            result.bolus_skipped += 1
            continue
        seen.add(key)
        session.add(b)
        result.bolus_added += 1
        result.note_ts(b.ts)


def upsert_basal(session: Session, doses: Iterable[BasalDose], result: ImportResult) -> None:
    doses = list(doses)
    if not doses:
        return
    lo, hi = min(d.ts for d in doses), max(d.ts for d in doses)
    seen = _existing_keys(session, BasalDose, doses[0].source, lo, hi)
    for d in doses:
        key = (d.ts,)
        if key in seen:
            result.basal_skipped += 1
            continue
        seen.add(key)
        session.add(d)
        result.basal_added += 1
        result.note_ts(d.ts)


def upsert_events(session: Session, events: Iterable[PumpEvent], result: ImportResult) -> None:
    events = list(events)
    if not events:
        return
    lo, hi = min(e.ts for e in events), max(e.ts for e in events)
    seen = _existing_keys(session, PumpEvent, events[0].source, lo, hi, extra_cols=(PumpEvent.kind,))
    for e in events:
        key = (e.ts, e.kind)
        if key in seen:
            result.events_skipped += 1
            continue
        seen.add(key)
        session.add(e)
        result.events_added += 1
        result.note_ts(e.ts)
