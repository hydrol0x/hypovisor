"""Structured output schema for meal estimation. Claude returns exactly this shape."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ItemEstimate(BaseModel):
    name: str = Field(description="Short canonical dish or ingredient name, e.g. 'chicken burrito bowl'")
    portion_desc: str = Field(description="Human portion, e.g. '1 cup', '2 slices', 'large bowl'")
    grams: float = Field(description="Estimated weight in grams of this item as eaten")
    carbs_g: float = Field(description="Best estimate of net-of-nothing total carbohydrate in grams")
    carbs_low_g: float = Field(description="Plausible low bound for carbs in grams")
    carbs_high_g: float = Field(description="Plausible high bound for carbs in grams")
    fat_g: float
    protein_g: float
    fiber_g: float
    confidence: float = Field(description="0 to 1 confidence in the carb estimate")
    gi_band: str = Field(description="low, medium, or high glycemic index band")
    matched_known_food: str | None = Field(
        default=None, description="Name of a known personal food this item is, if any"
    )
    reasoning: str = Field(description="One sentence on how the portion and carbs were judged")


class MealEstimate(BaseModel):
    meal_name: str = Field(description="Short name for the whole meal, e.g. 'Chipotle bowl with chips'")
    items: list[ItemEstimate]
    total_carbs_g: float
    total_carbs_low_g: float
    total_carbs_high_g: float
    total_fat_g: float
    total_protein_g: float
    total_fiber_g: float
    assumptions: list[str] = Field(
        description="Assumptions made about hidden ingredients, sauces, portion size"
    )
    questions: list[str] = Field(description="Questions whose answers would most reduce carb uncertainty")
    overall_confidence: float
    tags: list[str] = Field(
        description="3-6 lowercase tags: cuisine, meal type, dominant macro, e.g. 'pizza', 'high-fat'"
    )
