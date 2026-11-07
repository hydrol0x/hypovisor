"""Build the 5-minute grid from the event store and turn it into windowed training samples."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.glucose import load_series
from ..models import BasalDose, Bolus, Meal
from ..timeutil import utc_to_local
from .physio import add_physio_features

HORIZONS = (6, 12)  # steps of 5 min: 30 and 60 minutes
EXOG = [
    "bolus",
    "carbs",
    "basal_rate",
    "iob",
    "insulin_activity",
    "basal_iob_dev",
    "cob",
    "carb_absorption",
    "sin_hour",
    "cos_hour",
]
STATIC = [
    "iob",
    "cob",
    "insulin_activity",
    "carb_absorption",
    "basal_iob_dev",
    "sin_hour",
    "cos_hour",
    "bolus_recent",
    "carbs_recent",
]


def grid_from_db(session: Session, start: datetime, end: datetime) -> pd.DataFrame:
    """UTC 5-minute grid with glucose (NaN in gaps), bolus units per step, basal rate, carbs per step, local hour."""
    series = load_series(session, start, end)
    idx = pd.date_range(pd.Timestamp(start).floor("5min"), pd.Timestamp(end).ceil("5min"), freq="5min")
    df = pd.DataFrame(index=idx)
    if len(series):
        ts = pd.DatetimeIndex(series.ts).round("5min")
        g = pd.Series(series.values, index=ts)
        g = g[~g.index.duplicated(keep="last")]
        df["glucose"] = g.reindex(idx)
        off = pd.Series(series.offsets, index=ts)
        off = off[~off.index.duplicated(keep="last")].reindex(idx).ffill().bfill()
    else:
        df["glucose"] = np.nan
        off = pd.Series(0, index=idx)
    bolus = pd.Series(0.0, index=idx)
    carbs = pd.Series(0.0, index=idx)
    for b in session.scalars(select(Bolus).where(Bolus.ts >= start - timedelta(hours=8), Bolus.ts <= end)):
        t = pd.Timestamp(b.ts).round("5min")
        if b.units_normal and t in bolus.index:
            bolus[t] += b.units_normal
        if b.units_extended and b.extended_minutes:
            n = max(1, b.extended_minutes // 5)
            for k in range(n):
                tk = t + pd.Timedelta(minutes=5 * k)
                if tk in bolus.index:
                    bolus[tk] += b.units_extended / n
        if b.carbs_entered and t in carbs.index:
            carbs[t] += b.carbs_entered
    # Confirmed meals override pump carb entries within 15 minutes
    for m in session.scalars(
        select(Meal).where(Meal.ts >= start - timedelta(hours=8), Meal.ts <= end, Meal.carbs_g.is_not(None))
    ):
        t = pd.Timestamp(m.ts).round("5min")
        if t not in carbs.index:
            continue
        window = carbs.loc[t - pd.Timedelta(minutes=15) : t + pd.Timedelta(minutes=15)]
        if window.sum() > 0:
            carbs.loc[window.index] = 0.0
        carbs[t] += float(m.carbs_g)
    df["bolus"], df["carbs"] = bolus, carbs
    basal = pd.Series(np.nan, index=idx)
    for d in session.scalars(
        select(BasalDose)
        .where(BasalDose.ts >= start - timedelta(hours=1), BasalDose.ts <= end)
        .order_by(BasalDose.ts)
    ):
        t = pd.Timestamp(d.ts).round("5min")
        if t in basal.index and d.rate_uph is not None:
            basal[t] = d.rate_uph
    df["basal_rate"] = basal.ffill(limit=12).fillna(0.0)
    local = [utc_to_local(t.to_pydatetime(), int(o)) for t, o in zip(idx, off, strict=True)]
    df["hour"] = [t.hour + t.minute / 60 for t in local]
    df = add_physio_features(df)
    df["glucose_i"] = df["glucose"].interpolate(limit=3, limit_area="inside")
    return df


def make_samples(
    df: pd.DataFrame,
    history: int,
    horizons=HORIZONS,
    exog: list[str] | None = None,
    require_target: bool = True,
):
    g = df["glucose_i"].to_numpy(dtype=float)
    raw = df["glucose"].to_numpy(dtype=float)
    exog = EXOG if exog is None else exog
    ex = df[exog].to_numpy(dtype=float)
    n, hmax = len(df), (max(horizons) if require_target else 0)
    Xs, ys, idx = [], [], []
    for t in range(history - 1, n - hmax):
        hist = g[t - history + 1 : t + 1]
        if np.isnan(hist).any():
            continue
        e = ex[t - history + 1 : t + 1]
        if np.isnan(e).any():
            continue
        if require_target:
            tgt = np.array([raw[t + h] for h in horizons])
            if np.isnan(tgt).any():
                continue
            ys.append(tgt)
        Xs.append(np.column_stack([hist, e]))
        idx.append(t)
    X = np.array(Xs) if Xs else np.zeros((0, history, 1 + len(exog)))
    return X, (np.array(ys) if ys else np.zeros((0, len(horizons)))), np.array(idx, dtype=int)


def feature_matrix(X: np.ndarray, df: pd.DataFrame, idx: np.ndarray) -> np.ndarray:
    """Flat features: full window of every channel, recent deltas, window stats, trend/shape features, static physiology."""
    n, h, c = X.shape
    g = X[:, :, 0]
    d1 = np.diff(g, axis=1)
    d2 = np.diff(d1, axis=1)
    parts = [
        X.reshape(n, h * c),
        d1[:, -6:],
        g[:, -1:] - g[:, -7:-6],
        g.mean(axis=1, keepdims=True),
        g.std(axis=1, keepdims=True),
        df[STATIC].to_numpy(dtype=float)[idx],
        g[:, -1:] - g[:, -3:-2],
        g[:, -1:] - g[:, -13:-12],
        g[:, -1:] - g[:, :1],
        d1[:, -3:].mean(axis=1, keepdims=True),
        d1[:, -6:].mean(axis=1, keepdims=True),
        d2[:, -3:].mean(axis=1, keepdims=True),
        g[:, -6:].min(axis=1, keepdims=True),
        g[:, -6:].max(axis=1, keepdims=True),
        g[:, -12:].mean(axis=1, keepdims=True),
        np.abs(d1).mean(axis=1, keepdims=True),
    ]
    return np.concatenate(parts, axis=1)
