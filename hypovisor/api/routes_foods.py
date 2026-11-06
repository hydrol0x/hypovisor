from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.patterns import food_leaderboard, food_outcomes
from ..db import get_session
from ..food.knowledge_base import find_matches
from ..food.reference import openfoodfacts_barcode, usda_search
from ..models import Food

router = APIRouter(prefix="/api", tags=["foods"])


def _food_json(f: Food) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "aliases": f.aliases or [],
        "category": f.category,
        "serving_desc": f.serving_desc,
        "serving_grams": f.serving_grams,
        "carbs_g": f.carbs_g,
        "fat_g": f.fat_g,
        "protein_g": f.protein_g,
        "fiber_g": f.fiber_g,
        "kcal": f.kcal,
        "gi_band": f.gi_band,
        "provenance": f.provenance,
        "confidence": f.confidence,
        "reference_id": f.reference_id,
        "notes": f.notes,
        "times_eaten": f.times_eaten,
    }


@router.get("/foods")
def list_foods(q: str | None = None, session: Session = Depends(get_session)):
    if q:
        return [{**_food_json(f), "score": s} for f, s in find_matches(session, q, limit=20, threshold=0.3)]
    return [_food_json(f) for f in session.scalars(select(Food).order_by(Food.times_eaten.desc(), Food.name))]


@router.get("/foods/leaderboard")
def leaderboard(session: Session = Depends(get_session)):
    return food_leaderboard(session)


@router.get("/foods/{food_id}")
def get_food(food_id: int, session: Session = Depends(get_session)):
    f = session.get(Food, food_id)
    if f is None:
        raise HTTPException(404)
    return {**_food_json(f), "outcomes": food_outcomes(session, food_id)}


class FoodIn(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    category: str | None = None
    serving_desc: str | None = None
    serving_grams: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    protein_g: float | None = None
    fiber_g: float | None = None
    kcal: float | None = None
    gi_band: str | None = None
    notes: str | None = None


@router.post("/foods")
def create_food(body: FoodIn, session: Session = Depends(get_session)):
    if not body.name:
        raise HTTPException(400, "name required")
    f = Food(name=body.name, provenance="user", confidence=1.0, aliases=body.aliases or [])
    for k, v in body.model_dump(exclude={"name", "aliases"}).items():
        if v is not None:
            setattr(f, k, v)
    session.add(f)
    session.commit()
    return _food_json(f)


@router.put("/foods/{food_id}")
def update_food(food_id: int, body: FoodIn, session: Session = Depends(get_session)):
    f = session.get(Food, food_id)
    if f is None:
        raise HTTPException(404)
    changed_values = False
    for k, v in body.model_dump().items():
        if v is not None:
            setattr(f, k, v)
            if k in ("carbs_g", "fat_g", "protein_g", "fiber_g", "serving_desc", "serving_grams"):
                changed_values = True
    if changed_values:
        f.provenance, f.confidence = "corrected", 1.0
    session.commit()
    return _food_json(f)


@router.delete("/foods/{food_id}")
def delete_food(food_id: int, session: Session = Depends(get_session)):
    f = session.get(Food, food_id)
    if f:
        session.delete(f)
        session.commit()
    return {"ok": True}


@router.get("/reference/usda")
def reference_usda(q: str):
    try:
        return usda_search(q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"USDA lookup failed: {e}") from e


@router.get("/reference/barcode/{code}")
def reference_barcode(code: str):
    try:
        r = openfoodfacts_barcode(code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Open Food Facts lookup failed: {e}") from e
    if r is None:
        raise HTTPException(404, "barcode not found")
    return r
