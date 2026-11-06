"""Nightscout API v3 reader: entries (CGM), treatments (boluses, carbs, site changes), devicestatus.

This is the easiest "live" path for Tandem users: run jwoglom/tconnectsync against Tandem Source and
point Hypovisor at the resulting Nightscout. Loop/Trio/AAPS users get everything here as well.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BasalDose, Bolus, GlucoseReading, PumpEvent, Setting
from ..timeutil import now_utc
from .base import ImportResult, upsert_basal, upsert_boluses, upsert_events, upsert_glucose

SOURCE = "nightscout"


def _client(base_url: str | None, token: str | None) -> httpx.Client:
    base = (base_url or settings.nightscout_url).rstrip("/")
    tok = token or settings.nightscout_token
    headers = {"Accept": "application/json"}
    params = {}
    if tok:
        params["token"] = tok
    return httpx.Client(base_url=base + "/api/v3", headers=headers, params=params, timeout=30)


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _offset(doc: dict) -> int:
    return int(doc.get("utcOffset") or 0)


def _fetch(client: httpx.Client, collection: str, since: datetime, until: datetime) -> list[dict]:
    docs, limit, skip = [], 1000, 0
    lo, hi = (
        int(since.replace(tzinfo=timezone.utc).timestamp() * 1000),
        int(until.replace(tzinfo=timezone.utc).timestamp() * 1000),
    )
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
    readings = []
    for e in entries:
        if e.get("type", "sgv") != "sgv" or e.get("sgv") is None:
            continue
        readings.append(
            GlucoseReading(
                ts=_ms_to_dt(e["date"]),
                utc_offset_min=_offset(e),
                value_mgdl=float(e["sgv"]),
                source=SOURCE,
                device=e.get("device"),
                external_id=e.get("identifier") or e.get("_id"),
            )
        )
    boluses, basal, events = [], [], []
    for t in treatments:
        ts = (
            _ms_to_dt(t["date"])
            if "date" in t
            else datetime.fromisoformat(t["created_at"].replace("Z", "")).replace(tzinfo=None)
        )
        off = _offset(t)
        etype = (t.get("eventType") or "").lower()
        insulin = t.get("insulin")
        carbs = t.get("carbs")
        if insulin:
            automated = (
                "smb" in etype
                or t.get("isSMB")
                or (t.get("enteredBy") or "").lower() in ("loop", "openaps", "androidaps", "trio")
                and not carbs
            )
            kind = (
                "automated"
                if automated
                else "extended"
                if t.get("duration") and "combo" not in etype and "meal" not in etype and t.get("splitExt")
                else "normal"
            )
            reason = (
                "auto"
                if automated
                else "food_correction"
                if carbs and "correction" in etype
                else "food"
                if carbs
                else "correction"
                if "correction" in etype
                else "unknown"
            )
            boluses.append(
                Bolus(
                    ts=ts,
                    utc_offset_min=off,
                    units_total=float(insulin),
                    units_normal=float(insulin),
                    kind=kind,
                    reason=reason,
                    carbs_entered=float(carbs) if carbs else None,
                    bg_entered=float(t["glucose"])
                    if t.get("glucose") and str(t["glucose"]).replace(".", "").isdigit()
                    else None,
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
                    value=float(carbs),
                    notes=t.get("notes"),
                    source=SOURCE,
                )
            )
        if "temp basal" in etype and t.get("duration"):
            rate = t.get("absolute") if t.get("absolute") is not None else t.get("rate")
            if rate is not None:
                dur = float(t["duration"])
                basal.append(
                    BasalDose(
                        ts=ts,
                        utc_offset_min=off,
                        units=round(float(rate) * dur / 60, 4),
                        duration_min=dur,
                        rate_uph=float(rate),
                        is_temp=True,
                        source=SOURCE,
                    )
                )
        kind_map = {
            "site change": "site_change",
            "sensor start": "sensor_start",
            "sensor change": "sensor_start",
            "insulin change": "cartridge_change",
            "exercise": "exercise",
            "note": "note",
            "bg check": "bg_meter",
            "pump battery change": "note",
        }
        for key, kind in kind_map.items():
            if key in etype:
                events.append(
                    PumpEvent(
                        ts=ts,
                        utc_offset_min=off,
                        kind=kind,
                        value=float(t["glucose"]) if kind == "bg_meter" and t.get("glucose") else None,
                        duration_min=int(t["duration"]) if t.get("duration") else None,
                        notes=t.get("notes"),
                        source=SOURCE,
                    )
                )
                break
    upsert_glucose(session, readings, result)
    upsert_boluses(session, boluses, result)
    upsert_basal(session, basal, result)
    upsert_events(session, events, result)
    state = session.get(Setting, "nightscout_last_sync") or Setting(key="nightscout_last_sync")
    state.value = until.isoformat()
    session.add(state)
    result.log(session)
    session.flush()
    return result
