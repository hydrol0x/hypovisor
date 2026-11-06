"""Reference nutrient databases: USDA FoodData Central (needs free API key) and Open Food Facts (barcodes)."""

from __future__ import annotations

import httpx

from ..config import settings

FDC_URL = "https://api.nal.usda.gov/fdc/v1"
OFF_URL = "https://world.openfoodfacts.org/api/v2"

_FDC_NUTRIENTS = {1005: "carbs_g", 1004: "fat_g", 1003: "protein_g", 1079: "fiber_g", 1008: "kcal"}


def usda_search(query: str, limit: int = 8) -> list[dict]:
    if not settings.usda_api_key:
        return []
    r = httpx.get(
        f"{FDC_URL}/foods/search",
        params={
            "api_key": settings.usda_api_key,
            "query": query,
            "pageSize": limit,
            "dataType": "Foundation,SR Legacy,Branded",
        },
        timeout=15,
    )
    r.raise_for_status()
    out = []
    for f in r.json().get("foods", []):
        vals = {"carbs_g": None, "fat_g": None, "protein_g": None, "fiber_g": None, "kcal": None}
        for n in f.get("foodNutrients", []):
            key = _FDC_NUTRIENTS.get(n.get("nutrientId"))
            if key:
                vals[key] = n.get("value")
        out.append(
            {
                "source": "usda",
                "reference_id": str(f.get("fdcId")),
                "name": f.get("description"),
                "brand": f.get("brandOwner"),
                "serving_desc": "100 g",
                "serving_grams": 100.0,
                **vals,
            }
        )
    return out


def openfoodfacts_barcode(barcode: str) -> dict | None:
    r = httpx.get(
        f"{OFF_URL}/product/{barcode}.json",
        params={"fields": "product_name,brands,nutriments,serving_size,serving_quantity"},
        timeout=15,
        headers={"User-Agent": "hypovisor/0.1 (personal T1D tool)"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if data.get("status") != 1:
        return None
    p = data["product"]
    n = p.get("nutriments", {})
    return {
        "source": "openfoodfacts",
        "reference_id": barcode,
        "name": p.get("product_name"),
        "brand": p.get("brands"),
        "serving_desc": "100 g",
        "serving_grams": 100.0,
        "carbs_g": n.get("carbohydrates_100g"),
        "fat_g": n.get("fat_100g"),
        "protein_g": n.get("proteins_100g"),
        "fiber_g": n.get("fiber_100g"),
        "kcal": n.get("energy-kcal_100g"),
        "serving_size": p.get("serving_size"),
    }
