"""Nightscout readers: API v3 pull, and local JSON files (backups, OpenAPS Data Commons folders).

The API path is the easiest "live" route for Tandem users: run jwoglom/tconnectsync against Tandem Source and
point Hypovisor at the resulting Nightscout. Loop/Trio/AAPS users get everything here as well.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BasalDose, Bolus, GlucoseReading, PumpEvent, Setting
from ..timeutil import now_utc
from .base import ImportResult, upsert_basal, upsert_boluses, upsert_events, upsert_glucose

SOURCE = "nightscout"
_EVENT_KINDS = {
    "site change": "site_change",
    "sensor start": "sensor_start",
    "sensor change": "sensor_start",
    "insulin change": "cartridge_change",
    "exercise": "exercise",
    "note": "note",
    "bg check": "bg_meter",
    "pump battery change": "note",
}


def _client(base_url: str | None, token: str | None) -> httpx.Client:
    base = (base_url or settings.nightscout_url).rstrip("/")
    tok = token or settings.nightscout_token
    params = {"token": tok} if tok else {}
    return httpx.Client(
        base_url=base + "/api/v3", headers={"Accept": "application/json"}, params=params, timeout=30
    )


def _ms_to_dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _offset(doc: dict) -> int:
    try:
        return int(doc.get("utcOffset") or 0)
    except (TypeError, ValueError):
        return 0


def _doc_ts(doc: dict) -> datetime | None:
    if doc.get("date") is not None:
        try:
            v = float(doc["date"])
            return _ms_to_dt(v if v > 1e11 else v * 1000)
        except (TypeError, ValueError):
            pass
    for k in ("created_at", "dateString"):
        s = doc.get(k)
        if isinstance(s, str) and s:
            try:
                t = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t
            except ValueError:
                continue
    return None


def _fetch(client: httpx.Client, collection: str, since: datetime, until: datetime) -> list[dict]:
    docs, limit, skip = [], 1000, 0
    lo = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)
    hi = int(until.replace(tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        r = client.get(
            f"/{collection}",
            params={"date$gte": lo, "date$lte": hi, "limit": limit, "skip": skip, "sort": "date"},
        )
        r.raise_for_status()
        body = r.json()
        batch = body.get("result", body) if isinstance(body, dict) else body
        docs.extend(batch)
        if len(batch) < limit:
            break
        skip += limit
    return docs


def _number(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def docs_to_models(entries: list[dict], treatments: list[dict]) -> tuple[list, list, list, list]:
    """Map Nightscout entries/treatments documents to (readings, boluses, basal doses, events)."""
    readings = []
    for e in entries:
        ts = _doc_ts(e)
        sgv = _number(e.get("sgv"))
        if e.get("type", "sgv") != "sgv" or sgv is None or ts is None:
            continue
        readings.append(
            GlucoseReading(
                ts=ts,
                utc_offset_min=_offset(e),
                value_mgdl=sgv,
                source=SOURCE,
                device=e.get("device"),
                external_id=e.get("identifier") or e.get("_id"),
            )
        )
    boluses, basal, events = [], [], []
    for t in treatments:
        ts = _doc_ts(t)
        if ts is None:
            continue
        off = _offset(t)
        etype = (t.get("eventType") or "").lower()
        insulin = _number(t.get("insulin"))
        carbs = _number(t.get("carbs"))
        entered_by = (t.get("enteredBy") or "").lower()
        if insulin:
            automated = bool(
                "smb" in etype
                or t.get("isSMB")
                or (entered_by in ("loop", "openaps", "androidaps", "trio") and not carbs)
            )
            extended = bool(
                t.get("duration") and t.get("splitExt") and "combo" not in etype and "meal" not in etype
            )
            kind = "automated" if automated else "extended" if extended else "normal"
            if automated:
                reason = "auto"
            elif carbs:
                reason = "food_correction" if "correction" in etype else "food"
            else:
                reason = "correction" if "correction" in etype else "unknown"
            boluses.append(
                Bolus(
                    ts=ts,
                    utc_offset_min=off,
                    units_total=insulin,
                    units_normal=insulin,
                    kind=kind,
                    reason=reason,
                    carbs_entered=carbs,
                    bg_entered=_number(t.get("glucose")),
                    status=t.get("enteredBy"),
                    source=SOURCE,
                    external_id=t.get("identifier") or t.get("_id"),
                )
            )
        elif carbs:
            events.append(
                PumpEvent(
                    ts=ts,
                    utc_offset_min=off,
                    kind="carbs_logged",
                    value=carbs,
                    notes=t.get("notes"),
                    source=SOURCE,
                )
            )
        if "temp basal" in etype and _number(t.get("duration")):
            rate = _number(t.get("absolute"))
            if rate is None:
                rate = _number(t.get("rate"))
            dur = _number(t.get("duration"))
            if rate is not None and dur:
                basal.append(
                    BasalDose(
                        ts=ts,
                        utc_offset_min=off,
                        units=round(rate * dur / 60, 4),
                        duration_min=dur,
                        rate_uph=rate,
                        is_temp=True,
                        source=SOURCE,
                    )
                )
        for key, kind in _EVENT_KINDS.items():
            if key in etype:
                events.append(
                    PumpEvent(
                        ts=ts,
                        utc_offset_min=off,
                        kind=kind,
                        value=_number(t.get("glucose")) if kind == "bg_meter" else None,
                        duration_min=int(_number(t.get("duration")) or 0) or None,
                        notes=t.get("notes"),
                        source=SOURCE,
                    )
                )
                break
    return readings, boluses, basal, events


def _store(session: Session, result: ImportResult, models: tuple[list, list, list, list]) -> None:
    readings, boluses, basal, events = models
    upsert_glucose(session, readings, result)
    upsert_boluses(session, boluses, result)
    upsert_basal(session, basal, result)
    upsert_events(session, events, result)


def import_nightscout_files(session: Session, paths: list[str | Path]) -> ImportResult:
    """Import Nightscout JSON exports from disk (plain or gzipped): entries*.json, treatments*.json.
    Works for Nightscout backups and OpenAPS Data Commons donor folders."""
    result = ImportResult(source=SOURCE, filename=", ".join(str(p) for p in paths)[:300])
    entries: list[dict] = []
    treatments: list[dict] = []
    for p in paths:
        p = Path(p)
        target = entries if "entries" in p.name else treatments if "treatments" in p.name else None
        if target is None:
            result.warnings.append(f"ignored {p.name} (not entries/treatments)")
            continue
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("result", [data])
        target.extend(d for d in data if isinstance(d, dict))
    _store(session, result, docs_to_models(entries, treatments))
    result.log(session)
    session.flush()
    return result


def import_nightscout(
    session: Session,
    since: datetime | None = None,
    until: datetime | None = None,
    base_url: str | None = None,
    token: str | None = None,
) -> ImportResult:
    if not (base_url or settings.nightscout_url):
        raise ValueError("NIGHTSCOUT_URL is not configured")
    until = until or now_utc()
    if since is None:
        state = session.get(Setting, "nightscout_last_sync")
        since = (
            datetime.fromisoformat(state.value) - timedelta(hours=6)
            if state and state.value
            else until - timedelta(days=30)
        )
    result = ImportResult(source=SOURCE, filename=base_url or settings.nightscout_url)
    with _client(base_url, token) as client:
        entries = _fetch(client, "entries", since, until)
        treatments = _fetch(client, "treatments", since, until)
    _store(session, result, docs_to_models(entries, treatments))
    state = session.get(Setting, "nightscout_last_sync") or Setting(key="nightscout_last_sync")
    state.value = until.isoformat()
    session.add(state)
    result.log(session)
    session.flush()
    return result
