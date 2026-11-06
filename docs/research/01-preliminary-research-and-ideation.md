# Hypovisor: preliminary research and ideation

*Status: research notes, nothing implemented yet. Written 2026-11-06.*

## 1. What we are building

A personal, centralized T1D data platform that:

1. Pulls insulin pump data (basal rates, boluses, IOB, settings) and CGM data into one timeline.
2. Lets the user log food by photo or free-text, builds a personal food knowledge base, and fills in nutrient estimates (carbs first, then fat, protein, fiber) when the user does not supply them.
3. Analyzes the joined timeline to surface patterns (time-of-day, per-food, per-context) and to estimate what bolus has historically worked for a given food.

The rest of this document covers what the research says about each of those three pillars, then a proposed architecture, an MVP cut, and open questions.

---

## 2. Data access reality check

This is the part that most constrains the design. The short version: **there is no official, real-time, developer-facing API for any consumer pump, and only one official (delayed) API for a CGM.** Everything else is either a manual export, a community-reverse-engineered client, or routed through Nightscout.

### 2.1 CGM

| Source | Route | Latency | Official? | Notes |
|---|---|---|---|---|
| Dexcom G6/G7/ONE | Dexcom Developer API v3 (OAuth 2.0) | ~1 h US, ~3 h outside US | Yes | Sandbox is instant; "Limited" tier covers up to 5 users, fine for personal use. Endpoints: egvs, events, calibrations, devices, alerts, dataRange. Polling only. |
| Dexcom (any) | Dexcom Share via `pydexcom` | ~real-time (5 min) | No | Requires a "follower" set up in the Dexcom app. Breaks occasionally when Share changes. Used by Home Assistant. |
| Dexcom (any) | Clarity CSV export | Manual | Yes | 14-column CSV with metadata header; good for historical backfill. |
| FreeStyle Libre 2/3 | LibreLinkUp unofficial clients (Node, Rust) | ~real-time | No | Reverse engineered, regional endpoints. |
| Any CGM on Android | xDrip+ local web service on port 17580 | real-time | Community | Emulates Nightscout `/sgv.json` and `/treatments.json`. |
| Any CGM | Nightscout `entries` collection | real-time if an uploader exists | Community | De facto interchange format. |
| Any CGM on iOS | Apple HealthKit `bloodGlucose` | near real-time | Yes | Dexcom and Loop both write here. Requires an iOS app as the reader. |

### 2.2 Pump

| Pump | Route | Latency | Official? | Notes |
|---|---|---|---|---|
| Tandem t:slim X2 / Mobi | `tconnectsync` (Tandem Source undocumented API) | adaptive polling, ~5 to 25 min; CGM relay lags 30+ min | No | Pulls basal, bolus, pump events, insulin profiles (basal rates, ICR, ISF). Writes to Nightscout treatments. US Eastern is the only tested timezone. |
| Tandem | Tandem Source CSV export (zipped folder: basal, bolus, CGM, BG, hourly basal) | Manual | Yes | Local-time only, no timezone offset in the file. |
| Omnipod 5 | Glooko (no public consumer API); unofficial clients (GlucoPilot, PodQuery MCP) log in with Glooko credentials | ~1 h | No | 2FA must be off for the unofficial route. Glooko has a FHIR API aimed at clinics. |
| Omnipod DASH / Eros | Loop, Trio, AndroidAPS | real-time | Community | The DIY loop app is the pump controller and already uploads everything to Nightscout. |
| Medtronic 770G/780G | CareLink; xDrip+ "CareLink follower" to Nightscout | delayed | No | The older `minimed-connect-to-nightscout` scraper is dead. |
| Any pump on iOS | Apple HealthKit `insulinDelivery` (basal/bolus tagged) | varies | Yes | Tandem's t:connect app and Loop write here. Whether the Omnipod 5 app does should be verified. |

### 2.3 Why Nightscout matters

Nightscout is the lingua franca of the DIY diabetes world. Its API v3 has collections for `entries` (CGM), `treatments` (carbs, insulin, temp basals, site changes, notes), `devicestatus` (IOB, COB, predicted BG from a loop), `profile` (basal schedule, ICR, ISF, DIA) and `food`. Nearly every uploader, DIY loop, and dashboard already speaks it. Two consequences for us:

