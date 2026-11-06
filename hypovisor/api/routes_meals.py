from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..analytics.meal_response import compute_response, response_curve
from ..analytics.patterns import recent_bolus_context
from ..config import settings
from ..db import get_session
from ..food.estimator import estimate_meal, parse_quick_text
from ..food.knowledge_base import candidates_for_prompt, find_matches, learn_from_item, recent_meal_names
from ..models import Meal, MealItem
from ..timeutil import local_naive_to_utc, now_utc, utc_to_local

router = APIRouter(prefix="/api/meals", tags=["meals"])


def _meal_json(m: Meal, session: Session | None = None, with_curve: bool = False) -> dict:
    r = m.response
    d = {
        "id": m.id,
        "ts": m.ts.isoformat(),
        "local": utc_to_local(m.ts, m.utc_offset_min).isoformat(),
        "description": m.description,
        "notes": m.notes,
        "tags": m.tags or [],
        "status": m.status,
        "photo_url": f"/photos/{Path(m.photo_path).name}" if m.photo_path else None,
        "values_source": m.values_source,
        "carbs_g": m.carbs_g,
        "carbs_low_g": m.carbs_low_g,
        "carbs_high_g": m.carbs_high_g,
        "fat_g": m.fat_g,
        "protein_g": m.protein_g,
        "fiber_g": m.fiber_g,
        "fpu": m.fpu,
        "items": [
            {
                "id": i.id,
                "food_id": i.food_id,
                "food_name": i.food.name if i.food else None,
                "name": i.name,
                "portion_desc": i.portion_desc,
                "grams": i.grams,
                "carbs_g": i.carbs_g,
                "carbs_low_g": i.carbs_low_g,
                "carbs_high_g": i.carbs_high_g,
                "fat_g": i.fat_g,
                "protein_g": i.protein_g,
                "fiber_g": i.fiber_g,
                "confidence": i.confidence,
                "values_source": i.values_source,
            }
            for i in m.items
        ],
        "estimate": m.estimate_json,
        "response": None
        if r is None
        else {
            k: getattr(r, k)
            for k in (
                "score",
                "score_reason",
                "n_readings",
                "coverage",
                "baseline_mgdl",
                "baseline_trend",
                "peak_mgdl",
                "time_to_peak_min",
                "min_mgdl",
                "delta_1h",
                "delta_2h",
                "delta_3h",
                "delta_4h",
                "iauc_2h",
                "iauc_4h",
                "tir_0_2h",
                "tir_2_6h",
                "tir_0_6h",
                "low_event",
                "late_rise",
                "bolus_units",
                "prebolus_min",
                "carbs_entered_on_pump",
                "iob_at_meal",
                "basal_rate_at_meal",
                "insulin_6h",
                "local_hour",
            )
        },
    }
    if with_curve and session is not None:
        d["curve"] = response_curve(session, m)
    return d


