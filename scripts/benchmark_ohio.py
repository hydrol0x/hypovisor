"""Reproduce the OhioT1DM forecasting benchmark for Hypovisor's forecaster.

    python scripts/benchmark_ohio.py /path/to/OhioT1DM [--history 24] [--mode population|personal] [--pid]

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
from hypovisor.forecast.physio import add_physio_features  # noqa: E402


def prep(df):
    df = df.copy()
    df["carbs"] = df["carbs"] + np.where(df["carbs"] > 0, 0, df["carbs_bwz"])
    df = add_physio_features(df)
    df["glucose_i"] = df["glucose"].interpolate(limit=3, limit_area="inside")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--history", type=int, default=24)
    ap.add_argument("--mode", default="population", choices=["population", "personal"])
    ap.add_argument(
        "--pid", action="store_true", help="add patient id as a categorical feature (population mode)"
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = Path(args.root)
    P = {}
    for j, pid in enumerate(find_patients(root)):
        tr, te = (prep(d) for d in load_patient(root, pid))
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