- **Adopt Nightscout's data model as a compatibility surface.** If Hypovisor can *read from* and *write to* a Nightscout instance, it inherits every existing uploader (tconnectsync, xDrip+, Loop, Trio, AAPS) for free. We do not need to build pump drivers.
- **Do not make Nightscout the canonical store.** It is MongoDB-shaped, lacks the food/photo/nutrient model we need, and its treatment schema only has `carbs`, `protein`, `fat`, `insulin`, `notes`. We want a richer, relational, timezone-correct model and then sync a lossy projection to Nightscout if desired.

### 2.4 Apple Health as an alternative hub

On iOS, HealthKit already holds blood glucose, insulin delivery (with basal and bolus reasons), and dietary carbohydrates written by the Dexcom, Tandem and Loop apps. A small iOS companion app that exports HealthKit samples to our backend could replace per-vendor adapters for a Dexcom + Tandem user. Downside: requires shipping an iOS app, and LoopDocs warn that carb entries written to Health by third-party apps can be picked up by Loop and dosed on, so our app must **not** write carbs to Health unless the user is explicitly opting in.

### 2.5 Recommendation

Build an **adapter layer** with a canonical event model, and ship adapters in this priority order:

1. **Nightscout reader** (covers DIY loopers and anyone running tconnectsync / xDrip+).
2. **File importers**: Dexcom Clarity CSV, Tandem Source export, Glooko export. These are official, complete, and perfect for backfilling a year of history on day one.
3. **Dexcom Developer API** (official, delayed, fine for retrospective analysis).
4. **pydexcom / Dexcom Share** for the live dashboard, clearly labeled unofficial.
5. **Vendor-specific unofficial clients** (tconnectsync, Glooko scrapers) wrapped so a breaking change disables one adapter rather than the platform.

The open question that determines ordering is **which pump and CGM you actually use** (see section 7).

---

## 3. Food pipeline

### 3.1 What the evidence says about photo-based carb estimation

- A 2025 *Nutrients* study of GPT-4 on 114 meal photos found mean carbohydrate error around minus 6.5 percent, but agreement with reference values was poor for carbs (ICC 0.31) versus moderate-to-good for protein (0.67). Energy and protein were "comparable to dietitians"; carbs were the weak macro.
- A Finnish cafeteria study (192 images) found visually ambiguous items like sauces are consistently hard, and that giving the model a food-composition database as context helped carb estimation (more for Gemini than GPT).
- A 2026 *JMIR Diabetes* paper looked at run-to-run variability of GPT-4o and GPT-5.1 nutrient estimates from the same image, which is a separate problem from accuracy and matters for us: the same photo should not get different answers on different days.
- Nutrition5k and DPF-Nutrition show that adding depth or volume information cuts mass error roughly in half (29.5 percent to 13.7 percent MAE in the original Nutrition5k baseline). Monocular depth estimation recovers some of that.
- SNAQ, a commercial T1D photo-carb app with Dexcom and Libre integration, got a mixed independent review ("helps you carb count badly") and a company-cited 38 percent reduction in estimation error. Undermyfork takes the opposite approach: it does not estimate carbs at all, it pairs photos with the 2-hour post-meal CGM trace, colors each meal green/amber/red, and shows the insulin dose logged with each meal. A small study (n=21) reported an 11 percentage-point TIR improvement.
- Undermyfork's current site (checked 2026-11) says it "automatically takes glucose and insulin data from your phone" via Apple Health, so on iPhone it gets bolus units from any pump app that writes to HealthKit (Tandem t:connect, Loop). It does not have basal, IOB, loop state or pump settings, nutrient estimation, a learned personal food base (comparison is by user-entered tags), temporal or confounder analysis, what-if, or data export. Those gaps are the scope of this project; the HealthKit route is a point in favor of an iOS companion app for pump data.

**Design implications**

