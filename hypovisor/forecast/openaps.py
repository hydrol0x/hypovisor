"""OpenAPS Data Commons loader (benchmark only; the dataset is obtained from Open Humans under its own terms).

Per donor -> 5-minute grid with glucose, bolus, carbs, basal_rate, hour.

Per donor: openaps/<id>/direct-sharing-31/{entries,treatments,profile}*.json.gz (possibly date-split).
Basal = profile schedule (default profile, in the profile's timezone) overridden by 'Temp Basal' treatments
(absolute/rate, or percent of scheduled, for `duration` minutes). Boluses = treatments with insulin
(SMBs included, flagged). Carbs = treatments with carbs.
"""

from __future__ import annotations

import glob
import gzip
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def _load_docs(paths: list[str]) -> list[dict]:
    docs: dict[str, dict] = {}
    for p in sorted(paths):
        try:
            with gzip.open(p, "rt") as fh:
                data = json.load(fh)
        except Exception as e:  # noqa: BLE001
            print("  skip", p, e, file=sys.stderr)
            continue
        if isinstance(data, dict):
            data = data.get("result", [data])
        for d in data:
            if not isinstance(d, dict):
                continue
            key = d.get("_id") or f"{d.get('date')}_{d.get('created_at')}_{d.get('eventType', '')}"
            docs[key] = d
    return list(docs.values())


def _ts(d: dict) -> pd.Timestamp | None:
    if d.get("date") is not None:
        try:
            v = float(d["date"])
            if v > 1e11:  # ms
                v /= 1000
            return pd.Timestamp(v, unit="s", tz="UTC").tz_convert(None)
        except (TypeError, ValueError):
            pass
    for k in ("created_at", "dateString", "timestamp"):
        s = d.get(k)
        if isinstance(s, str) and s:
            try:
                t = pd.Timestamp(s)
                return t.tz_convert(None) if t.tzinfo else t
            except (ValueError, TypeError):
                continue
    return None


