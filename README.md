# Hypovisor

A personal, self-hosted type 1 diabetes data platform. It joins insulin pump data (basal and bolus),
CGM data, and food intake on one timeline, builds a personal food knowledge base from photos or text
with model-estimated nutrients, and mines the joined history for patterns: how each food behaves,
what bolus has worked for it, time-of-day sensitivity, dawn rise, site and sensor age, and more.

It shows your own history. It is **not** a bolus calculator and does not recommend doses.

Initial target: **Dexcom G7 + Tandem t:slim X2**, via their official CSV exports. Sync adapters for
Nightscout and the Dexcom Developer API are included as well.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env            # set HYPOVISOR_TIMEZONE and (optionally) ANTHROPIC_API_KEY
hypovisor synth ./data/synth --load   # optional: 21 days of synthetic G7 + t:slim data and meals
hypovisor serve                       # http://localhost:8000
```

Then import your real exports from the **Import** page (or the CLI):

```bash
hypovisor import clarity ~/Downloads/Clarity_Export_....csv
hypovisor import tandem  ~/Downloads/Tandem_Source_Export.zip
hypovisor import nightscout-json ~/nightscout-backup/     # entries*.json(.gz) + treatments*.json(.gz)
```

- **Dexcom Clarity**: clarity.dexcom.com → Reports → export icon. Any language, comma or semicolon separated.
- **Tandem Source**: source.tandemdiabetes.com → Reports → export CSV. Upload the zip or the folder's CSVs.

Exports contain local times without a timezone, so `HYPOVISOR_TIMEZONE` must be the zone the devices were in.

## What it does

| Area | What you get |
|---|---|
| Timeline | Glucose, boluses (bubble size = units), basal rate, bolus IOB, meals and events per day. Daily TIR / mean / CV / GMI. |
| Meals | Photo or text → Claude structured estimate (items, grams, carbs with low/high bounds, fat, protein, fiber, confidence). You confirm or correct every number. `45g carbs pizza` works offline. |
| Food knowledge base | Confirmed items become personal foods. Corrections overwrite model values; later model guesses never overwrite a correction. Fuzzy matching reuses your saved foods on the next photo. |
| Meal response | For every meal: baseline, peak, time to peak, Δ1–4h, iAUC, TIR 0–2h / 2–6h, low events, late rise, plus insulin context (bolus for the meal, pre-bolus lead, IOB at the meal, basal rate). Scored green / amber / red. |
| Per-food cards | What has worked: outcomes grouped by bolus size and pre-bolus lead, with overlaid response curves. |
| Insights | AGP-style hourly profile, day of week, overnight / dawn rise, meal outcomes by slot, observed ISF from correction boluses, observed carb ratio per slot, fasting drift by hour (autotune-lite), TIR by site / sensor age. |
| Import / sync | Clarity CSV, Tandem Source export, Nightscout API v3 pull (works with tconnectsync), Dexcom Developer API v3 (OAuth, ~1 h delayed). All imports are idempotent. |
| Forecast | 30 and 60 minute glucose forecast from the latest CGM window plus insulin/carb physiology, trained on your own history (LightGBM). Shown on the dashboard with its validation error. Chosen by benchmarking 15 model families on OhioT1DM; see `docs/research/02-glucose-forecasting-benchmark.md`. |

## Layout

```
hypovisor/
  models.py            canonical event store (glucose, boluses, basal doses, pump events, meals, foods, responses)
  importers/           dexcom_clarity, tandem_source, nightscout, dexcom_api
  analytics/           glucose metrics, insulin (IOB), meal_response, patterns, settings_estimate
  food/                schemas (structured output), estimator (Claude), knowledge_base, reference (USDA / Open Food Facts)
  api/                 FastAPI routes + Jinja pages;  web/  templates and static assets (installable PWA)
  synth.py             synthetic Clarity + Tandem exports with per-food glucose behaviour
  cli.py               hypovisor init-db | import | meals-csv | synth | recompute | serve
tests/                 importers (incl. German semicolon Clarity), analytics, food KB, API flow
```

SQLite by default (`HYPOVISOR_DATABASE_URL`); Postgres works with the `postgres` extra.

## Food estimation

Set `ANTHROPIC_API_KEY`. The estimator calls `claude-opus-5-5` (override with `HYPOVISOR_ANTHROPIC_MODEL`)
with a strict JSON schema (`hypovisor/food/schemas.py`), passing the photo, your text, and the best-matching
saved foods as context. Published evaluations show photo-based carb estimates are the weakest macro, so the
UI shows ranges and confidence and makes correction one tap. Reference lookups: USDA FoodData Central
(`USDA_FDC_API_KEY`) and Open Food Facts barcodes (`/api/reference/barcode/{code}`).

## Forecasting

```bash
pip install -e ".[forecast]"
hypovisor forecast evaluate      # chronological holdout on your data, RMSE vs persistence
hypovisor forecast train         # saves data/forecast_model.json; dashboard and GET /api/forecast use it
```

On OhioT1DM (12 people, standard protocol) the shipped model scores 18.4 / 30.7 mg/dL RMSE at 30 / 60 minutes
trained per person, and 17.9 / 29.8 pooled across people, versus 23.3 / 38.1 for repeating the last value. On 39
OpenAPS Data Commons donors (90 days each) it scores 19.1 / 30.3 per person versus 23.7 / 37.0 for persistence.
To reproduce with your own copies: `python scripts/benchmark_ohio.py /path/to/OhioT1DM --mode population --pid`
or `python scripts/benchmark_ohio.py /path/to/openaps --dataset openaps`.
The forecast is informational; it is not a dosing aid.

## Sync options

- **Nightscout**: set `NIGHTSCOUT_URL` and a read token in `NIGHTSCOUT_TOKEN`, then `POST /api/sync/nightscout`
  (or the button on the Import page). Pulls entries, treatments (boluses, carbs, temp basals, site changes).
  For a t:slim X2 this is the practical near-real-time path: run
  [tconnectsync](https://github.com/jwoglom/tconnectsync) → Nightscout → Hypovisor.
- **Dexcom Developer API**: register an app at developer.dexcom.com, set `DEXCOM_CLIENT_ID/SECRET`, visit
  `/api/sync/dexcom/authorize`, then `POST /api/sync/dexcom`. Data is delayed about one hour (US).
  Use `DEXCOM_BASE_URL=https://sandbox-api.dexcom.com` to test with Dexcom's sandbox users.

## API

Interactive docs at `/docs`. Main endpoints: `/api/timeline?day=`, `/api/meals/estimate` (multipart photo +
description), `PUT /api/meals/{id}` (confirm / correct), `/api/foods/{id}` (outcomes), `/api/insights/overview?days=`.

## Safety notes

- Everything here is descriptive. Targets, insulin model (DIA, peak) and timezone are in `.env`.
- Hypovisor never writes carbs or insulin to a pump, Nightscout, or Apple Health.
- Settings observations (ISF, ICR, basal drift) are sample-size-labelled history to discuss with your clinician.

## Development

```bash
pytest -q
ruff check hypovisor tests
```

Research notes that led to this design are in `docs/research/`.
