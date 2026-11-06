"""Dexcom Clarity CSV importer.

Format notes (verified against a real G7 export):
- 14 columns. Headers are localized (English: Index, Timestamp (YYYY-MM-DDThh:mm:ss), Event Type,
  Event Subtype, Patient Info, Device Info, Source Device ID, Glucose Value (mg/dL), Insulin Value (u),
  Carb Value (grams), Duration (hh:mm:ss), Glucose Rate of Change (mg/dL/min), Transmitter Time (Long Integer),
  Transmitter ID). German exports use ';' as separator and translated headers, so we parse by POSITION.
- The first ~10-20 rows are metadata (name, DOB, device, alert settings) with an empty timestamp.
- Event Type "EGV" rows are glucose readings. Values below 40 / above 400 appear as text ("Low"/"High",
  localized). "Calibration", "Insulin", "Carbs", "Alert"/"Warnung" rows may also be present.
- Timestamps are local time with no offset.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import GlucoseReading, PumpEvent
from ..timeutil import local_naive_to_utc, parse_iso_local
from .base import ImportResult, upsert_events, upsert_glucose

SOURCE = "dexcom_clarity"

COL_TS, COL_TYPE, COL_SUBTYPE, COL_DEVICE_INFO, COL_SOURCE_DEVICE = 1, 2, 3, 5, 6
COL_GLUCOSE, COL_INSULIN, COL_CARBS, COL_RATE, COL_TRANSMITTER = 7, 8, 9, 11, 13

LOW_LABELS = {"low", "niedrig", "bas", "bajo", "basso", "laag"}
HIGH_LABELS = {"high", "hoch", "haut", "alto", "hoog"}


def detect_dialect(text: str) -> str:
    """Return the separator (',' or ';') by checking which yields 14 columns on the header line."""
    first = text.splitlines()[0] if text else ""
    for sep in (";", ","):
        if len(next(csv.reader([first], delimiter=sep))) == 14:
            return sep
    raise ValueError("Not a Dexcom Clarity export: expected 14 columns")


def _to_float(s: str) -> float | None:
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_clarity(
    text: str, tz_name: str | None = None
) -> tuple[list[GlucoseReading], list[PumpEvent], list[str]]:
    sep = detect_dialect(text)
    header_line = text.splitlines()[0]
    mmol = "mmol" in header_line.lower()
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    header = next(reader)
    if len(header) != 14:
        raise ValueError("Not a Dexcom Clarity export: expected 14 columns")
    readings: list[GlucoseReading] = []
    events: list[PumpEvent] = []
    warnings: list[str] = []
    for row in reader:
        if len(row) < 14:
            continue
        ts_text = row[COL_TS].strip()
        if not ts_text:
            continue  # metadata rows
        try:
            local = parse_iso_local(ts_text)
        except ValueError:
            warnings.append(f"unparseable timestamp: {ts_text}")
            continue
        ts, offset = local_naive_to_utc(local, tz_name)
        etype = row[COL_TYPE].strip().lower()
        device = row[COL_SOURCE_DEVICE].strip() or None
        if etype == "egv":
            raw = row[COL_GLUCOSE].strip()
            value = _to_float(raw)
            label = None
            if value is None and raw:
                lraw = raw.lower()
                label = "Low" if lraw in LOW_LABELS else "High" if lraw in HIGH_LABELS else raw[:32]
            elif value is not None and mmol:
                value = round(value * 18.018, 1)
            readings.append(
                GlucoseReading(
                    ts=ts,
                    utc_offset_min=offset,
                    value_mgdl=value,
                    raw_label=label,
                    trend_rate=_to_float(row[COL_RATE]),
                    source=SOURCE,
                    device=device,
                    external_id=row[COL_TRANSMITTER].strip() or None,
                )
            )
        elif etype in ("calibration", "kalibrierung"):
            events.append(
                PumpEvent(
                    ts=ts,
                    utc_offset_min=offset,
                    kind="bg_meter",
                    value=_to_float(row[COL_GLUCOSE]),
                    source=SOURCE,
                )
            )
        elif etype in ("carbs", "kohlenhydrate"):
            events.append(
                PumpEvent(
                    ts=ts,
                    utc_offset_min=offset,
                    kind="carbs_logged",
                    value=_to_float(row[COL_CARBS]),
                    source=SOURCE,
                )
            )
        elif etype in ("insulin",):
            events.append(
                PumpEvent(
                    ts=ts,
                    utc_offset_min=offset,
                    kind="insulin_logged",
                    value=_to_float(row[COL_INSULIN]),
                    notes=row[COL_SUBTYPE].strip() or None,
                    source=SOURCE,
                )
            )
        elif etype in ("alert", "warnung", "alerte", "alerta"):
            events.append(
                PumpEvent(
                    ts=ts,
                    utc_offset_min=offset,
                    kind="alert",
                    notes=row[COL_SUBTYPE].strip() or None,
                    source=SOURCE,
                )
            )
    return readings, events, warnings


def import_clarity_csv(
    session: Session, path: str | Path | None = None, text: str | None = None, tz_name: str | None = None
) -> ImportResult:
    if text is None:
        if path is None:
            raise ValueError("path or text required")
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
    result = ImportResult(source=SOURCE, filename=str(path) if path else None)
    readings, events, warnings = parse_clarity(text, tz_name)
    result.warnings.extend(warnings)
    upsert_glucose(session, readings, result)
    upsert_events(session, events, result)
    result.log(session)
    session.flush()
    return result
