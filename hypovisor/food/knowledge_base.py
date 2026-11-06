"""Personal food knowledge base: fuzzy matching, learning from corrections, usage counts."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Food, Meal, MealItem

_STOP = {
    "a",
    "an",
    "the",
    "of",
    "with",
    "and",
    "my",
    "some",
    "large",
    "small",
    "medium",
    "bowl",
    "plate",
    "cup",
    "slice",
    "slices",
    "piece",
    "pieces",
}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP}


def similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and character similarity; 1.0 is identical."""
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    contains = 1.0 if ta and tb and (ta <= tb or tb <= ta) else 0.0
    return max(0.6 * jac + 0.4 * seq, 0.85 * contains)


def find_matches(
    session: Session, query: str, limit: int = 5, threshold: float = 0.45
) -> list[tuple[Food, float]]:
    foods = list(session.scalars(select(Food)))
    scored = []
    for f in foods:
        names = [f.name] + list(f.aliases or [])
        score = max(similarity(query, n) for n in names)
        if score >= threshold:
            scored.append((f, round(score, 3)))
    scored.sort(key=lambda x: (-x[1], -(x[0].times_eaten or 0)))
    return scored[:limit]


def candidates_for_prompt(session: Session, description: str | None, limit: int = 8) -> list[dict]:
    """Known foods worth showing the model: best name matches for the description plus the most-eaten foods."""
    seen: dict[int, Food] = {}
    if description:
        for f, _ in find_matches(session, description, limit=limit, threshold=0.3):
            seen[f.id] = f
    for f in session.scalars(select(Food).order_by(Food.times_eaten.desc()).limit(limit)):
        seen.setdefault(f.id, f)
    return [
        {
            "id": f.id,
            "name": f.name,
            "serving_desc": f.serving_desc,
            "carbs_g": f.carbs_g,
            "fat_g": f.fat_g,
            "protein_g": f.protein_g,
            "times_eaten": f.times_eaten,
        }
        for f in list(seen.values())[: limit + 4]
    ]


def recent_meal_names(session: Session, limit: int = 8) -> list[str]:
    rows = session.scalars(
        select(Meal.description).where(Meal.description.is_not(None)).order_by(Meal.ts.desc()).limit(limit)
    ).all()
    return [r[:60] for r in rows if r]


def get_or_create_food(
    session: Session,
    name: str,
    *,
    serving_desc: str = "1 serving",
    serving_grams: float | None = None,
    carbs_g: float = 0,
    fat_g: float = 0,
    protein_g: float = 0,
    fiber_g: float = 0,
    provenance: str = "user",
    confidence: float | None = None,
    gi_band: str | None = None,
    category: str | None = None,
) -> Food:
    name = name.strip()
    existing = session.scalar(select(Food).where(Food.name == name))
    if existing:
        return existing
    matches = find_matches(session, name, limit=1, threshold=0.92)
    if matches:
        return matches[0][0]
    food = Food(
        name=name,
        serving_desc=serving_desc,
        serving_grams=serving_grams,
        carbs_g=carbs_g,
        fat_g=fat_g,
        protein_g=protein_g,
        fiber_g=fiber_g,
        provenance=provenance,
        confidence=confidence,
        gi_band=gi_band,
        category=category,
        aliases=[],
    )
    session.add(food)
    session.flush()
    return food


def learn_from_item(session: Session, item: MealItem, user_corrected: bool) -> Food:
    """Link a confirmed meal item to a food, creating or updating the food's values.

    - A user-corrected item overwrites the food's values (provenance=corrected).
    - A model-estimated item only fills in a food that has no better provenance.
    """
    food = item.food or get_or_create_food(
        session,
        item.name,
        serving_desc=item.portion_desc or "1 serving",
        serving_grams=item.grams,
        carbs_g=item.carbs_g or 0,
        fat_g=item.fat_g or 0,
        protein_g=item.protein_g or 0,
        fiber_g=item.fiber_g or 0,
        provenance="corrected" if user_corrected else "model",
        confidence=item.confidence,
    )
    if user_corrected:
        food.serving_desc = item.portion_desc or food.serving_desc
        food.serving_grams = item.grams or food.serving_grams
        food.carbs_g = item.carbs_g if item.carbs_g is not None else food.carbs_g
        food.fat_g = item.fat_g if item.fat_g is not None else food.fat_g
        food.protein_g = item.protein_g if item.protein_g is not None else food.protein_g
        food.fiber_g = item.fiber_g if item.fiber_g is not None else food.fiber_g
        food.provenance = "corrected"
        food.confidence = 1.0
    elif food.provenance == "model" and item.confidence and (food.confidence or 0) < item.confidence:
        food.carbs_g, food.fat_g, food.protein_g, food.fiber_g = (
            item.carbs_g or 0,
            item.fat_g or 0,
            item.protein_g or 0,
            item.fiber_g or 0,
        )
        food.confidence = item.confidence
    if item.name.lower() != food.name.lower() and item.name not in (food.aliases or []):
        food.aliases = [*(food.aliases or []), item.name][:20]
    food.times_eaten = (food.times_eaten or 0) + 1
    item.food = food
    return food