1. **Treat every model estimate as a prior with an uncertainty band, never a number.** Show "35 to 55 g" with a confidence, not "45 g".
2. **Make correction one tap.** The user's own correction is the most valuable training signal in the system.
3. **Prefer retrieval over generation.** Most people eat the same 30 to 50 things repeatedly. If the photo matches a food already in the personal knowledge base, reuse the known (user-corrected) values. Only fall back to fresh estimation for novel foods.
4. **Give the model context.** Pass the top candidates from USDA or Open Food Facts, the user's past entries for similar foods, and any text the user typed ("half a chipotle bowl, no rice") alongside the image.
5. **Portion is the hard part, not identification.** Ask for a reference object or use a consistent plate; consider optional second photo at an angle; let the user pick "small / my usual / large" relative to their own history.
6. **Capture the outcome, not only the input.** The CGM trace after the meal tells us whether the carb estimate was right in the way that matters. Section 4 uses this to self-correct.

### 3.2 Reference nutrient databases

| Database | Best for | Access | License |
|---|---|---|---|
| USDA FoodData Central | Generic whole foods, restaurant and branded US items, authoritative reference values | Free API key, ~1,000 req/h; bulk CSV download available | Public domain |
| Open Food Facts | Packaged products by barcode, global coverage | Free REST API (v2 product by barcode; v1 for full-text search; v3 in progress) | ODbL, attribution required |
| Nutritionix, Edamam, Passio | Restaurant chains, commercial image SDKs | Paid, roughly $1,850+/month for Nutritionix | Commercial |

Plan: download the FDC Foundation + SR Legacy + Branded bulk files into Postgres so lookups are local and fast; hit Open Food Facts live only for barcode scans; skip commercial APIs for now.

### 3.3 Knowledge base design

Three layers:

- **Reference foods** (USDA / OFF rows, immutable, per 100 g).
- **Personal foods**: the user's own recurring items and composite meals ("my overnight oats", "Joe's Pizza plain slice"), each with a canonical portion, nutrient values, provenance (user-entered, model-estimated, corrected), an image set, and a text embedding for retrieval. Composite meals reference reference foods or other personal foods.
- **Meal events**: a timestamped instance of eating, linking one or more personal foods with portion multipliers, photos, free-text notes, the model's raw estimate, the user's final values, and later the computed glucose response.

Matching a new photo or description to the personal KB: embed the model's structured description (dish name, components, cuisine) and nearest-neighbor against personal foods, confirmed by the model with the candidate list in-context. pgvector in Postgres is enough at personal scale.

### 3.4 Model choice for recognition and estimation

Use a vision-capable Claude model with **structured output** so the response is a validated JSON object: list of components, each with name, estimated grams, carbs/fat/protein/fiber with low and high bounds, confidence, and the reference-database candidate it anchored on. The current default is `claude-opus-5-5` at $4 input / $20 output per million tokens, with adaptive thinking. A meal photo plus a short context prompt is a few thousand tokens, so a year of three meals a day is on the order of tens of dollars. Batch re-estimation (for example, re-running old photos after a prompt change) can use the Batch API at half price.

