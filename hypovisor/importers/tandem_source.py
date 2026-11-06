"""Tandem Source export importer (t:slim X2, Mobi).

A Tandem Source export is a folder (often zipped) of CSVs named
`<Name>_<Serial>_<date>-<Type>.csv` with these types (verified against a real export):

- Bolus.csv (BOM-prefixed header): Type, Bolus Type, Bolus Delivery Method, BG (mg/dL), Serial Number,
  Completion Date Time, Insulin Delivered, Food Delivered, Correction Delivered, Completion Status Desc,
  Bolex Start Date Time, Bolex Completion Date Time, Bolex Insulin Delivered, Bolex Completion Status Desc,
  Standard Percent, Duration (mins), Carb Size, Target BG (mg/dL), Correction Factor, Carb Ratio
- Basal-doses.csv: Device Type, Serial Number, Event Date Time, Commanded Basal Dose (units of insulin)
  (one row roughly every 5 minutes; Control-IQ modulates these)
- CGM.csv: Device Type, Serial Number, Description, Event Date Time, Readings (mg/dL)
- BG.csv: Device Type, Serial Number, Description, Event Date Time, BG (mg/dL), Note
- Hourly-basal-delivery.csv: Serial Number, Event Date, 12 AM ... 11 PM  (daily totals per hour)

Timestamps are local with no offset. Values can be mg/dL or mmol/L depending on account settings.
"""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import BasalDose, Bolus, GlucoseReading, PumpEvent
from ..timeutil import local_naive_to_utc, parse_iso_local
from .base import ImportResult, upsert_basal, upsert_boluses, upsert_events, upsert_glucose

SOURCE = "tandem_source"


_ISOLATES = str.maketrans("", "", "\u2066\u2067\u2068\u2069\ufeff")


def _clean(v: str | None) -> str:
    return (v or "").translate(_ISOLATES).strip()


def _read_csv(path: Path) -> list[dict]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append({(k or "").strip().lstrip("﻿"): (v or "").strip() for k, v in row.items()})
    return rows