def _sched(profile_store: dict, key: str) -> list[tuple[int, float]]:
    out = []
    for e in profile_store.get(key, []) or []:
        try:
            sec = (
                int(float(e.get("timeAsSeconds", 0)))
                if e.get("timeAsSeconds") not in (None, "")
                else int(e["time"][:2]) * 3600 + int(e["time"][3:5]) * 60
            )
            out.append((sec, float(e["value"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out)


def load_donor(donor_dir: Path) -> pd.DataFrame | None:
    entries = _load_docs(glob.glob(str(donor_dir / "entries*.json.gz")))
    treatments = _load_docs(glob.glob(str(donor_dir / "treatments*.json.gz")))
    profiles = _load_docs(glob.glob(str(donor_dir / "profile*.json.gz")))
    sg = [(_ts(e), e.get("sgv")) for e in entries if e.get("type", "sgv") == "sgv" and e.get("sgv")]
    sg = [(t, float(v)) for t, v in sg if t is not None and 20 <= float(v) <= 500]
    if len(sg) < 288 * 14:
        return None
    g = pd.Series({t: v for t, v in sg}).sort_index()
    g = g[~g.index.duplicated(keep="last")]
    g.index = g.index.round("5min")
    g = g[~g.index.duplicated(keep="last")]
    idx = pd.date_range(g.index.min(), g.index.max(), freq="5min")
    df = pd.DataFrame(index=idx)
    df["glucose"] = g.reindex(idx)

    # Profile: pick the most recent profile document's default store
    tzname = "UTC"
    basal_sched: list[tuple[int, float]] = []
    prof_sorted = sorted(
        [p for p in profiles if isinstance(p.get("store"), dict)],
        key=lambda p: p.get("startDate") or p.get("created_at") or "",
    )
    if prof_sorted:
        p = prof_sorted[-1]
        store = p["store"].get(p.get("defaultProfile")) or next(iter(p["store"].values()))
        tzname = store.get("timezone") or p.get("timezone") or "UTC"
        basal_sched = _sched(store, "basal")
    try:
        tz = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local = idx.tz_localize("UTC").tz_convert(tz)
    secs = (local.hour * 3600 + local.minute * 60).to_numpy()
    df["hour"] = (local.hour + local.minute / 60).to_numpy()
    sched_rate = np.zeros(len(idx))
    if basal_sched:
        times = np.array([s for s, _ in basal_sched])
        vals = np.array([v for _, v in basal_sched])
        pos = np.searchsorted(times, secs, side="right") - 1
        sched_rate = vals[np.where(pos < 0, len(vals) - 1, pos)]
    basal = pd.Series(sched_rate, index=idx)

    bolus = pd.Series(0.0, index=idx)
    carbs = pd.Series(0.0, index=idx)
    n_bolus = n_carbs = n_temp = 0
    temp_overrides = []
    for t in treatments:
        ts = _ts(t)
        if ts is None:
            continue
        ts5 = ts.round("5min")
        et = (t.get("eventType") or "").lower()
        ins = t.get("insulin")
        if ins not in (None, "", 0) and ts5 in bolus.index:
            try:
                bolus[ts5] += float(ins)
                n_bolus += 1
            except (TypeError, ValueError):
                pass
        c = t.get("carbs")
        if c not in (None, "", 0) and ts5 in carbs.index:
            try:
                carbs[ts5] += float(c)
                n_carbs += 1
            except (TypeError, ValueError):
                pass
        if "temp basal" in et and t.get("duration") not in (None, "", 0):
            try:
                dur = float(t["duration"])
            except (TypeError, ValueError):
                continue
            rate = t.get("absolute") if t.get("absolute") not in (None, "") else t.get("rate")
            pct = t.get("percent")
            temp_overrides.append((ts5, dur, rate, pct))
            n_temp += 1
    # apply temp basals in time order (later ones cancel earlier)
    for ts5, dur, rate, pct in sorted(temp_overrides, key=lambda x: x[0]):
        end = ts5 + pd.Timedelta(minutes=dur)
        sl = basal.loc[ts5:end]
        if len(sl) == 0:
            continue
        if rate not in (None, ""):
            basal.loc[sl.index] = float(rate)
        elif pct not in (None, ""):
            basal.loc[sl.index] = pd.Series(sched_rate, index=idx).loc[sl.index] * (1 + float(pct) / 100)
    df["basal_rate"] = basal.to_numpy()
    df["bolus"] = bolus.to_numpy()
    df["carbs"] = carbs.to_numpy()
    df["carbs_bwz"] = 0.0
    df.attrs.update(
        dict(tz=tzname, n_bolus=n_bolus, n_carbs=n_carbs, n_temp=n_temp, has_sched=bool(basal_sched))
    )
    return df


def survey(root: Path) -> pd.DataFrame:
    rows = []
    for d in sorted(root.glob("*/direct-sharing-31")):
        did = d.parent.name
        try:
            df = load_donor(d)
        except Exception as e:  # noqa: BLE001
            print("  error", did, e, file=sys.stderr)
            continue
        if df is None:
            rows.append(dict(donor=did, days=0))
            continue
        daily = df["glucose"].notna().groupby(df.index.date).mean()
        good = daily[daily >= 0.8]
        rows.append(
            dict(
                donor=did,
                days=len(daily),
                good_days=len(good),
                cgm_cov=round(float(df["glucose"].notna().mean()), 3),
                n_bolus=df.attrs["n_bolus"],
                n_carbs=df.attrs["n_carbs"],
                n_temp=df.attrs["n_temp"],
                has_sched=df.attrs["has_sched"],
                tz=df.attrs["tz"],
                start=str(df.index.min().date()),
                end=str(df.index.max().date()),
            )
        )
        print(rows[-1], flush=True)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    out = survey(Path(sys.argv[1]))
    out.to_csv(sys.argv[2], index=False)
    print(out.describe())
