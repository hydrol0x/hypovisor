"""LightGBM glucose forecaster: one booster per horizon predicting the change from the last reading."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from ..config import settings
from ..timeutil import now_utc
from .features import HORIZONS, feature_matrix, grid_from_db, make_samples

PARAMS = dict(
    n_estimators=3000,
    learning_rate=0.01,
    num_leaves=63,
    subsample=0.7,
    subsample_freq=1,
    colsample_bytree=0.6,
    min_child_samples=100,
    reg_lambda=5.0,
    verbose=-1,
    n_jobs=4,
)
DEFAULT_HISTORY = 24  # 2 h; 6 h was 0.1 mg/dL better on OhioT1DM but needs long gap-free windows


def _lgb():
    try:
        import lightgbm as lgb
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Forecasting needs the 'forecast' extra: pip install 'hypovisor[forecast]'") from e
    return lgb


class GlucoseForecaster:
    def __init__(self, history: int = DEFAULT_HISTORY, horizons=HORIZONS):
        self.history, self.horizons = history, tuple(horizons)
        self.boosters = []
        self.meta: dict = {}

    # ------------------------------------------------------------ training
    def fit_arrays(
        self, Ftr, ytr_delta, Fval=None, yval_delta=None, params: dict | None = None
    ) -> GlucoseForecaster:
        lgb = _lgb()
        params = {**PARAMS, **(params or {})}
        self.boosters = []
        for k in range(ytr_delta.shape[1]):
            m = lgb.LGBMRegressor(**params)
            if Fval is not None and len(Fval):
                m.fit(
                    Ftr,
                    ytr_delta[:, k],
                    eval_X=Fval,
                    eval_y=yval_delta[:, k],
                    callbacks=[lgb.early_stopping(100, verbose=False)],
                )
            else:
                m.fit(Ftr, ytr_delta[:, k])
            self.boosters.append(m.booster_)
        return self

    def fit_from_db(self, session: Session, days: int = 120) -> dict:
        """Train on the user's own history; last 20% (chronological) is the validation/early-stopping split."""
        end = now_utc()
        df = grid_from_db(session, end - timedelta(days=days), end)
        X, y, idx = make_samples(df, self.history, self.horizons)
        if len(X) < 500:
            raise ValueError(
                f"Not enough gap-free history to train ({len(X)} samples; need at least 500, about 2 days of CGM)"
            )
        F = feature_matrix(X, df, idx)
        delta = y - X[:, -1, 0:1]
        nval = int(len(X) * 0.2)
        self.fit_arrays(F[:-nval], delta[:-nval], F[-nval:], delta[-nval:])
        pred = self.predict_features(F[-nval:], X[-nval:, -1, 0])
        persist = np.repeat(X[-nval:, -1, 0:1], len(self.horizons), axis=1)
        self.meta = {
            "trained_at": now_utc().isoformat(),
            "days": days,
            "history": self.history,
            "n_train": int(len(X) - nval),
            "n_val": int(nval),
            "val_rmse": {
                f"{h * 5}min": float(np.sqrt(np.mean((pred[:, k] - y[-nval:, k]) ** 2)))
                for k, h in enumerate(self.horizons)
            },
            "val_rmse_persistence": {
                f"{h * 5}min": float(np.sqrt(np.mean((persist[:, k] - y[-nval:, k]) ** 2)))
                for k, h in enumerate(self.horizons)
            },
            "best_iterations": [int(b.best_iteration or b.num_trees()) for b in self.boosters],
        }
        return self.meta

    # ------------------------------------------------------------ prediction
    def predict_features(self, F: np.ndarray, last: np.ndarray) -> np.ndarray:
        return last[:, None] + np.column_stack([b.predict(F) for b in self.boosters])

    def predict_latest(self, session: Session, max_age_minutes: float = 20.0) -> dict:
        """Forecast from the most recent gap-free window. `available` is False when the latest reading is
        older than `max_age_minutes` or the window has gaps."""
        from sqlalchemy import func, select

        from ..models import GlucoseReading

        end = now_utc()
        last = session.scalar(select(func.max(GlucoseReading.ts)))
        if last is None:
            return {"available": False, "reason": "no CGM data"}
        age = (end - last).total_seconds() / 60
        if age > max_age_minutes:
            return {
                "available": False,
                "reason": f"latest CGM reading is {age:.0f} min old",
                "as_of": last.isoformat(),
            }
        df = grid_from_db(session, last - timedelta(hours=self.history * 5 / 60 + 8), last)
        valid = df.index[df["glucose"].notna()]
        last_ts = valid[-1]
        sub = df.loc[:last_ts]
        X, _, idx = make_samples(sub, self.history, self.horizons, require_target=False)
        if len(X) == 0 or idx[-1] != len(sub) - 1:
            return {"available": False, "reason": "gap in the last CGM window", "as_of": last.isoformat()}
        F = feature_matrix(X[-1:], sub, idx[-1:])
        pred = self.predict_features(F, X[-1:, -1, 0])[0]
        return {
            "available": True,
            "as_of": last_ts.to_pydatetime().isoformat(),
            "age_minutes": round(age, 1),
            "last_value": float(X[-1, -1, 0]),
            "forecast": {f"{h * 5}min": round(float(pred[k]), 1) for k, h in enumerate(self.horizons)},
            "expected_rmse": self.meta.get("val_rmse"),
            "iob": float(sub["iob"].iloc[-1]),
            "cob": float(sub["cob"].iloc[-1]),
        }

    # ------------------------------------------------------------ persistence
    @staticmethod
    def default_path() -> Path:
        return Path(settings.photo_dir).parent / "forecast_model.json"

    def save(self, path: Path | None = None) -> Path:
        path = path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "history": self.history,
                    "horizons": list(self.horizons),
                    "meta": self.meta,
                    "boosters": [b.model_to_string() for b in self.boosters],
                }
            )
        )
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> GlucoseForecaster | None:
        path = path or cls.default_path()
        if not path.exists():
            return None
        lgb = _lgb()
        d = json.loads(path.read_text())
        m = cls(history=d["history"], horizons=d["horizons"])
        m.meta = d["meta"]
        m.boosters = [lgb.Booster(model_str=s) for s in d["boosters"]]
        return m