@router.get("")
def list_meals(limit: int = 50, offset: int = 0, session: Session = Depends(get_session)):
    meals = session.scalars(
        select(Meal)
        .options(selectinload(Meal.items), selectinload(Meal.response))
        .order_by(Meal.ts.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [_meal_json(m) for m in meals]


@router.post("/estimate")
async def estimate(
    description: str | None = Form(None),
    local_ts: str | None = Form(None),
    photo: UploadFile | None = File(None),
    session: Session = Depends(get_session),
):
    """Create a draft meal from a photo and/or description and return the model's structured estimate
    plus knowledge-base matches. Nothing is final until the client confirms via PUT /api/meals/{id}."""
    if not description and photo is None:
        raise HTTPException(400, "Provide a description or a photo")
    when = datetime.fromisoformat(local_ts) if local_ts else utc_to_local(now_utc())
    ts, off = local_naive_to_utc(when)
    photo_path = None
    if photo is not None and photo.filename:
        settings.photo_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(photo.filename).suffix.lower() or ".jpg"
        photo_path = settings.photo_dir / f"{ts.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
        photo_path.write_bytes(await photo.read())
    meal = Meal(
        ts=ts,
        utc_offset_min=off,
        description=description,
        photo_path=str(photo_path) if photo_path else None,
        status="draft",
    )
    session.add(meal)
    session.flush()

    known = candidates_for_prompt(session, description)
    estimate_obj, error = None, None
    quick = parse_quick_text(description) if description else None
    if quick and photo_path is None:
        estimate_obj = quick
    else:
        try:
            estimate_obj = estimate_meal(description, photo_path, known, recent_meal_names(session))
        except Exception as e:  # noqa: BLE001 - surface to the UI, allow manual entry
            error = str(e)
            if quick:
                estimate_obj = quick
    if estimate_obj is not None:
        meal.estimate_json = estimate_obj.model_dump()
        meal.description = meal.description or estimate_obj.meal_name
        meal.tags = estimate_obj.tags
        meal.carbs_g, meal.carbs_low_g, meal.carbs_high_g = (
            estimate_obj.total_carbs_g,
            estimate_obj.total_carbs_low_g,
            estimate_obj.total_carbs_high_g,
        )
        meal.fat_g, meal.protein_g, meal.fiber_g = (
            estimate_obj.total_fat_g,
            estimate_obj.total_protein_g,
            estimate_obj.total_fiber_g,
        )
        for it in estimate_obj.items:
            item = MealItem(
                name=it.name,
                portion_desc=it.portion_desc,
                grams=it.grams,
                carbs_g=it.carbs_g,
                carbs_low_g=it.carbs_low_g,
                carbs_high_g=it.carbs_high_g,
                fat_g=it.fat_g,
                protein_g=it.protein_g,
                fiber_g=it.fiber_g,
                confidence=it.confidence,
                values_source="model",
            )
            meal.items.append(item)
            match_name = it.matched_known_food or it.name
            matches = find_matches(session, match_name, limit=1, threshold=0.6)
            if matches:
                item.food = matches[0][0]
    session.commit()
    session.refresh(meal)
    out = _meal_json(meal)
    out["error"] = error
    out["kb_matches"] = {
        i.name: [
            {
                "id": f.id,
                "name": f.name,
                "score": s,
                "serving_desc": f.serving_desc,
                "carbs_g": f.carbs_g,
                "fat_g": f.fat_g,
                "protein_g": f.protein_g,
                "provenance": f.provenance,
                "times_eaten": f.times_eaten,
            }
            for f, s in find_matches(session, i.name, limit=3)
        ]
        for i in meal.items
    }
    out["insulin_context"] = recent_bolus_context(session, ts)
    return out


class ItemIn(BaseModel):
    id: int | None = None
    food_id: int | None = None
    name: str
    portion_desc: str | None = None
    grams: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    protein_g: float | None = None
    fiber_g: float | None = None
    user_corrected: bool = False


class MealIn(BaseModel):
    local_ts: datetime | None = None
    description: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    items: list[ItemIn] = []
    carbs_g: float | None = None
    fat_g: float | None = None
    protein_g: float | None = None
    fiber_g: float | None = None
    confirm: bool = True


def _apply(meal: Meal, body: MealIn, session: Session) -> None:
    if body.local_ts is not None:
        meal.ts, meal.utc_offset_min = local_naive_to_utc(body.local_ts)
    if body.description is not None:
        meal.description = body.description
    if body.notes is not None:
        meal.notes = body.notes
    if body.tags is not None:
        meal.tags = body.tags
    if body.items:
        existing = {i.id: i for i in meal.items}
        keep = []
        any_corrected = False
        for it in body.items:
            item = existing.get(it.id) if it.id else None
            if item is None:
                item = MealItem(name=it.name)
                meal.items.append(item)
            item.name = it.name
            item.portion_desc = it.portion_desc or item.portion_desc
            item.grams = it.grams if it.grams is not None else item.grams
            for k in ("carbs_g", "fat_g", "protein_g", "fiber_g"):
                v = getattr(it, k)
                if v is not None:
                    setattr(item, k, v)
            if it.user_corrected:
                item.values_source = "user"
                item.carbs_low_g = item.carbs_high_g = item.carbs_g
                any_corrected = True
            if it.food_id:
                from ..models import Food

                item.food = session.get(Food, it.food_id)
            keep.append(item)
        for item in list(meal.items):
            if item not in keep:
                meal.items.remove(item)
        meal.values_source = "mixed" if any_corrected else meal.values_source
        meal.carbs_g = round(sum(i.carbs_g or 0 for i in meal.items), 1)
        meal.fat_g = round(sum(i.fat_g or 0 for i in meal.items), 1)
        meal.protein_g = round(sum(i.protein_g or 0 for i in meal.items), 1)
        meal.fiber_g = round(sum(i.fiber_g or 0 for i in meal.items), 1)
        meal.carbs_low_g = round(
            sum((i.carbs_low_g if i.carbs_low_g is not None else i.carbs_g) or 0 for i in meal.items), 1
        )
        meal.carbs_high_g = round(
            sum((i.carbs_high_g if i.carbs_high_g is not None else i.carbs_g) or 0 for i in meal.items), 1
        )
    for k in ("carbs_g", "fat_g", "protein_g", "fiber_g"):
        v = getattr(body, k)
        if v is not None:
            setattr(meal, k, v)
            meal.values_source = "user"
            if k == "carbs_g":
                meal.carbs_low_g = meal.carbs_high_g = v
    if body.confirm:
        meal.status = "confirmed"
        for item in meal.items:
            learn_from_item(session, item, user_corrected=item.values_source == "user")


@router.post("")
def create_meal(body: MealIn, session: Session = Depends(get_session)):
    when = body.local_ts or utc_to_local(now_utc())
    ts, off = local_naive_to_utc(when)
    meal = Meal(ts=ts, utc_offset_min=off, status="draft", values_source="user")
    session.add(meal)
    session.flush()
    _apply(meal, body, session)
    compute_and_attach(session, meal)
    session.commit()
    session.refresh(meal)
    return _meal_json(meal, session, with_curve=True)


def compute_and_attach(session: Session, meal: Meal) -> None:
    resp = compute_response(session, meal)
    if meal.response is None:
        meal.response = resp


@router.get("/{meal_id}")
def get_meal(meal_id: int, session: Session = Depends(get_session)):
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404)
    out = _meal_json(meal, session, with_curve=True)
    out["kb_matches"] = {
        i.name: [
            {"id": f.id, "name": f.name, "score": s, "carbs_g": f.carbs_g, "serving_desc": f.serving_desc}
            for f, s in find_matches(session, i.name, limit=3)
        ]
        for i in meal.items
    }
    return out


@router.put("/{meal_id}")
def update_meal(meal_id: int, body: MealIn, session: Session = Depends(get_session)):
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404)
    _apply(meal, body, session)
    compute_and_attach(session, meal)
    session.commit()
    session.refresh(meal)
    return _meal_json(meal, session, with_curve=True)


@router.post("/{meal_id}/recompute")
def recompute(meal_id: int, session: Session = Depends(get_session)):
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404)
    compute_and_attach(session, meal)
    session.commit()
    session.refresh(meal)
    return _meal_json(meal, session, with_curve=True)


@router.delete("/{meal_id}")
def delete_meal(meal_id: int, session: Session = Depends(get_session)):
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404)
    if meal.photo_path:
        Path(meal.photo_path).unlink(missing_ok=True)
    session.delete(meal)
    session.commit()
    return {"ok": True}
