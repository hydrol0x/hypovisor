from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..forecast.model import GlucoseForecaster, evaluate_holdout

router = APIRouter(prefix="/api/forecast", tags=["forecast"])
_cache: dict = {}


def _model() -> GlucoseForecaster | None:
    if "m" not in _cache:
        _cache["m"] = GlucoseForecaster.load()
    return _cache["m"]


@router.get("")
def latest(session: Session = Depends(get_session)):
    m = _model()
    if m is None:
        return {"available": False, "reason": "no trained model; POST /api/forecast/train"}
    try:
        p = m.predict_latest(session)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    return {"trained_at": m.meta.get("trained_at"), **p}


@router.post("/train")
def train(days: int = Query(120, ge=7, le=730), session: Session = Depends(get_session)):
    m = GlucoseForecaster()
    try:
        meta = m.fit_from_db(session, days=days)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e
    path = m.save()
    _cache["m"] = m
    return {"saved_to": str(path), **meta}


@router.get("/evaluate")
def evaluate(days: int = Query(120, ge=7, le=730), session: Session = Depends(get_session)):
    try:
        return evaluate_holdout(session, days=days)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e
