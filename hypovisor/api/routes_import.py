from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.meal_response import recompute_all
from ..db import get_session
from ..importers import import_clarity_csv, import_tandem_source
from ..models import ImportLog

router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/clarity")
async def upload_clarity(file: UploadFile = File(...), session: Session = Depends(get_session)):
    data = await file.read()
    try:
        result = import_clarity_csv(session, text=data.decode("utf-8-sig", errors="replace"))
        result.filename = file.filename
        n = recompute_all(session)
        session.commit()
    except ValueError as e:
        session.rollback()
        raise HTTPException(400, str(e)) from e
    return {"result": result.as_dict(), "meal_responses_recomputed": n}


@router.post("/tandem")
async def upload_tandem(file: UploadFile = File(...), session: Session = Depends(get_session)):
    data = await file.read()
    suffix = ".zip" if (file.filename or "").lower().endswith(".zip") else ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        result = import_tandem_source(session, tmp_path)
        result.filename = file.filename
        n = recompute_all(session)
        session.commit()
    except ValueError as e:
        session.rollback()
        raise HTTPException(400, str(e)) from e
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"result": result.as_dict(), "meal_responses_recomputed": n}


@router.get("/log")
def import_log(session: Session = Depends(get_session)):
    rows = session.scalars(select(ImportLog).order_by(ImportLog.ts.desc()).limit(50)).all()
    return [
        {"id": r.id, "ts": r.ts.isoformat(), "source": r.source, "filename": r.filename, "summary": r.summary}
        for r in rows
    ]