def evaluate_holdout(
    session: Session, days: int = 120, history: int = DEFAULT_HISTORY, test_fraction: float = 0.2
) -> dict:
    """Chronological holdout on the user's own data: train on the first part, score the last `test_fraction`.
    Reports RMSE / MAE against persistence so the number is interpretable."""
    end = now_utc()
    df = grid_from_db(session, end - timedelta(days=days), end)
    X, y, idx = make_samples(df, history, HORIZONS)
    if len(X) < 500:
        raise ValueError("Not enough history to evaluate")
    F = feature_matrix(X, df, idx)
    delta = y - X[:, -1, 0:1]
    ntest = int(len(X) * test_fraction)
    nval = int((len(X) - ntest) * 0.2)
    m = GlucoseForecaster(history).fit_arrays(
        F[: -ntest - nval], delta[: -ntest - nval], F[-ntest - nval : -ntest], delta[-ntest - nval : -ntest]
    )
    pred = m.predict_features(F[-ntest:], X[-ntest:, -1, 0])
    persist = np.repeat(X[-ntest:, -1, 0:1], len(HORIZONS), axis=1)
    out = {"n_test": int(ntest), "n_train": int(len(X) - ntest), "history": history}
    for k, h in enumerate(HORIZONS):
        key = f"{h * 5}min"
        err, perr = pred[:, k] - y[-ntest:, k], persist[:, k] - y[-ntest:, k]
        out[key] = {
            "rmse": float(np.sqrt(np.mean(err**2))),
            "mae": float(np.mean(np.abs(err))),
            "persistence_rmse": float(np.sqrt(np.mean(perr**2))),
            "persistence_mae": float(np.mean(np.abs(perr))),
        }
    return out


_ = datetime
