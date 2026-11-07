"""OhioT1DM XML -> per-patient 5-minute grid (benchmark loader; the dataset itself is DUA-restricted and not shipped).

Files: <id>-ws-training.xml / <id>-ws-testing.xml. Root <patient id weight insulin_type>, children such as
<glucose_level><event ts="dd-mm-yyyy HH:MM:SS" value="..."/>, <basal><event ts value/>,
<temp_basal><event ts_begin ts_end value/>, <bolus><event ts_begin ts_end type dose bwz_carb_input/>,
<meal><event ts type carbs/>, <finger_stick>, <exercise>, <sleep>, <basis_*> wearables, <hypo_event> ...
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

STEP = pd.Timedelta(minutes=5)


def _ts(s: str) -> datetime:
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(s)


def parse_xml(path: Path) -> dict:
    root = ET.parse(path).getroot()
    out = {"id": root.get("id"), "weight": root.get("weight"), "insulin_type": root.get("insulin_type")}
    glucose = [(_ts(e.get("ts")), float(e.get("value"))) for e in root.findall("glucose_level/event")]
    out["glucose"] = pd.Series(dict(glucose)).sort_index() if glucose else pd.Series(dtype=float)
    out["basal"] = [(_ts(e.get("ts")), float(e.get("value"))) for e in root.findall("basal/event")]
    out["temp_basal"] = [
        (_ts(e.get("ts_begin")), _ts(e.get("ts_end")), float(e.get("value")))
        for e in root.findall("temp_basal/event")
    ]
    out["bolus"] = [
        (
            _ts(e.get("ts_begin")),
            _ts(e.get("ts_end")),
            e.get("type"),
            float(e.get("dose")),
            float(e.get("bwz_carb_input") or 0),
        )
        for e in root.findall("bolus/event")
    ]
    out["meal"] = [
        (_ts(e.get("ts")), e.get("type"), float(e.get("carbs") or 0)) for e in root.findall("meal/event")
    ]
    out["finger_stick"] = [
        (_ts(e.get("ts")), float(e.get("value"))) for e in root.findall("finger_stick/event")
    ]
    out["exercise"] = [
        (_ts(e.get("ts")), float(e.get("intensity") or 0), float(e.get("duration") or 0))
        for e in root.findall("exercise/event")
    ]
    out["hypo_event"] = [_ts(e.get("ts")) for e in root.findall("hypo_event/event")]
    for name in ("basis_heart_rate", "basis_gsr", "basis_skin_temperature", "basis_steps", "acceleration"):
        evs = [
            (_ts(e.get("ts")), float(e.get("value")))
            for e in root.findall(f"{name}/event")
            if e.get("value") not in (None, "")
        ]
        out[name] = pd.Series(dict(evs)).sort_index() if evs else pd.Series(dtype=float)
    return out


def to_grid(p: dict) -> pd.DataFrame:
    """Resample onto a regular 5-minute grid. glucose stays NaN in gaps (no imputation here)."""
    g = p["glucose"]
    if g.empty:
        raise ValueError("no glucose")
    start = g.index.min().floor("5min")
    end = g.index.max().ceil("5min")
    idx = pd.date_range(start, end, freq="5min")
    df = pd.DataFrame(index=idx)
    # Nearest-reading assignment (readings are ~5 min apart but drift by seconds)
    gs = g.copy()
    gs.index = gs.index.round("5min")
    gs = gs[~gs.index.duplicated(keep="last")]
    df["glucose"] = gs.reindex(idx)
    # Basal: step function of scheduled rate, overridden by temp basals
    basal = pd.Series(np.nan, index=idx)
    for ts, rate in sorted(p["basal"]):
        basal.loc[basal.index >= ts] = rate
    basal = basal.ffill().bfill().fillna(0)
    for b, e, rate in p["temp_basal"]:
        basal.loc[(basal.index >= b) & (basal.index < e)] = rate
    df["basal_rate"] = basal
    # Bolus units per step (dual/extended spread over ts_begin..ts_end)
    bolus = pd.Series(0.0, index=idx)
    carbs_bwz = pd.Series(0.0, index=idx)
    for b, e, typ, dose, bwz in p["bolus"]:
        b5 = pd.Timestamp(b).round("5min")
        if typ and "square" in typ or (e and e > b and (e - b).total_seconds() > 600):
            n = max(1, int((pd.Timestamp(e).round("5min") - b5) / STEP))
            for k in range(n):
                t = b5 + k * STEP
                if t in bolus.index:
                    bolus.loc[t] += dose / n
        elif b5 in bolus.index:
            bolus.loc[b5] += dose
        if bwz and b5 in carbs_bwz.index:
            carbs_bwz.loc[b5] += bwz
    df["bolus"] = bolus
    df["carbs_bwz"] = carbs_bwz
    carbs = pd.Series(0.0, index=idx)
    for ts, _typ, c in p["meal"]:
        t = pd.Timestamp(ts).round("5min")
        if t in carbs.index:
            carbs.loc[t] += c
    df["carbs"] = carbs
    for name in ("basis_heart_rate", "basis_gsr", "basis_skin_temperature", "basis_steps"):
        s = p.get(name)
        if s is not None and not s.empty:
            s = s.copy()
            s.index = s.index.round("5min")
            s = s[~s.index.duplicated(keep="last")]
            df[name] = s.reindex(idx)
    df["hour"] = df.index.hour + df.index.minute / 60
    return df


def load_patient(root: Path, pid: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    files = list(root.rglob(f"{pid}-ws-training.xml")), list(root.rglob(f"{pid}-ws-testing.xml"))
    if not files[0] or not files[1]:
        raise FileNotFoundError(pid)
    return to_grid(parse_xml(files[0][0])), to_grid(parse_xml(files[1][0]))


def find_patients(root: Path) -> list[str]:
    ids = sorted(
        {
            re.match(r"(\d+)-ws-(training|testing)\.xml", p.name).group(1)
            for p in root.rglob("*-ws-*.xml")
            if re.match(r"(\d+)-ws-", p.name)
        }
    )
    return ids


if __name__ == "__main__":
    root = Path(sys.argv[1])
    for pid in find_patients(root):
        tr, te = load_patient(root, pid)
        print(
            pid,
            "train",
            len(tr),
            f"{tr.glucose.notna().mean():.2%} glucose",
            "boluses",
            int((tr.bolus > 0).sum()),
            "meals",
            int((tr.carbs > 0).sum()),
            "| test",
            len(te),
            f"{te.glucose.notna().mean():.2%}",
        )
