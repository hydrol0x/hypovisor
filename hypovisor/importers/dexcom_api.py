"""Dexcom Developer API v3 client (official, OAuth 2.0, data delayed ~1h US / ~3h elsewhere).

Flow: GET /api/sync/dexcom/authorize -> Dexcom login -> redirect to DEXCOM_REDIRECT_URI with ?code= ->
we exchange it for tokens (stored in the settings table) -> POST /api/sync/dexcom pulls EGVs since last sync.
Use https://sandbox-api.dexcom.com as DEXCOM_BASE_URL to test with simulated users.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GlucoseReading, PumpEvent, Setting
from ..timeutil import now_utc
from .base import ImportResult, upsert_events, upsert_glucose

SOURCE = "dexcom_api"
TOKEN_KEY = "dexcom_tokens"


def authorize_url(state: str = "hypovisor") -> str:
    q = {
        "client_id": settings.dexcom_client_id,
        "redirect_uri": settings.dexcom_redirect_uri,
        "response_type": "code",
        "scope": "offline_access",
        "state": state,
    }
    return f"{settings.dexcom_base_url}/v2/oauth2/login?{urlencode(q)}"


def _token_request(data: dict) -> dict:
    r = httpx.post(
        f"{settings.dexcom_base_url}/v2/oauth2/token",
        data={**data, "client_id": settings.dexcom_client_id, "client_secret": settings.dexcom_client_secret},
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    tok["expires_at"] = (now_utc() + timedelta(seconds=int(tok.get("expires_in", 7200)) - 60)).isoformat()
    return tok


def exchange_code(session: Session, code: str) -> dict:
    tok = _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.dexcom_redirect_uri}
    )
    s = session.get(Setting, TOKEN_KEY) or Setting(key=TOKEN_KEY)
    s.value = tok
    session.add(s)
    session.flush()
    return tok


def _access_token(session: Session) -> str:
    s = session.get(Setting, TOKEN_KEY)
    if not s or not s.value:
        raise ValueError("Dexcom not authorized yet; visit /api/sync/dexcom/authorize")
    tok = dict(s.value)
    if datetime.fromisoformat(tok["expires_at"]) <= now_utc():
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        s.value = tok
        session.flush()
    return tok["access_token"]


def _parse_dexcom_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)


def import_dexcom_api(
    session: Session, since: datetime | None = None, until: datetime | None = None
) -> ImportResult:
    until = until or now_utc()
    if since is None:
        st = session.get(Setting, "dexcom_last_sync")
        since = (
            datetime.fromisoformat(st.value) - timedelta(hours=3)
            if st and st.value
            else until - timedelta(days=30)
        )
    token = _access_token(session)
    headers = {"Authorization": f"Bearer {token}"}
    result = ImportResult(source=SOURCE, filename=settings.dexcom_base_url)
    fmt = "%Y-%m-%dT%H:%M:%S"
    readings, events = [], []
    cursor = since
    while cursor < until:
        chunk_end = min(cursor + timedelta(days=30), until)
        r = httpx.get(
            f"{settings.dexcom_base_url}/v3/users/self/egvs",
            params={"startDate": cursor.strftime(fmt), "endDate": chunk_end.strftime(fmt)},
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        for rec in r.json().get("records", []):
            if rec.get("value") is None and not rec.get("displayValue"):
                continue
            sys_t = _parse_dexcom_time(rec["systemTime"])
            disp_t = _parse_dexcom_time(rec["displayTime"]) if rec.get("displayTime") else sys_t
            offset = (
                int(round((disp_t - sys_t).total_seconds() / 60))
                if "displayTime" in rec and "+" not in rec["displayTime"] and "Z" not in rec["displayTime"]
                else 0
            )
            readings.append(
                GlucoseReading(
                    ts=sys_t,
                    utc_offset_min=offset,
                    value_mgdl=float(rec["value"]) if rec.get("value") is not None else None,
                    raw_label=rec.get("displayValue") if rec.get("value") is None else None,
                    trend_rate=rec.get("trendRate"),
                    source=SOURCE,
                    device=rec.get("displayDevice"),
                    external_id=rec.get("recordId"),
                )
            )
        r = httpx.get(
            f"{settings.dexcom_base_url}/v3/users/self/events",
            params={"startDate": cursor.strftime(fmt), "endDate": chunk_end.strftime(fmt)},
            headers=headers,
            timeout=60,
        )
        if r.status_code == 200:
            for ev in r.json().get("records", []):
                if ev.get("eventStatus") == "deleted":
                    continue
                kind = {
                    "carbs": "carbs_logged",
                    "insulin": "insulin_logged",
                    "exercise": "exercise",
                    "health": "note",
                }.get((ev.get("eventType") or "").lower())
                if kind:
                    events.append(
                        PumpEvent(
                            ts=_parse_dexcom_time(ev["systemTime"]),
                            kind=kind,
                            value=ev.get("value"),
                            notes=ev.get("eventSubType"),
                            source=SOURCE,
                        )
                    )
        cursor = chunk_end
    upsert_glucose(session, readings, result)
    upsert_events(session, events, result)
    st = session.get(Setting, "dexcom_last_sync") or Setting(key="dexcom_last_sync")
    st.value = until.isoformat()
    session.add(st)
    result.log(session)
    session.flush()
    return result