Glycemic descriptors to store beyond macros: glycemic index band (low/med/high, same as Nightscout's `gi` field), fat-protein units for extended-bolus logic, fiber, alcohol, liquid vs solid. The fat-protein unit convention is:

$$
\text{FPU} = \frac{9 \cdot \text{fat}_\text{g} + 4 \cdot \text{protein}_\text{g}}{100}
$$

with the Warsaw method treating one FPU as roughly equivalent to 10 g of carbohydrate delivered over 3 to 8 hours depending on FPU count. Evidence is mostly from one group and small trials, and a 2025 comparison found a simpler "add 30 percent of the carb bolus as extended" rule performed similarly. We should surface FPU as a feature for pattern analysis, not as a dosing rule.

---

## 4. Analytics and pattern layer

### 4.1 Meal response fingerprint

For each meal event, cut the CGM window from 30 minutes before to 6 hours after and compute:

- baseline glucose and trend at meal time,
- peak, time to peak, 2-hour and 4-hour delta,
- incremental area under the curve above baseline, defined as

$$
\text{iAUC}_{T} = \int_{0}^{T} \max\big(G(t) - G(0),\, 0\big)\, dt
$$

- time in range in the 0 to 2 h and 2 to 6 h windows (the late window catches fat/protein meals),
- insulin context: bolus units, pre-bolus lead time, IOB at meal start, basal rate, any loop automation (temp basals, SMBs, autobolus from devicestatus),
- confounders: exercise before/after, sensor age, site age, time of day, prior hypo.

Aggregating fingerprints per personal food (or per cluster of similar foods) gives the Undermyfork-style "this food usually goes green / amber / red" card, but with the insulin context attached so it is actionable.

### 4.2 Temporal patterns

- **Time-of-day sensitivity**: ratio of observed glucose change to expected change from IOB and COB, binned by hour, reveals dawn phenomenon and afternoon sensitivity shifts. oref0's autosens does a rolling 8 to 24 h version of this.
- **Settings estimation**: oref0 Autotune is open source, runs on Nightscout data, and estimates basal rates, ISF and ICR from history. We can either wrap it directly (it is a Node/JS tool) or reimplement the core deviation-attribution logic in Python. Its caveat applies to us too: recommendations are only as good as the starting settings and should move in small steps.
- **Day-of-week, weekday vs weekend, menstrual cycle, sleep, exercise**: simple stratified summaries first (AGP-style percentiles per stratum), statistical tests second.
- **Site and sensor lifecycle**: TIR vs hours since site change, vs sensor day. These are easy and often surprising.

### 4.3 Bolus estimation for a known food

Three levels of ambition, in order:

1. **Empirical lookup (MVP).** For a food eaten N times, show the table of (bolus, pre-bolus lead, IOB, starting BG) versus outcome (peak, 2 h TIR, lows). "Last 6 times you had this: 7 u with 15 min lead went green 4 of 4; 6 u went amber 2 of 2." No model, just honest history.
2. **Counterfactual replay.** ReplayBG (MATLAB, with the PyReplayBG Python port) fits a personalized glucose-insulin model to the user's own CGM, insulin and carb history and can then replay a meal with a different bolus. This answers "what if I had taken 8 u" with a physiologically constrained model rather than a regression. simglucose (Python UVA/Padova 2008) is the research-only simulator the same family of work validates against.
3. **Learned forecasting.** OhioT1DM-style sequence models (CNN-LSTM, Transformer) that take CGM, insulin and carb histories and predict 30 to 90 minutes ahead. GluFormer (Nature 2025) is a CGM foundation model trained on 10 million readings with a multimodal variant that adds dietary tokens to predict individual food responses; the code is public, weight availability needs checking. Public datasets for pretraining or benchmarking: OhioT1DM, AZT1D (CC BY 4.0, 25 AID users), DiaTrend, T1DEXI (DUA), OpenAPS Data Commons (DUA), and the 2026 MetaboNet consolidation of 21 sources. The cautionary result in the literature is that diary-based meal-to-meal prediction often barely beats a naive baseline, so level 3 is a research track, not a product dependency.

### 4.4 Safety framing

A bolus calculator is a regulated medical device in the US (FDA has said so since 2015, and the January 2026 reissue of the Clinical Decision Support guidance keeps patient-facing dosing software in scope). For a personal, self-hosted tool this is not a legal problem, but it should shape the UX:

- Present history and ranges, never a single recommended dose.
- Always show the pump's own calculator result alongside, and the assumptions (ICR, ISF, IOB) ours used.
- Hard guardrails: never suggest above the pump's max bolus; flag when IOB is high or glucose is falling; suppress suggestions when data is stale.
- Never write insulin or carbs back to a loop system without explicit opt-in, because Loop will dose on carb entries it sees in HealthKit or Nightscout.

---

## 5. Proposed architecture

### 5.1 Canonical event model

One append-only `events` table with subtype tables, all in UTC with the original local offset stored:

- `glucose` (value mg/dL, trend, device, source adapter, raw id)
- `insulin_bolus` (units, type: normal/extended/correction/automated, duration, requested vs delivered, carbs entered on pump, BG entered on pump)
- `basal` (scheduled rate, temp rate, percent, duration, suspend/resume)
- `loop_status` (IOB, COB, predictions, enacted action) from devicestatus
- `profile_snapshot` (basal schedule, ICR, ISF, DIA, targets, effective_from)
- `meal` (links to meal items, photos, notes, estimates, final values)
- `activity`, `note`, `site_change`, `sensor_change`, `illness`, `menstrual`
- `derived` (meal fingerprints, daily metrics, pattern findings), recomputable

Deduplication keys mirror Nightscout's (timestamp + eventType + device) so re-imports are idempotent.

### 5.2 Components

```
[Adapters]  nightscout | dexcom-api | pydexcom | clarity-csv | tandem-csv | tconnectsync | healthkit-companion
     |
     v
[Ingest worker, Python]  normalize -> events (Postgres + TimescaleDB)
     |
     v
[Food service]  photo upload -> Claude vision (structured JSON) -> KB match (pgvector) -> user confirm
     |
     v
[Analytics jobs]  meal fingerprints, temporal patterns, autotune-style settings, bolus history tables
     |
     v
[API, FastAPI]  <->  [Web app / PWA, Next.js: timeline, meal capture, food cards, insights]
                      [optional Nightscout write-back]
```

Choices and reasons:

- **Postgres + TimescaleDB + pgvector**: one database for time series, relational food KB, and embeddings. Personal scale (a few hundred thousand CGM rows a year) does not need anything heavier.
- **Python** for adapters and analytics because tconnectsync, pydexcom, PyReplayBG and simglucose are all Python, and the ML ecosystem is there.
- **PWA first, not native**: the phone's job is to take a photo and tag it. A PWA avoids app store friction. A thin iOS companion only if HealthKit turns out to be the best pump data path.
- **Self-hostable** (Docker Compose), single user initially, with a user id on every row so multi-user is not a rewrite.
- **Photos in object storage** (S3-compatible, MinIO locally), originals kept, thumbnails derived.

### 5.3 LLM usage boundaries

- Vision + structured output for food recognition and estimation.
- Natural-language food entry ("two eggs on sourdough with butter") parsed to the same structured schema.
- Natural-language questions over the derived data ("how does pizza go when I pre-bolus 20 minutes?") via tool use against the analytics API, with the model never computing numbers itself.
- Not used for: dose arithmetic, glucose forecasting, or anything in the safety path.

---

## 6. MVP cut and phasing

**Phase 0, backfill and look (1 to 2 weeks of work)**
- Importers for Clarity CSV and the pump vendor's export; Nightscout reader if applicable.
- Postgres schema; a timeline view with glucose, boluses, basal.
- Daily metrics (TIR, mean, CV, GMI) so the dashboard is immediately useful.

**Phase 1, food capture**
- PWA photo and text capture; Claude structured estimation with uncertainty; one-tap correction.
- Personal food KB with retrieval; USDA bulk load; barcode via Open Food Facts.

**Phase 2, meal fingerprints**
- Automatic 6-hour window analysis per meal; per-food cards with green/amber/red history and the insulin context table (section 4.3 level 1).
- Site/sensor lifecycle and time-of-day sensitivity views.

**Phase 3, settings and what-if**
- Autotune-style basal/ICR/ISF estimation with small-step recommendations.
- PyReplayBG integration for counterfactual bolus replay on past meals.

**Phase 4, live and predictive (optional)**
- pydexcom or Nightscout live feed; glucose forecasting experiments on the user's own data, benchmarked against naive baselines before anything ships.

---

## 7. Open questions that change the plan

1. **Which pump, CGM and phone?** Tandem + Dexcom + iPhone is the easiest combination (tconnectsync, Clarity, HealthKit all work). Omnipod 5 is the hardest (Glooko only, 1 h lag, unofficial). A DIY loop (Loop/Trio/AAPS) is the best case because Nightscout already has everything including IOB/COB and predictions.
2. **Is a Nightscout instance already running?** If yes, phase 0 shrinks to "point at it".
3. **Real-time or retrospective first?** Retrospective analysis delivers most of the value and avoids every unofficial API. Live views can be added later.
4. **Self-hosted or cloud?** Affects photo storage, auth, and whether HealthKit sync is practical.
5. **Appetite for unofficial APIs?** They break, and credential-based scrapers may violate vendor terms. The plan above keeps them optional.
6. **Single user forever, or shareable with a clinician or family?** Affects auth and data export (PDF AGP-style reports) priorities.

---

## 8. Risks

- **Vendor API churn**: tconnectsync, pydexcom and LibreLinkUp clients all document breakage. Mitigation: adapters are isolated, file importers are the fallback.
- **Carb estimation accuracy**: the literature says photo carbs are the weakest macro. Mitigation: retrieval over generation, uncertainty bands, user correction, outcome-based learning.
- **Timezones**: Tandem exports have no offset, Nightscout stores UTC plus offset, Clarity exports local time. Every adapter must capture the original offset or the per-food analysis will be silently wrong around travel and DST.
- **Confounded conclusions**: a meal's outcome depends on IOB, exercise, stress, and site age as much as the food. Mitigation: store confounders from day one, stratify before claiming a pattern, show sample sizes.
- **Safety creep**: the line between "history of what worked" and "recommended dose" is thin. Mitigation: the framing rules in section 4.4.

---

## 9. Existing projects worth studying or reusing

- Nightscout (`cgm-remote-monitor`): API v3 spec at `lib/api3/swagger.yaml`; `food` and `treatments` schemas.
- `tconnectsync` (Tandem Source to Nightscout), `pydexcom` (Dexcom Share), `libre-link-unofficial-api`.
- `oref0` Autotune (settings estimation), AndroidAPS and Trio (UAM and dynamic carb absorption logic worth reading).
- `replay-bg` and PyReplayBG (digital twin), `simglucose` (UVA/Padova in Python).
- GluFormer (CGM foundation model), OhioT1DM and AZT1D (datasets), MetaboNet (consolidated loader).
- `awesome-diabetes-software` (gcappon) as an index.
- Undermyfork and SNAQ as product references for the meal-photo-plus-CGM UX; Tidepool for the device-agnostic data model.
- `aidR` (R package) documents the Dexcom Clarity and Tandem Source export formats precisely.

---

## 10. Sources

CGM and pump access
- Dexcom API v3 endpoint overview: https://developer.dexcom.com/docs/dexcomv3/endpoint-overview
- Dexcom API integration guide (third party, 2026): https://www.themomentum.ai/blog/dexcom-api-integration-developer-guide
- pydexcom: https://pypi.org/project/pydexcom
- tconnectsync: https://github.com/jwoglom/tconnectsync
- Glooko / Omnipod 5 unofficial access (GlucoPilot wiki): https://github.com/Paco5687/GlucoPilot/wiki/Connecting-Data-Sources
- PodQuery MCP: https://github.com/rilhia/podquery-mcp/releases
- Omnipod 5 to Glooko (Insulet): https://www.omnipod.com/sites/default/files/HCP_Linking_Omnipod-5_Data_to_Glooko_US_English.pdf
- LibreLinkUp unofficial clients: https://www.github.com/DRFR0ST/libre-link-unofficial-api and https://docs.rs/crate/libre_link_up_api_client/latest
- Nightscout API v3 swagger: https://raw.githubusercontent.com/nightscout/cgm-remote-monitor/HEAD/lib/api3/swagger.yaml
- Nightscout API v3 tutorial: https://app.unpkg.com/nightscout@14.2.11/files/lib/api3/doc/tutorial.md
- Nightscout uploaders and Medtronic: https://nightscout.readthedocs.io/en/latest/uploader/uploaders.html and https://nightscout.readthedocs.io/en/latest/uploader/xdripcarelink.html
- xDrip+ local web services: https://github.com/NightscoutFoundation/xDrip/blob/master/Documentation/technical/Local_Web_Services.md
- Apple Health in Loop: https://loopkit.github.io/looptips/data/health and https://loopkit.github.io/loopdocs/faqs/apple-health-faqs
- Dexcom Clarity and Tandem Source export formats (aidR): https://cran.r-universe.dev/aidR/doc/format-dexcom-clarity.html and https://cran.r-universe.dev/aidR/doc/format-tandem-source.html
- Terra API Dexcom integration: https://tryterra.co/integrations/dexcom
- Tidepool platform paper (Howard Look et al.): https://scite.ai/authors/howard-look-gNRMLd

Food recognition and nutrient estimation
- GPT-4 meal photo study (Nutrients 2025): https://mdpi-res.com/d_attachment/nutrients/nutrients-17-00607/article_deploy/nutrients-17-00607.pdf and summary https://www.news-medical.net/news/20250210/AI-vs-dietitians-Can-ChatGPT-accurately-assess-your-meals.aspx
- Cafeteria LMM study (Turku): https://www.utupub.fi/bitstreams/28c52bb8-6313-4b7f-a0ca-3056ef0ea250/download
- Precision of GPT-4o / GPT-5.1 nutrient estimates (JMIR Diabetes 2026): https://diabetes.jmir.org/2026/1/e102715/PDF
- ChatGPT-4o carbs for adolescents with T1D: https://journals.sagepub.com/doi/10.1177/19322968261424270
- Nutrition5k: https://arxiv.org/pdf/2103.03375
- DPF-Nutrition (depth prediction and fusion): https://arxiv.org/pdf/2310.11702
- USDA FoodData Central: https://www.ars.usda.gov/oc/utm/taking-a-deep-dive-into-fooddata-central/
- Open Food Facts API tutorial: https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorial-off-api/
- Nutrition API comparison (vendor-authored): https://about.greenchoicenow.com/nutrition-data-api-comparison
- Undermyfork: https://www.startuphealth.com/startup-health-blog/2023/06/02/undermyfork-uses-an-instagram-like-interface-to-unlock-the-potential-of-continuous-glucose and https://diatribe.org/diabetes-technology/8-apps-improve-your-time-range
- SNAQ: https://www.dexcom.com/partnerships/digital-health-apps/snaq and review https://www.diabettech.com/snaq-a-diabetes-ai-to-help-you-carb-count-badly/

Modeling, prediction, settings
- OpenAPS Autotune and settings: https://openaps.readthedocs.io/en/dev/docs/Customize-Iterate/optimize-your-settings.html
- OpenAPS glossary (COB, UAM, predBGs): https://openaps.readthedocs.io/en/latest/docs/Resources/glossary.html
- Meal handling across loops: https://www.diabettech.com/meal-times-when-closed-looping-some-points-to-consider/
- Trio docs: https://open-iaps-docs.readthedocs.io
- Glycemic-aware forecasting on OhioT1DM: https://arxiv.org/pdf/2502.14183
- Deep what-if LSTM with meal and insulin inputs: https://pmc.ncbi.nlm.nih.gov/articles/PMC8126192
- Meal-to-meal prediction challenge (diary data barely beats baseline): https://astro.paperswithcode.com/paper/the-challenge-of-predicting-meal-to-meal
- ReplayBG: https://pubmed.ncbi.nlm.nih.gov/37368794/ and https://github.com/gcappon/replay-bg
- simglucose: https://github.com/jxx123/simglucose
- awesome-diabetes-software: https://github.com/gcappon/awesome-diabetes-software
- GluFormer: https://github.com/Guylu/GluFormer and https://web3.arxiv.org/abs/2408.11876
- MetaboNet dataset consolidation: https://arxiv.org/html/2601.11505
- AZT1D dataset: https://fugumt.com/fugumt/paper_check/2506.14789v1_enmode
- Tidepool Big Data Donation: https://cdhi.ucsf.edu/node/1486
- Personalized glycemic response (Zeevi et al. 2015): https://www.wisdom.weizmann.ac.il/~eran/zeevi_cell_2015.pdf
- Fat-protein units and the Warsaw method: https://www.mdpi.com/2072-6643/17/20/3287 and https://journals.sagepub.com/doi/pdf/10.1089/dia.2011.0083

Regulatory
- FDA Clinical Decision Support guidance (2026 reissue): https://www.fda.gov/media/109618/download and summary https://innolitics.com/articles/fda-guidance-clinical-decision-support-software-2026/
- Bolus calculator app regulation history: https://pmc.ncbi.nlm.nih.gov/articles/PMC4454096
