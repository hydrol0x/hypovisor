from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..analytics.glucose import daily_metrics, hourly_profile, load_series, summary_metrics
from ..analytics.meal_response import recompute_all
from ..analytics.patterns import (
    day_of_week,
    food_leaderboard,
    lifecycle,
    meal_scores_by_hour,
    overnight_and_dawn,
    sensitivity_by_time_of_day,
)
from ..analytics.settings_estimate import basal_deviation_by_hour, observed_carb_ratio
from ..db import get_session
from ..timeutil import now_utc

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("/overview")
def overview(days: int = Query(30, ge=1, le=365), session: Session = Depends(get_session)):
    end = now_utc()
    start = end - timedelta(days=days)
    series = load_series(session, start, end)
    return {
        "days": days,
        "overall": summary_metrics(series.values),
        "daily": daily_metrics(series),
        "hourly": hourly_profile(series),
        "day_of_week": day_of_week(series),
        "overnight": overnight_and_dawn(series),
        "meal_scores_by_slot": meal_scores_by_hour(session),
        "food_leaderboard": food_leaderboard(session)[:20],
        "sensitivity": sensitivity_by_time_of_day(session, start, end),
        "carb_ratio": observed_carb_ratio(session, start, end),
        "basal_drift": basal_deviation_by_hour(session, start, end),
        "site_lifecycle": lifecycle(session, start, end, "site_change"),
        "sensor_lifecycle": lifecycle(session, start, end, "sensor_start"),
    }


@router.post("/recompute")
def recompute(session: Session = Depends(get_session)):
    n = recompute_all(session)
    session.commit()
    return {"recomputed": n}
