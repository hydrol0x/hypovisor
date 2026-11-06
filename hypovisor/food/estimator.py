"""Meal estimation with Claude (vision + text) using structured outputs.

The model's answer is a *prior with an uncertainty band*, never the final number. The UI always asks the
user to confirm or correct, and the knowledge base keeps corrected values for next time.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from ..config import settings
from .schemas import MealEstimate

SYSTEM_PROMPT = """You are a careful dietitian assisting a person with type 1 diabetes who doses insulin based on your carbohydrate estimates. Over- and under-estimates both cause harm, so:
- Estimate each distinct item separately with a realistic portion in grams.
- Give honest low/high carbohydrate bounds. Sauces, dressings, breading, rice and bread portions are the usual sources of error; widen the bounds when they are hidden or ambiguous.
- Prefer values consistent with standard food composition data (USDA FoodData Central) for the item type.
- If the person's description or a listed known food matches an item, use the known food's values and set matched_known_food to its exact name.
- If the user states a quantity or a carb count, trust it over the image.
- Never inflate confidence. 0.9 means you would bet on +/-10 g; 0.4 means +/-40%.
- Keep assumptions and questions short and specific."""


def _image_block(photo_path: Path) -> dict:
    media = mimetypes.guess_type(str(photo_path))[0] or "image/jpeg"
    data = base64.standard_b64encode(photo_path.read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def build_user_content(
    description: str | None, photo_path: Path | None, known_foods: list[dict], recent_meals: list[str]
) -> list[dict]:
    content: list[dict] = []
    if photo_path is not None:
        content.append(_image_block(photo_path))
    parts = []
    if description:
        parts.append(f"User's description: {description.strip()}")
    else:
        parts.append("No description given; estimate from the photo.")
    if known_foods:
        lines = [
            f"- {f['name']}: {f['serving_desc']} = {f['carbs_g']:.0f} g carbs, {f['fat_g']:.0f} g fat, {f['protein_g']:.0f} g protein"
            + (f" (eaten {f['times_eaten']}x)" if f.get("times_eaten") else "")
            for f in known_foods
        ]
        parts.append(
            "Known personal foods that may match (use their values if they are the same thing):\n"
            + "\n".join(lines)
        )
    if recent_meals:
        parts.append("Recent meal names for context: " + "; ".join(recent_meals[:8]))
    parts.append("Return the structured estimate.")
    content.append({"type": "text", "text": "\n\n".join(parts)})
    return content


def estimate_meal(
    description: str | None = None,
    photo_path: str | Path | None = None,
    known_foods: list[dict] | None = None,
    recent_meals: list[str] | None = None,
    model: str | None = None,
) -> MealEstimate:
    """Call Claude and return a validated MealEstimate. Raises RuntimeError when no API key is configured."""
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; cannot estimate. Enter values manually or configure the key."
        )
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    content = build_user_content(
        description, Path(photo_path) if photo_path else None, known_foods or [], recent_meals or []
    )
    response = client.messages.parse(
        model=model or settings.anthropic_model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=MealEstimate,
        output_config={"effort": "medium"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined to analyze this input.")
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError("The model returned no structured estimate.")
    return parsed


def parse_quick_text(text: str) -> MealEstimate | None:
    """Offline fallback for inputs like '45g carbs pizza' or 'carbs: 60'. Returns a minimal estimate or None."""
    import re

    m = re.search(r"(\d+(?:\.\d+)?)\s*g?\s*(?:carbs?|cho|carbohydrates?)", text, re.I) or re.search(
        r"carbs?\s*[:=]?\s*(\d+(?:\.\d+)?)", text, re.I
    )
    if not m:
        return None
    carbs = float(m.group(1))
    name = (
        re.sub(r"(\d+(?:\.\d+)?)\s*g?\s*(?:carbs?|cho|carbohydrates?)", "", text, flags=re.I).strip(" ,.-")
        or "meal"
    )
    return MealEstimate(
        meal_name=name[:80],
        items=[
            {
                "name": name[:80],
                "portion_desc": "as described",
                "grams": 0,
                "carbs_g": carbs,
                "carbs_low_g": carbs,
                "carbs_high_g": carbs,
                "fat_g": 0,
                "protein_g": 0,
                "fiber_g": 0,
                "confidence": 1.0,
                "gi_band": "medium",
                "matched_known_food": None,
                "reasoning": "carbs stated by user",
            }
        ],
        total_carbs_g=carbs,
        total_carbs_low_g=carbs,
        total_carbs_high_g=carbs,
        total_fat_g=0,
        total_protein_g=0,
        total_fiber_g=0,
        assumptions=["Carbohydrates stated by the user; other macros unknown"],
        questions=[],
        overall_confidence=1.0,
        tags=[],
    )
