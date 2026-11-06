from datetime import datetime, timedelta


def _load(client, synth_dir):
    with open(synth_dir / "clarity_export.csv", "rb") as fh:
        r = client.post("/api/import/clarity", files={"file": ("clarity.csv", fh, "text/csv")})
    assert r.status_code == 200, r.text
    import shutil

    zip_path = shutil.make_archive(str(synth_dir / "tandem"), "zip", synth_dir / "tandem_source_export")
    with open(zip_path, "rb") as fh:
        r = client.post("/api/import/tandem", files={"file": ("tandem.zip", fh, "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_timeline_and_meal_flow(client, synth_dir):
    out = _load(client, synth_dir)
    assert out["result"]["bolus_added"] > 10
    st = client.get("/api/status").json()
    assert st["counts"]["glucose"] > 1500 and st["counts"]["basal_doses"] > 1000
    first = datetime.fromisoformat(st["first_glucose"])
    day = (first + timedelta(days=2)).date().isoformat()
    tl = client.get(f"/api/timeline?day={day}").json()
    assert tl["metrics"]["n"] > 200 and tl["basal"] and tl["iob"]

    # Log a meal by text on a day with data, confirm with a correction, check response + food learning
    bolus_local = tl["boluses"][0]["local"]
    r = client.post(
        "/api/meals/estimate", data={"description": "turkey sandwich 45g carbs", "local_ts": bolus_local}
    )
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["status"] == "draft" and m["carbs_g"] == 45 and m["error"] is None
    r = client.put(
        f"/api/meals/{m['id']}",
        json={
            "items": [
                {
                    "name": "turkey sandwich",
                    "carbs_g": 48,
                    "fat_g": 12,
                    "protein_g": 28,
                    "user_corrected": True,
                }
            ],
            "confirm": True,
        },
    )
    d = r.json()
    assert (
        d["status"] == "confirmed" and d["carbs_g"] == 48 and d["items"][0]["food_name"] == "turkey sandwich"
    )
    assert d["response"]["score"] in ("green", "amber", "red") and d["response"]["bolus_units"] is not None
    assert len(d["curve"]) > 50
    foods = client.get("/api/foods").json()
    assert (
        foods[0]["name"] == "turkey sandwich"
        and foods[0]["carbs_g"] == 48
        and foods[0]["provenance"] == "corrected"
    )
    fo = client.get(f"/api/foods/{foods[0]['id']}").json()["outcomes"]
    assert fo["n_meals"] == 1 and fo["bolus_history"][0]["n"] == 1

    # second estimate of the same food uses the knowledge base match
    r = client.post(
        "/api/meals/estimate", data={"description": "turkey sandwich 45g carbs", "local_ts": bolus_local}
    )
    assert r.json()["items"][0]["food_name"] == "turkey sandwich"

    ov = client.get("/api/insights/overview?days=60").json()
    assert ov["overall"]["n"] > 1500 and len(ov["hourly"]) == 24 and ov["carb_ratio"]
    assert client.get("/").status_code == 200 and client.get("/insights").status_code == 200
    assert client.delete(f"/api/meals/{m['id']}").status_code == 200


def test_estimate_without_api_key_reports_error(client):
    r = client.post("/api/meals/estimate", data={"description": "a big bowl of pasta"})
    assert r.status_code == 200
    assert "ANTHROPIC_API_KEY" in (r.json()["error"] or "")
    assert r.json()["status"] == "draft"


def test_events_and_lifecycle(client, synth_dir):
    _load(client, synth_dir)
    st = client.get("/api/status").json()
    first = datetime.fromisoformat(st["first_glucose"])
    r = client.post(
        "/api/events", json={"local_ts": (first + timedelta(days=1)).isoformat(), "kind": "site_change"}
    )
    assert r.status_code == 200
    ov = client.get("/api/insights/overview?days=60").json()
    assert ov["site_lifecycle"] and ov["site_lifecycle"][0]["day"] == 0
