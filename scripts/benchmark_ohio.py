"""Reproduce the forecasting benchmark for Hypovisor's forecaster on OhioT1DM or the OpenAPS Data Commons.

    python scripts/benchmark_ohio.py /path/to/OhioT1DM [--history 24] [--mode population|personal] [--pid]
    python scripts/benchmark_ohio.py /path/to/openaps --dataset openaps --max-donors 40 --days 90

Standard protocol: official per-patient training/testing XML files, last 20% of training as validation for
early stopping, RMSE/MAE at 30 and 60 minutes over real (non-imputed) test readings. OhioT1DM is distributed
under a data use agreement and is not included in this repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypovisor.forecast.features import HORIZONS, feature_matrix, make_samples  # noqa: E402
from hypovisor.forecast.model import GlucoseForecaster  # noqa: E402
from hypovisor.forecast.ohio import find_patients, load_patient  # noqa: E402
from hypovisor.forecast.openaps import load_donor  # noqa: E402
from hypovisor.forecast.physio import add_physio_features  # noqa: E402


def prep(df):
    df = df.copy()
    df["carbs"] = df["carbs"] + np.where(df["carbs"] > 0, 0, df["carbs_bwz"])
    df = add_physio_features(df)
    df["glucose_i"] = df["glucose"].interpolate(limit=3, limit_area="inside")
    return df


def openaps_patients(root: Path, max_donors: int, days: int, min_good_days: int = 60):
    """Donors with enough well-covered days; last `days` days each; chronological 80/20 train/test split."""
    import pandas as pd

    out = {}
    for d in sorted(root.glob("*/direct-sharing-31")):
        df = load_donor(d)
        if df is None:
            continue
        daily = df["glucose"].notna().groupby(df.index.date).mean()
        good = daily[daily >= 0.8].index
        if len(good) < min_good_days:
            continue
        end = pd.Timestamp(good[-1]) + pd.Timedelta(days=1)
        w = df.loc[end - pd.Timedelta(days=days) : end]
        cut = int(len(w) * 0.8)
        out[d.parent.name] = (w.iloc[:cut], w.iloc[cut:])
        print("loaded", d.parent.name, f"{len(w)} steps", flush=True)
        if len(out) >= max_donors:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--dataset", default="ohio", choices=["ohio", "openaps"])
    ap.add_argument("--max-donors", dest="max_donors", type=int, default=40)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--history", type=int, default=24)
    ap.add_argument("--mode", default="population", choices=["population", "personal"])
    ap.add_argument(
        "--pid", action="store_true", help="add patient id as a categorical feature (population mode)"
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = Path(args.root)
    P = {}
    if args.dataset == "ohio":
        patients = {pid: load_patient(root, pid) for pid in find_patients(root)}
    else:
        patients = openaps_patients(root, args.max_donors, args.days)
    for j, (pid, (tr_raw, te_raw)) in enumerate(patients.items()):
        tr, te = prep(tr_raw), prep(te_raw)
        Xtr, ytr, itr = make_samples(tr, args.history, HORIZONS)
        Xte, yte, ite = make_samples(te, args.history, HORIZONS)
        nval = int(len(Xtr) * 0.2)
        Ftr, Fval, Fte = (
            feature_matrix(Xtr[:-nval], tr, itr[:-nval]),
            feature_matrix(Xtr[-nval:], tr, itr[-nval:]),
            feature_matrix(Xte, te, ite),
        )
        if args.pid:
            Ftr, Fval, Fte = (np.column_stack([F, np.full(len(F), j)]) for F in (Ftr, Fval, Fte))
        P[pid] = dict(
            Ftr=Ftr,
            Fval=Fval,
            Fte=Fte,
            dtr=ytr[:-nval] - Xtr[:-nval, -1, 0:1],
            dval=ytr[-nval:] - Xtr[-nval:, -1, 0:1],
            yte=yte,
            last=Xte[:, -1, 0],
        )
    per = {}
    if args.mode == "population":
        pool = {k: np.concatenate([p[k] for p in P.values()]) for k in ("Ftr", "Fval", "dtr", "dval")}
        model = GlucoseForecaster(args.history).fit_arrays(
            pool["Ftr"], pool["dtr"], pool["Fval"], pool["dval"]
        )
    for pid, p in P.items():
        if args.mode == "personal":
            model = GlucoseForecaster(args.history).fit_arrays(p["Ftr"], p["dtr"], p["Fval"], p["dval"])
        pred = model.predict_features(p["Fte"], p["last"])
        persist = np.repeat(p["last"][:, None], len(HORIZONS), axis=1)
        per[pid] = {}
        for k, h in enumerate(HORIZONS):
            per[pid][f"rmse{h * 5}"] = float(np.sqrt(np.mean((pred[:, k] - p["yte"][:, k]) ** 2)))
            per[pid][f"mae{h * 5}"] = float(np.mean(np.abs(pred[:, k] - p["yte"][:, k])))
            per[pid][f"persistence_rmse{h * 5}"] = float(
                np.sqrt(np.mean((persist[:, k] - p["yte"][:, k]) ** 2))
            )
        print(pid, {k: round(v, 2) for k, v in per[pid].items()})
    mean = {k: float(np.mean([v[k] for v in per.values()])) for k in next(iter(per.values()))}
    print("MEAN", {k: round(v, 2) for k, v in mean.items()})
    if args.out:
        Path(args.out).write_text(
            json.dumps({"args": vars(args), "per_patient": per, "mean": mean}, indent=1)
        )


if __name__ == "__main__":
    main()
