from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..analytics.meal_response import recompute_all
from ..config import settings
from ..db import get_session
from ..importers.dexcom_api import authorize_url, exchange_code, import_dexcom_api
from ..importers.nightscout import import_nightscout

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("/nightscout")
def sync_nightscout(since: datetime | None = None, session: Session = Depends(get_session)):
    try:
        result = import_nightscout(session, since=since)
        n = recompute_all(session)
        session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        session.rollback()
        raise HTTPException(502, f"Nightscout sync failed: {e}") from e
    return {"result": result.as_dict(), "meal_responses_recomputed": n}


@router.get("/dexcom/authorize")
def dexcom_authorize():
    if not settings.dexcom_client_id:
        raise HTTPException(400, "DEXCOM_CLIENT_ID not configured")
    return RedirectResponse(authorize_url())


@router.get("/dexcom/callback")
def dexcom_callback(code: str, session: Session = Depends(get_session)):
    try:
        exchange_code(session, code)
        session.commit()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Token exchange failed: {e}") from e
    return RedirectResponse("/import")


@router.post("/dexcom")
def sync_dexcom(since: datetime | None = None, session: Session = Depends(get_session)):
    try:
        result = import_dexcom_api(session, since=since)
        n = recompute_all(session)
        session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        session.rollback()
        raise HTTPException(502, f"Dexcom sync failed: {e}") from e
    return {"result": result.as_dict(), "meal_responses_recomputed": n}