def _f(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


def _col(row: dict, *candidates: str) -> str | None:
    """Find a column by case-insensitive prefix so unit suffixes and localization do not break us."""
    keys = {k.lower(): k for k in row}
    for c in candidates:
        cl = c.lower()
        if cl in keys:
            return row[keys[cl]]
        for k in keys:
            if k.startswith(cl):
                return row[keys[k]]
    return None


def classify_files(folder: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in sorted(folder.rglob("*.csv")):
        if p.name.startswith("._"):
            continue
        name = p.name.lower()
        if name.endswith("-bolus.csv") or name == "bolus.csv":
            found["bolus"] = p
        elif "basal-doses" in name or name == "basal.csv":
            found["basal"] = p
        elif name.endswith("-cgm.csv") or name == "cgm.csv":
            found["cgm"] = p
        elif name.endswith("-bg.csv") or name == "bg.csv":
            found["bg"] = p
        elif "hourly-basal" in name:
            found["hourly"] = p
    if not found:
        # fall back to header sniffing
        for p in sorted(folder.rglob("*.csv")):
            head = p.read_bytes()[:400].decode("utf-8-sig", errors="replace").lower()
            if "bolus type" in head:
                found.setdefault("bolus", p)
            elif "commanded basal dose" in head:
                found.setdefault("basal", p)
            elif "readings (" in head:
                found.setdefault("cgm", p)
            elif "bg (" in head and "note" in head:
                found.setdefault("bg", p)
    return found


def parse_bolus_rows(rows: list[dict], tz_name: str | None) -> list[Bolus]:
    out: list[Bolus] = []
    for r in rows:
        if (_col(r, "Type") or "").lower() not in ("bolus", ""):
            continue
        ts_text = _col(r, "Completion Date Time") or _col(r, "Event Date Time")
        if not ts_text:
            continue
        total = _f(_col(r, "Insulin Delivered"))
        ext = _f(_col(r, "Bolex Insulin Delivered")) or 0.0
        if total is None:
            total = ext
        if total is None or total <= 0:
            continue
        duration = _f(_col(r, "Duration")) or 0.0
        std_pct = _f(_col(r, "Standard Percent"))
        btype = (_col(r, "Bolus Type") or "").lower()
        method = (_col(r, "Bolus Delivery Method") or "").lower()
        if "automatic" in btype or "automatic" in method or "control-iq" in btype:
            kind, reason = "automated", "auto"
        elif duration > 0 and std_pct is not None and 0 < std_pct < 100:
            kind, reason = "dual", "food_correction"
        elif duration > 0 or "extended" in method:
            kind, reason = "extended", "food"
        else:
            kind = "normal"
            has_food = "food" in btype
            has_corr = "correction" in btype
            reason = (
                "food_correction"
                if has_food and has_corr
                else "food"
                if has_food
                else "correction"
                if has_corr
                else "unknown"
            )
        ts_local = parse_iso_local(ts_text)
        ts, offset = local_naive_to_utc(ts_local, tz_name)
        # Completion time is after delivery; a standard bolus of a few units takes well under a minute, so we
        # keep completion time as the bolus time. Extended boluses record the start separately.
        bolex_start = _col(r, "Bolex Start Date Time")
        if kind in ("extended", "dual") and bolex_start:
            ts, offset = local_naive_to_utc(parse_iso_local(bolex_start), tz_name)
        carbs = _f(_col(r, "Carb Size"))
        bg = _f(_col(r, "BG"))
        target = _f(_col(r, "Target BG"))
        isf = _f(_col(r, "Correction Factor"))
        icr = _f(_col(r, "Carb Ratio"))
        out.append(
            Bolus(
                ts=ts,
                utc_offset_min=offset,
                units_total=round(total, 3),
                units_normal=round(total - ext, 3),
                units_extended=round(ext, 3),
                extended_minutes=int(duration),
                kind=kind,
                reason=reason,
                carbs_entered=carbs if carbs and carbs > 0 else None,
                bg_entered=bg if bg and bg > 0 else None,
                food_units=_f(_col(r, "Food Delivered")),
                correction_units=_f(_col(r, "Correction Delivered")),
                target_bg=target if target and target > 0 else None,
                isf=isf if isf and isf > 0 else None,
                icr=icr if icr and icr > 0 else None,
                status=_col(r, "Completion Status Desc") or None,
                source=SOURCE,
                device=_col(r, "Serial Number") or None,
            )
        )
    return out


def parse_basal_rows(rows: list[dict], tz_name: str | None) -> list[BasalDose]:
    parsed = []
    for r in rows:
        ts_text = _col(r, "Event Date Time")
        units = _f(_col(r, "Commanded Basal Dose"))
        if not ts_text or units is None:
            continue
        ts, offset = local_naive_to_utc(parse_iso_local(ts_text), tz_name)
        parsed.append((ts, offset, units, _col(r, "Device Type")))
    parsed.sort(key=lambda x: x[0])
    out: list[BasalDose] = []
    for i, (ts, offset, units, device) in enumerate(parsed):
        if i + 1 < len(parsed):
            gap = (parsed[i + 1][0] - ts).total_seconds() / 60
            duration = gap if 0 < gap <= 60 else 5.0
        else:
            duration = 5.0
        out.append(
            BasalDose(
                ts=ts,
                utc_offset_min=offset,
                units=units,
                duration_min=round(duration, 2),
                rate_uph=round(units / (duration / 60), 3) if duration > 0 else None,
                source=SOURCE,
                device=device,
            )
        )
    return out


def parse_cgm_rows(rows: list[dict], tz_name: str | None) -> list[GlucoseReading]:
    out = []
    for r in rows:
        ts_text = _col(r, "Event Date Time")
        if not ts_text:
            continue
        key = next((k for k in r if k.lower().startswith("readings")), None)
        if key is None:
            continue
        value = _f(r[key])
        if value is not None and "mmol" in key.lower():
            value = round(value * 18.018, 1)
        ts, offset = local_naive_to_utc(parse_iso_local(ts_text), tz_name)
        out.append(
            GlucoseReading(
                ts=ts,
                utc_offset_min=offset,
                value_mgdl=value,
                raw_label=None if value is not None else (r[key] or None),
                source=SOURCE,
                device=_col(r, "Device Type"),
            )
        )
    return out


def parse_bg_rows(rows: list[dict], tz_name: str | None) -> list[PumpEvent]:
    out = []
    for r in rows:
        ts_text = _col(r, "Event Date Time")
        if not ts_text:
            continue
        key = next((k for k in r if k.lower().startswith("bg")), None)
        value = _f(r[key]) if key else None
        if value is not None and key and "mmol" in key.lower():
            value = round(value * 18.018, 1)
        ts, offset = local_naive_to_utc(parse_iso_local(ts_text), tz_name)
        out.append(
            PumpEvent(
                ts=ts,
                utc_offset_min=offset,
                kind="bg_meter",
                value=value,
                notes=_col(r, "Note") or None,
                source=SOURCE,
            )
        )
    return out


def import_tandem_source(session: Session, path: str | Path, tz_name: str | None = None) -> ImportResult:
    """Import a Tandem Source export folder, zip file, or a single CSV from it."""
    path = Path(path)
    result = ImportResult(source=SOURCE, filename=str(path))
    with tempfile.TemporaryDirectory() as tmp:
        if path.is_file() and path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmp)
            folder = Path(tmp)
        elif path.is_file():
            folder = Path(tmp)
            (folder / path.name).write_bytes(path.read_bytes())
        else:
            folder = path
        files = classify_files(folder)
        if not files:
            raise ValueError(
                "No Tandem Source CSV files found (expected *-Bolus.csv, *-Basal-doses.csv, *-CGM.csv ...)"
            )
        if "bolus" in files:
            upsert_boluses(session, parse_bolus_rows(_read_csv(files["bolus"]), tz_name), result)
        if "basal" in files:
            upsert_basal(session, parse_basal_rows(_read_csv(files["basal"]), tz_name), result)
        if "cgm" in files:
            upsert_glucose(session, parse_cgm_rows(_read_csv(files["cgm"]), tz_name), result)
        if "bg" in files:
            upsert_events(session, parse_bg_rows(_read_csv(files["bg"]), tz_name), result)
        if "hourly" in files:
            result.warnings.append(
                "Hourly-basal-delivery.csv ignored: basal is reconstructed from Basal-doses.csv"
            )
    result.log(session)
    session.flush()
    return result
