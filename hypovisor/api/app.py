"""FastAPI application factory. Serves the JSON API, the web UI, and uploaded photos."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..db import init_db
from . import routes_data, routes_foods, routes_import, routes_insights, routes_meals, routes_sync, routes_web

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    settings.photo_dir.mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hypovisor", version="0.1.0", description="Personal T1D data platform", lifespan=_lifespan
    )
    settings.photo_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.mount("/photos", StaticFiles(directory=settings.photo_dir), name="photos")
    for r in (
        routes_import,
        routes_data,
        routes_meals,
        routes_foods,
        routes_insights,
        routes_sync,
        routes_web,
    ):
        app.include_router(r.router)
    return app


app = create_app()
