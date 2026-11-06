"""Synthetic data generator: writes a realistic Dexcom Clarity CSV and a Tandem Source export folder,
plus a list of meals, so the whole pipeline can be exercised without real exports or an API key.

The glucose model is deliberately simple (carb absorption minus insulin action plus noise), but it has
per-food behaviour (pizza rises late, oatmeal spikes early) so pattern views show something sensible.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .analytics.insulin import activity_fraction

FOODS = [
    # name, carbs, fat, protein, absorption peak minutes, late fraction (0-1 of carbs arriving 2-5h), slot
    ("oatmeal with banana", 55, 6, 10, 45, 0.0, "breakfast"),
    ("eggs and toast", 25, 18, 20, 50, 0.05, "breakfast"),
    ("greek yogurt with granola", 35, 8, 18, 40, 0.0, "breakfast"),
    ("chicken burrito bowl", 65, 20, 40, 70, 0.15, "lunch"),
    ("turkey sandwich", 45, 12, 28, 55, 0.05, "lunch"),
    ("sushi (8 pieces)", 70, 6, 24, 50, 0.05, "lunch"),
    ("salad with chicken", 15, 22, 35, 60, 0.05, "lunch"),
    ("pizza (2 slices)", 60, 24, 26, 90, 0.45, "dinner"),
    ("pasta with meat sauce", 80, 18, 30, 75, 0.25, "dinner"),
    ("salmon, rice and broccoli", 50, 16, 38, 60, 0.1, "dinner"),
    ("cheeseburger and fries", 75, 38, 32, 80, 0.4, "dinner"),
    ("ice cream", 30, 14, 5, 35, 0.1, "snack"),
    ("apple", 25, 0, 0, 30, 0.0, "snack"),
]
SLOT_HOURS = {"breakfast": (7, 9), "lunch": (12, 13.5), "dinner": (18, 20), "snack": (15, 22)}


@dataclass
class SynthMeal:
    local_ts: datetime
    name: str
    carbs: float
    fat: float
    protein: float
    bolus: float
    prebolus_min: int


def generate(
    out_dir: Path,
    days: int = 21,
    start: date | None = None,
    seed: int = 7,
    icr: float = 10.0,
    isf: float = 45.0,
    basal_uph: float = 0.85,
) -> dict:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = start or (date.today() - timedelta(days=days))
    t0 = datetime(start.year, start.month, start.day)
    n_steps = days * 288
    times = [t0 + timedelta(minutes=5 * i) for i in range(n_steps)]

    # Plan meals and boluses
    meals: list[SynthMeal] = []
    for d in range(days):
        day = t0 + timedelta(days=d)
        for slot in ("breakfast", "lunch", "dinner"):
            if rng.random() < 0.08:
                continue
            choices = [f for f in FOODS if f[6] == slot]
            f = rng.choice(choices)
            h0, h1 = SLOT_HOURS[slot]
            hour = rng.uniform(h0, h1)
            ts = day + timedelta(minutes=round(hour * 60 / 5) * 5)
            scale = rng.uniform(0.8, 1.25)
            carbs = round(f[1] * scale)
            user_est_err = rng.uniform(0.8, 1.15)  # the person's own carb count error
            bolus = round(carbs * user_est_err / icr * 2) / 2
            pre = rng.choice([0, 0, 5, 10, 15, 15, 20])
            meals.append(SynthMeal(ts, f[0], carbs, round(f[2] * scale), round(f[3] * scale), bolus, pre))
        if rng.random() < 0.35:
            f = rng.choice([x for x in FOODS if x[6] == "snack"])
            ts = day + timedelta(minutes=round(rng.uniform(15, 21.5) * 60 / 5) * 5)
            carbs = round(f[1] * rng.uniform(0.8, 1.2))
            meals.append(
                SynthMeal(
                    ts, f[0], carbs, f[2], f[3], round(carbs / icr * 2) / 2 if rng.random() < 0.6 else 0.0, 0
                )
            )
    meals.sort(key=lambda m: m.local_ts)
    food_by_name = {f[0]: f for f in FOODS}

    # Simulate glucose
    g = np.zeros(n_steps)
    g[0] = 120
    boluses: list[tuple[datetime, float, float, float]] = []  # ts, units, carbs, bg
    for m in meals:
        if m.bolus > 0:
            boluses.append((m.local_ts - timedelta(minutes=m.prebolus_min), m.bolus, m.carbs, 0.0))
    corrections: list[datetime] = []
    rescues: list[SynthMeal] = []
    carb_rate = np.zeros(n_steps)  # grams absorbed per 5-min step

    def add_carbs(local_ts: datetime, grams: float, peak_min: float, late_frac: float) -> None:
        i0 = int((local_ts - t0).total_seconds() // 300)
        early_g, late_g = grams * (1 - late_frac), grams * late_frac
        w = np.array([(k * 5 / peak_min) * math.exp(1 - k * 5 / peak_min) for k in range(48)])
        w = w / w.sum()
        for k in range(48):
            if i0 + k < n_steps:
                carb_rate[i0 + k] += early_g * w[k]
        for k in range(24, 72):
            if i0 + k < n_steps:
                carb_rate[i0 + k] += late_g / 48

    for m in meals:
        f = food_by_name[m.name]
        add_carbs(m.local_ts, m.carbs, f[4], f[5])

    mgdl_per_gram = isf / icr * 0.85  # 1 g carb ~ ISF/ICR mg/dL, a bit less for fiber/hepatic uptake
    basal_rates = np.zeros(n_steps)  # U/h actually delivered (Control-IQ-like modulation)
    egp_base = (
        basal_uph / 12 * isf
    )  # endogenous glucose production per step that the basal is meant to cancel
    i = 1
    basal_rates[0] = basal_uph
    while i < n_steps:
        t = times[i]
        prev = g[i - 1]
        dawn = 1.3 if 3 <= t.hour < 7 else 1.0
        # pump modulates basal like a simple hybrid closed loop
        rate = basal_uph * (1.35 if prev > 180 else 0.0 if prev < 75 else 0.5 if prev < 95 else 1.0)
        basal_rates[i] = round(rate, 3)
        # automated-style correction when high and no recent bolus/correction (at most every 3 h)
        if (
            prev > 230
            and not any(abs((t - b[0]).total_seconds()) < 3 * 3600 for b in boluses)
            and not any(abs((t - c).total_seconds()) < 3 * 3600 for c in corrections)
        ):
            units = round((prev - 120) / isf * 0.6 * 2) / 2
            if units >= 0.5:
                corrections.append(t)
                boluses.append((t, units, 0.0, prev))
        # rescue carbs on lows
        if prev < 65 and not any(abs((t - r.local_ts).total_seconds()) < 2 * 3600 for r in rescues):
            r = SynthMeal(t, "glucose tabs", 15, 0, 0, 0.0, 0)
            rescues.append(r)
            add_carbs(t, 15, 20, 0.0)
        act = 0.0
        for ts_b, units, _, _ in boluses:
            d = (t - ts_b).total_seconds() / 60
            if 0 <= d < 300:
                act += units * float(activity_fraction(d)[0]) * 5
        basal_act = 0.0
        for k in range(1, 60):  # basal doses over the last 5 h acting through the same curve
            if i - k >= 0:
                basal_act += basal_rates[i - k] / 12 * float(activity_fraction(k * 5)[0]) * 5
        dg = (
            carb_rate[i] * mgdl_per_gram - act * isf - basal_act * isf + egp_base * dawn + nrng.normal(0, 1.0)
        )
        g[i] = min(400, max(40, prev + dg))
        i += 1
    g = g + nrng.normal(0, 2.0, n_steps)
    boluses.sort(key=lambda b: b[0])
    meals = sorted(meals + rescues, key=lambda m: m.local_ts)

    # Write Clarity CSV (English headers, comma)
    clarity = out_dir / "clarity_export.csv"
    with clarity.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "Index",
                "Timestamp (YYYY-MM-DDThh:mm:ss)",
                "Event Type",
                "Event Subtype",
                "Patient Info",
                "Device Info",
                "Source Device ID",
                "Glucose Value (mg/dL)",
                "Insulin Value (u)",
                "Carb Value (grams)",
                "Duration (hh:mm:ss)",
                "Glucose Rate of Change (mg/dL/min)",
                "Transmitter Time (Long Integer)",
                "Transmitter ID",
            ]
        )
        w.writerow([1, "", "FirstName", "", "Synthetic", "", "", "", "", "", "", "", "", ""])
        w.writerow([2, "", "LastName", "", "Person", "", "", "", "", "", "", "", "", ""])
        w.writerow([3, "", "Device", "", "", "Dexcom G7", "iOS G7", "", "", "", "", "", "", ""])
        w.writerow([4, "", "Alert", "High", "", "", "iOS G7", 250, "", "", "", "", "", ""])
        idx = 5
        for t, v in zip(times, g, strict=True):
            if random.Random(hash(t)).random() < 0.015:
                continue  # gaps
            val = "Low" if v < 40 else "High" if v > 400 else int(round(v))
            w.writerow(
                [
                    idx,
                    t.strftime("%Y-%m-%dT%H:%M:%S"),
                    "EGV",
                    "",
                    "",
                    "",
                    "iOS G7",
                    val,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "SYNTH1",
                ]
            )
            idx += 1
    # Write Tandem Source folder
    tdir = out_dir / "tandem_source_export"
    tdir.mkdir(exist_ok=True)
    prefix = "Synthetic_0000001_" + date.today().strftime("%d%b%Y") + "_0000"
    with (tdir / f"{prefix}-Bolus.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "Type",
                "Bolus Type",
                "Bolus Delivery Method",
                "BG (mg/dL)",
                "Serial Number",
                "Completion Date Time",
                "Insulin Delivered",
                "Food Delivered",
                "Correction Delivered",
                "Completion Status Desc",
                "Bolex Start Date Time",
                "Bolex Completion Date Time",
                "Bolex Insulin Delivered",
                "Bolex Completion Status Desc",
                "Standard Percent",
                "Duration (mins)",
                "Carb Size",
                "Target BG (mg/dL)",
                "Correction Factor",
                "Carb Ratio",
            ]
        )
        for ts, units, carbs, bg in boluses:
            btype = "Food / Correction" if carbs and bg else "Food" if carbs else "Correction"
            food_u = round(carbs / icr, 2) if carbs else 0
            w.writerow(
                [
                    "Bolus",
                    btype,
                    "Standard",
                    int(bg) if bg else "",
                    "0000001",
                    (ts + timedelta(seconds=40)).strftime("%Y-%m-%dT%H:%M:%S"),
                    units,
                    food_u,
                    round(units - food_u, 2),
                    "Completed",
                    "",
                    "",
                    "",
                    "",
                    100,
                    0,
                    int(carbs) if carbs else 0,
                    110,
                    isf,
                    icr,
                ]
            )
    with (tdir / f"{prefix}-Basal-doses.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["Device Type", "Serial Number", "Event Date Time", "Commanded Basal Dose (units of insulin)"]
        )
        for t, rate in zip(times, basal_rates, strict=True):
            w.writerow(
                ["t:slim X2 Insulin Pump", "0000001", t.strftime("%Y-%m-%dT%H:%M:%S"), round(rate / 12, 3)]
            )
    with (tdir / f"{prefix}-CGM.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Device Type", "Serial Number", "Description", "Event Date Time", "Readings (mg/dL)"])
        for t, v in zip(times, g, strict=True):
            w.writerow(
                [
                    "t:slim X2 Insulin Pump",
                    "0000001",
                    "EGV",
                    (t + timedelta(seconds=11)).strftime("%Y-%m-%dT%H:%M:%S"),
                    int(round(min(400, max(40, v)))),
                ]
            )
    with (tdir / f"{prefix}-BG.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Device Type", "Serial Number", "Description", "Event Date Time", "BG (mg/dL)", "Note"])
        for ts, _units, _carbs, bg in boluses:
            if bg:
                w.writerow(
                    ["t:slim X2 Insulin Pump", "0000001", "BG", ts.strftime("%Y-%m-%dT%H:%M:%S"), int(bg), ""]
                )
    meals_csv = out_dir / "meals.csv"
    with meals_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["local_ts", "description", "carbs_g", "fat_g", "protein_g"])
        for m in meals:
            w.writerow([m.local_ts.strftime("%Y-%m-%dT%H:%M:%S"), m.name, m.carbs, m.fat, m.protein])
    return {
        "clarity": str(clarity),
        "tandem": str(tdir),
        "meals": str(meals_csv),
        "n_meals": len(meals),
        "n_boluses": len(boluses),
        "days": days,
    }
