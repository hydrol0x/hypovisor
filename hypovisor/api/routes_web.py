"""Server-rendered pages. Data comes from the JSON API via fetch() in the page scripts."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))
router = APIRouter(include_in_schema=False)


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update({"request": request, "settings": settings, "page": name.split(".")[0]})
    return TEMPLATES.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return _render(request, "dashboard.html")


@router.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, day: str | None = None):
    return _render(request, "timeline.html", day=day or "")


@router.get("/meals", response_class=HTMLResponse)
def meals(request: Request):
    return _render(request, "meals.html")


@router.get("/meals/new", response_class=HTMLResponse)
def meal_new(request: Request):
    return _render(request, "meal_new.html")


@router.get("/meals/{meal_id}", response_class=HTMLResponse)
def meal_detail(request: Request, meal_id: int):
    return _render(request, "meal_detail.html", meal_id=meal_id)


@router.get("/foods", response_class=HTMLResponse)
def foods(request: Request):
    return _render(request, "foods.html")


@router.get("/foods/{food_id}", response_class=HTMLResponse)
def food_detail(request: Request, food_id: int):
    return _render(request, "food_detail.html", food_id=food_id)


@router.get("/insights", response_class=HTMLResponse)
def insights(request: Request):
    return _render(request, "insights.html")


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return _render(request, "import.html")


@router.get("/manifest.json")
def manifest():
    return {
        "name": "Hypovisor",
        "short_name": "Hypovisor",
        "start_url": "/meals/new",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }
