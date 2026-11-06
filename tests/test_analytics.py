from datetime import datetime, timedelta

import numpy as np

from hypovisor.analytics.glucose import Series, hourly_profile, interpolate_at, summary_metrics
from hypovisor.analytics.insulin import activity_fraction, bolus_iob_at, iob_fraction
from hypovisor.analytics.meal_response import compute_response, recompute_all
from hypovisor.models import Bolus, GlucoseReading, Meal


def test_iob_curve_shape():
    assert iob_fraction(0)[0] == 1.0
    assert iob_fraction(299)[0] < 0.02
    assert iob_fraction(300)[0] == 0.0
    f = iob_fraction(np.array([30, 60, 120, 180]))
    assert np.all(np.diff(f) < 0)
    act = activity_fraction(np.arange(0, 300, 5))
    assert abs(act.sum() * 5 - 1.0) < 0.05  # integrates to ~1 unit
    assert 60 <= np.argmax(act) * 5 <= 90  # peak near 75 min


def test_bolus_iob_with_extended():
    t = datetime(2026, 1, 1, 12, 0)
    b = Bolus(
        ts=t, units_total=4, units_normal=2, units_extended=2, extended_minutes=120, kind="dual", source="t"
    )
    assert abs(bolus_iob_at([b], t) - 2.0 - 2 / 24) < 0.01  # first extended micro-dose starts at t
    # IOB rises while the extended part is still being delivered, then decays
    assert bolus_iob_at([b], t + timedelta(minutes=60)) > bolus_iob_at([b], t + timedelta(minutes=10))
    assert bolus_iob_at([b], t + timedelta(minutes=240)) < bolus_iob_at([b], t + timedelta(minutes=120))
    assert bolus_iob_at([b], t + timedelta(hours=8)) == 0.0


def test_summary_metrics_and_profile():
    v = np.array([60, 100, 150, 200, 300, np.nan])
    m = summary_metrics(v)
    assert m["n"] == 5 and m["tir"] == 40.0 and m["tbr"] == 20.0 and m["tar"] == 40.0
    ts = np.array(
        [np.datetime64("2026-01-01T05:00:00", "s") + np.timedelta64(5 * i, "m") for i in range(288)]
    )
    s = Series(ts, np.full(288, 120.0), np.full(288, -300))
    prof = hourly_profile(s)
    assert len(prof) == 24 and prof[0]["n"] == 12 and prof[0]["p50"] == 120.0
    assert interpolate_at(s, datetime(2026, 1, 1, 5, 2, 30)) == 120.0
    assert interpolate_at(s, datetime(2026, 1, 2, 9, 0)) is None


def _load_series(session, start: datetime, values, offset=-300):
    for i, v in enumerate(values):
        session.add(
            GlucoseReading(
                ts=start + timedelta(minutes=5 * i),
                utc_offset_min=offset,
                value_mgdl=v,
                source="dexcom_clarity",
            )
        )
    session.flush()


def test_meal_response_green_and_red(session):
    t0 = datetime(2026, 2, 1, 17, 0)  # 12:00 local
    # green: 110 baseline, modest rise to 160, back down
    rise = [110] * 6 + [110 + 50 * np.sin(np.pi * k / 36) for k in range(36)] + [112] * 40
    _load_series(session, t0 - timedelta(minutes=30), rise)
    session.add(
        Bolus(
            ts=t0 - timedelta(minutes=15),
            utc_offset_min=-300,
            units_total=5,
            units_normal=5,
            carbs_entered=50,
            source="tandem_source",
        )
    )
    meal = Meal(ts=t0, utc_offset_min=-300, description="green meal", carbs_g=50, status="confirmed")
    session.add(meal)
    session.flush()
    r = compute_response(session, meal)
    assert r.score == "green", r.score_reason
    assert r.bolus_units == 5 and r.prebolus_min == 15 and r.carbs_entered_on_pump == 50
    assert 150 <= r.peak_mgdl <= 165 and 60 <= r.time_to_peak_min <= 120
    assert r.low_event is False and r.late_rise is False and r.local_hour == 12
    assert r.tir_0_6h == 100.0

    # red: goes to 280 and then crashes to 60
    t1 = datetime(2026, 2, 2, 23, 0)
    curve = (
        [120] * 6
        + [120 + 160 * np.sin(np.pi * k / 48) for k in range(48)]
        + [80, 70, 62, 58, 65, 80]
        + [110] * 22
    )
    _load_series(session, t1 - timedelta(minutes=30), curve)
    meal2 = Meal(ts=t1, utc_offset_min=-300, description="red meal", carbs_g=90, status="confirmed")
    session.add(meal2)
    session.flush()
    r2 = compute_response(session, meal2)
    assert r2.score == "red" and r2.low_event is True and r2.peak_mgdl > 250
    assert r2.bolus_units is None
    assert recompute_all(session) == 2
