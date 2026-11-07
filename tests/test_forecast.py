from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("lightgbm")

from hypovisor.forecast.features import (  # noqa: E402
    EXOG,
    HORIZONS,
    feature_matrix,
    grid_from_db,
    make_samples,
)
from hypovisor.forecast.model import GlucoseForecaster, evaluate_holdout  # noqa: E402
from hypovisor.forecast.physio import cob_kernel, iob_kernel  # noqa: E402
from hypovisor.importers import import_clarity_csv, import_tandem_source  # noqa: E402
from hypovisor.models import GlucoseReading  # noqa: E402


def test_kernels_integrate_to_one():
    iob, act = iob_kernel()
    assert iob[0] == 1.0 and abs(act.sum() - 1.0) < 0.05
    cob, rate = cob_kernel()
    assert abs(rate.sum() - 1.0) < 1e-9 and cob[-1] < 1e-9


def test_grid_and_samples_from_synthetic(session, synth_dir):
    import_clarity_csv(session, synth_dir / "clarity_export.csv")
    import_tandem_source(session, synth_dir / "tandem_source_export")
    lo = session.query(GlucoseReading.ts).order_by(GlucoseReading.ts).first()[0]
    hi = session.query(GlucoseReading.ts).order_by(GlucoseReading.ts.desc()).first()[0]
    df = grid_from_db(session, lo, hi)
    assert df["glucose"].notna().mean() > 0.9
    assert (df["bolus"] > 0).sum() > 10 and (df["carbs"] > 0).sum() > 10 and df["basal_rate"].max() > 0
    assert df["iob"].max() > 1.0 and df["cob"].max() > 10
    X, y, idx = make_samples(df, 24, HORIZONS)
    assert X.shape[1:] == (24, 1 + len(EXOG)) and y.shape[1] == 2 and len(X) > 1000
    F = feature_matrix(X, df, idx)
    assert F.shape[0] == len(X) and not np.isnan(F).any()


def test_forecaster_beats_persistence_and_roundtrips(session, synth_dir, tmp_path, monkeypatch):
    from hypovisor import forecast as fc

    import_clarity_csv(session, synth_dir / "clarity_export.csv")
    import_tandem_source(session, synth_dir / "tandem_source_export")
    hi = session.query(GlucoseReading.ts).order_by(GlucoseReading.ts.desc()).first()[0]
    # pretend "now" is just after the last reading so the data is fresh
    monkeypatch.setattr(fc.model, "now_utc", lambda: hi + timedelta(minutes=2))
    monkeypatch.setattr(fc.features, "load_series", fc.features.load_series)
    ev = evaluate_holdout(session, days=30)
    assert ev["30min"]["rmse"] < ev["30min"]["persistence_rmse"]
    assert ev["60min"]["rmse"] < ev["60min"]["persistence_rmse"]
    m = GlucoseForecaster()
    meta = m.fit_from_db(session, days=30)
    assert meta["val_rmse"]["30min"] < meta["val_rmse_persistence"]["30min"]
    path = m.save(tmp_path / "model.json")
    m2 = GlucoseForecaster.load(path)
    assert m2 is not None and len(m2.boosters) == 2 and m2.meta["trained_at"] == meta["trained_at"]
    p = m2.predict_latest(session)
    assert p["available"] is True and set(p["forecast"]) == {"30min", "60min"}
    assert 40 <= p["forecast"]["30min"] <= 400
    # stale data -> not available
    monkeypatch.setattr(fc.model, "now_utc", lambda: hi + timedelta(hours=3))
    assert m2.predict_latest(session)["available"] is False


def test_forecast_api_without_model(client):
    r = client.get("/api/forecast")
    assert r.status_code == 200 and r.json()["available"] is False


_ = datetime
