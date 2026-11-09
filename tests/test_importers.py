from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from hypovisor.importers import import_clarity_csv, import_tandem_source
from hypovisor.models import BasalDose, Bolus, GlucoseReading, PumpEvent

FIX = Path(__file__).parent / "fixtures"


def test_clarity_german_semicolon(session):
    r = import_clarity_csv(session, FIX / "clarity_de_sample.csv")
    assert r.glucose_added == 3
    rows = session.scalars(select(GlucoseReading).order_by(GlucoseReading.ts)).all()
    # 06:00 New York in January is 11:00 UTC
    assert rows[0].ts == datetime(2026, 1, 1, 11, 0, 0)
    assert rows[0].utc_offset_min == -300
    assert rows[0].value_mgdl == 110
    assert rows[1].value_mgdl is None and rows[1].raw_label == "Low"
    assert rows[2].value_mgdl is None and rows[2].raw_label == "High"
    kinds = {e.kind for e in session.scalars(select(PumpEvent))}
    assert kinds == {"bg_meter", "alert"}


def test_clarity_english_synthetic_and_idempotent(session, synth_dir):
    r1 = import_clarity_csv(session, synth_dir / "clarity_export.csv")
    assert r1.glucose_added > 1500
    r2 = import_clarity_csv(session, synth_dir / "clarity_export.csv")
    assert r2.glucose_added == 0 and r2.glucose_skipped == r1.glucose_added


def test_clarity_rejects_other_csv(session, tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b,c\n1,2,3\n")
    try:
        import_clarity_csv(session, p)
    except ValueError as e:
        assert "14 columns" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_tandem_source_folder(session, synth_dir):
    r = import_tandem_source(session, synth_dir / "tandem_source_export")
    assert r.bolus_added > 10 and r.basal_added > 1000 and r.glucose_added > 1500
    b = session.scalars(select(Bolus).where(Bolus.carbs_entered.is_not(None)).order_by(Bolus.ts)).first()
    assert b.kind == "normal" and b.reason in ("food", "food_correction") and b.units_total > 0
    assert b.icr == 10.0 and b.isf == 45.0
    d = session.scalars(select(BasalDose).order_by(BasalDose.ts)).first()
    assert d.duration_min == 5.0 and d.rate_uph is not None
    r2 = import_tandem_source(session, synth_dir / "tandem_source_export")
    assert r2.bolus_added == 0 and r2.basal_added == 0


def test_tandem_bolus_classification(session, tmp_path):
    hdr = (
        "Type,Bolus Type,Bolus Delivery Method,BG (mg/dL),Serial Number,Completion Date Time,Insulin Delivered,Food Delivered,"
        "Correction Delivered,Completion Status Desc,Bolex Start Date Time,Bolex Completion Date Time,Bolex Insulin Delivered,"
        "Bolex Completion Status Desc,Standard Percent,Duration (mins),Carb Size,Target BG (mg/dL),Correction Factor,Carb Ratio\n"
    )
    rows = [
        "Bolus,Food / Correction,Standard,158,1,2026-03-05T04:51:44,2.48,1.89,0.59,Completed,,,,,100,0,17,110,45,10\n",
        "Bolus,Food,Extended,,1,2026-03-05T12:00:00,5.0,5.0,0,Completed,2026-03-05T12:00:00,2026-03-05T14:00:00,2.5,Completed,50,120,60,110,45,10\n",
        "Bolus,Automatic Bolus,Standard,210,1,2026-03-05T16:00:00,1.2,0,1.2,Completed,,,,,100,0,0,110,45,10\n",
    ]
    p = tmp_path / "X_1_date-Bolus.csv"
    p.write_text("﻿" + hdr + "".join(rows), encoding="utf-8")
    r = import_tandem_source(session, p)
    assert r.bolus_added == 3
    kinds = {b.kind: b for b in session.scalars(select(Bolus))}
    assert set(kinds) == {"normal", "dual", "automated"}
    assert (
        kinds["dual"].units_extended == 2.5
        and kinds["dual"].units_normal == 2.5
        and kinds["dual"].extended_minutes == 120
    )
    assert kinds["automated"].reason == "auto"
    assert kinds["normal"].carbs_entered == 17 and kinds["normal"].bg_entered == 158


def test_merged_cgm_prefers_clarity(session, synth_dir):
    from hypovisor.analytics.glucose import load_series

    import_clarity_csv(session, synth_dir / "clarity_export.csv")
    import_tandem_source(session, synth_dir / "tandem_source_export")
    total = session.scalar(select(func.count()).select_from(GlucoseReading))
    lo = session.scalar(select(func.min(GlucoseReading.ts)))
    hi = session.scalar(select(func.max(GlucoseReading.ts)))
    s = load_series(session, lo, hi)
    # Tandem relays the same readings 11 s later; merge must not double count
    assert len(s) < total * 0.6


def test_nightscout_json_files(session, tmp_path):
    import json

    from hypovisor.importers.nightscout import import_nightscout_files

    entries = [
        {"_id": "a", "date": 1533427130000, "sgv": 150, "type": "sgv", "device": "xDrip"},
        {"_id": "b", "date": 1533427430000, "sgv": 155, "type": "sgv"},
        {"_id": "c", "date": 1533427730000, "sgv": "bad", "type": "sgv"},
    ]
    treatments = [
        {
            "_id": "t1",
            "created_at": "2018-08-04T23:01:08Z",
            "eventType": "Correction Bolus",
            "insulin": 1.5,
            "carbs": None,
        },
        {"_id": "t2", "date": 1533426425552, "eventType": "Carb Correction", "carbs": 17, "insulin": None},
        {
            "_id": "t3",
            "date": 1533426000000,
            "eventType": "Temp Basal",
            "rate": 0.3,
            "duration": 30,
            "enteredBy": "openaps",
        },
        {"_id": "t4", "date": 1533426100000, "eventType": "Meal Bolus", "insulin": 4, "carbs": 40},
        {"_id": "t5", "date": 1533426200000, "eventType": "Site Change", "notes": "left"},
    ]
    (tmp_path / "entries.json").write_text(json.dumps(entries))
    (tmp_path / "treatments.json").write_text(json.dumps(treatments))
    r = import_nightscout_files(session, [tmp_path / "entries.json", tmp_path / "treatments.json"])
    assert r.glucose_added == 2 and r.bolus_added == 2 and r.basal_added == 1 and r.events_added == 2
    b = {x.reason: x for x in session.scalars(select(Bolus))}
    assert b["correction"].units_total == 1.5 and b["food"].carbs_entered == 40
    d = session.scalars(select(BasalDose)).one()
    assert d.rate_uph == 0.3 and d.units == 0.15 and d.is_temp
    assert {e.kind for e in session.scalars(select(PumpEvent))} == {"carbs_logged", "site_change"}
