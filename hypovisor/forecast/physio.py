"""Physiology-informed features on a 5-minute grid: insulin on board and activity, carbs on board and
absorption, basal deviation. The insulin curve is the same exponential model as hypovisor.analytics.insulin."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import settings


def _curve(dia_min: float, peak_min: float):
    td, tp = float(dia_min), float(peak_min)
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * np.exp(-td / tau))
    return tau, a, s


def iob_kernel(dia_min: float | None = None, peak_min: float | None = None, step: int = 5):
    dia_min = dia_min or settings.dia_minutes
    peak_min = peak_min or settings.insulin_peak_minutes
    tau, a, s = _curve(dia_min, peak_min)
    t = np.arange(0, dia_min, step, dtype=float)
    iob = 1 - s * (1 - a) * ((t**2 / (tau * dia_min * (1 - a)) - t / tau - 1) * np.exp(-t / tau) + 1)
    act = s / tau**2 * t * (1 - t / dia_min) * np.exp(-t / tau) * step
    return np.clip(iob, 0, 1), act


def cob_kernel(absorb_min: int = 180, step: int = 5):
    n = absorb_min // step
    t = np.arange(n, dtype=float)
    peak = n / 3
    rate = np.where(t < peak, t / peak, (n - t) / (n - peak))
    rate = rate / rate.sum()
    return np.clip(1 - np.cumsum(rate), 0, 1), rate


def causal_conv(x: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    return np.convolve(np.nan_to_num(x, nan=0.0), kernel)[: len(x)]


def add_physio_features(df: pd.DataFrame) -> pd.DataFrame:
    """Expects columns bolus (U per step), basal_rate (U/h), carbs (g per step), hour (local, fractional)."""
    iob_k, act_k = iob_kernel()
    cob_k, abs_k = cob_kernel()
    bolus = df["bolus"].to_numpy(dtype=float)
    basal_units = df["basal_rate"].to_numpy(dtype=float) / 12.0
    basal_dev = basal_units - np.nanmedian(basal_units)
    carbs = df["carbs"].to_numpy(dtype=float)
    df["iob"] = causal_conv(bolus, iob_k)
    df["insulin_activity"] = causal_conv(bolus, act_k)
    df["basal_iob_dev"] = causal_conv(basal_dev, iob_k)
    df["cob"] = causal_conv(carbs, cob_k)
    df["carb_absorption"] = causal_conv(carbs, abs_k)
    df["bolus_recent"] = causal_conv(bolus, np.ones(6))
    df["carbs_recent"] = causal_conv(carbs, np.ones(6))
    df["sin_hour"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["cos_hour"] = np.cos(2 * np.pi * df["hour"] / 24)
    return df
