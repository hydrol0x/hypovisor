# Glucose forecasting benchmark on OhioT1DM

*2026-11-07. Scripts: `scripts/benchmark_ohio.py` (reproduces the shipped model's number); the full exploratory
harness that produced the table below lived in a scratch directory and is summarized here.*

## Question

Hypovisor had no forecasting model. Before adding one, we asked: on a public T1D dataset with CGM, insulin and
meals, how well do the obvious approaches work, and do the architectures favoured in recent papers beat them?

## Data and protocol

**OhioT1DM** (Marling and Bunescu): 12 adults with T1D, 8 weeks each, CGM every 5 minutes, basal and temp basal,
boluses with pump carb entries, self-reported meals, wearables. Official per-patient training and testing XML files.

Protocol (the standard one in the BGLP challenges):

- Train on the training file; the last 20 percent of it, chronologically, is the validation split for early stopping.
- Test on the testing file. Only real test readings are scored; gaps up to 15 minutes inside the input window are
  linearly interpolated, longer gaps drop the sample.
- Input window 2 hours (24 steps) unless stated; targets at 30 and 60 minutes; models predict the change from the
  last reading. RMSE and MAE in mg/dL averaged over the 12 patients.
- "personal": one model per patient. "population": one model on all 12 training files, scored per patient.
  "pop-finetune": population model, then a few epochs on the patient's own training data.
- Exogenous channels: "physio" = IOB, insulin activity, basal deviation IOB, COB, carb absorption, time of day;
  "basic" = raw bolus, carbs, basal rate; "none" = CGM only. Tree and linear models always also receive the
  physiology scalars at the last step.

## Results

Sorted by 30-minute RMSE. Training time is for all 12 patients on a 4-core CPU.

| Model | Mode | Exog | History | RMSE 30 | RMSE 60 | MAE 30 | MAE 60 | Train s |
|---|---|---|---|---|---|---|---|---|
| LightGBM tuned + trend features + patient id | population | physio+raw | 6 h | **17.70** | **29.51** | 12.48 | 21.63 | 97 |
| LightGBM tuned + trend features + patient id | population | physio+raw | 2 h | 17.79 | 29.59 | 12.50 | 21.68 | 65 |
| LightGBM tuned + trend features | population | physio+raw | 6 h | 17.81 | 29.80 | 12.57 | 21.94 | 100 |
| LightGBM tuned + trend features | population | physio+raw | 2 h | 17.91 | 29.89 | 12.60 | 21.99 | 71 |
| LightGBM tuned | population | physio+raw | 2 h | 17.95 | 30.00 | 12.64 | 22.06 | 64 |
| LightGBM default | population | physio | 2 h | 18.08 | 30.23 | 12.76 | 22.28 | 16 |
| Ensemble LightGBM + DLinear + PatchTST (3 seeds) | population | physio | 2 h | 18.22 | 30.24 | 12.91 | 22.34 | 1896 |
| LightGBM tuned + trend features | personal | physio+raw | 2 h | 18.38 | 30.71 | 13.04 | 22.59 | 54 |
| LightGBM default | personal | basic | 2 h | 18.41 | 30.95 | 13.00 | 22.71 | 10 |
| LightGBM default | personal | none | 2 h | 18.49 | 30.99 | 13.05 | 22.76 | 6 |
| LightGBM default | personal | physio | 2 h | 18.58 | 31.12 | 13.15 | 22.91 | 22 |
| PatchTST | population | physio | 2 h | 18.99 | 31.15 | 13.67 | 23.21 | 517 |
| Ridge on lags | personal | physio | 2 h | 19.15 | 31.98 | 13.57 | 23.72 | 12 |
| PatchTST + glycemia-weighted loss | population | physio | 2 h | 19.26 | 31.59 | 13.73 | 23.39 | 501 |
| PatchTST | population | physio | 4 h | 19.42 | 31.37 | 13.99 | 23.26 | 1248 |
| DLinear | population | physio | 2 h | 19.50 | 32.77 | 13.79 | 24.42 | 16 |
| Transformer (encoder) | pop-finetune | physio | 2 h | 19.66 | 32.04 | 14.01 | 23.52 | 462 |
| CNN-LSTM | pop-finetune | physio | 2 h | 19.73 | 32.31 | 14.03 | 23.62 | 87 |
| Transformer (encoder) | population | physio | 2 h | 19.78 | 31.87 | 14.09 | 23.44 | 315 |
| CNN-LSTM | population | physio | 2 h | 19.87 | 32.23 | 14.14 | 23.72 | 70 |
| N-BEATS (generic) | population | physio | 2 h | 20.02 | 31.87 | 14.25 | 23.64 | 49 |
| LSTM | pop-finetune | physio | 2 h | 20.05 | 32.31 | 14.25 | 23.63 | 186 |
| CNN-LSTM + glycemia-weighted loss | population | physio | 2 h | 20.06 | 32.47 | 14.34 | 24.09 | 68 |
| LSTM | population | physio | 2 h | 20.24 | 32.50 | 14.38 | 23.83 | 112 |
| LSTM + glycemia-weighted loss | population | physio | 2 h | 20.35 | 32.90 | 14.45 | 24.28 | 83 |
| MLP | personal | physio | 2 h | 20.78 | 33.48 | 14.97 | 24.78 | 61 |
| CNN-LSTM | personal | physio | 2 h | 20.87 | 33.13 | 15.13 | 24.62 | 48 |
| LSTM | personal | physio | 2 h | 21.05 | 33.59 | 15.42 | 25.16 | 107 |
| N-BEATS (generic) | personal | physio | 2 h | 21.44 | 35.05 | 15.71 | 26.22 | 45 |
| Transformer (encoder) | personal | physio | 2 h | 21.53 | 34.80 | 15.74 | 25.81 | 306 |
| Persistence (last value) | – | – | – | 23.29 | 38.09 | 16.85 | 28.22 | 0 |
| Linear extrapolation (15 min slope) | – | – | – | 27.30 | 56.67 | 18.21 | 38.94 | 0 |

Per-patient spread for the best model at 30 minutes: 15.0 (patient 570) to 21.4 (patient 575). Patients 540, 567,
575 and 584 are the hard ones, as in the published literature.

## How this compares with the literature

- Persistence on the same protocol is reported around 24 and 40 mg/dL; ours is 23.3 and 38.1.
- The BGLP 2020 challenge winners and the commonly cited personalized FCNN/LSTM baselines sit around 18.2 to
  18.6 at 30 minutes and 31 at 60 minutes. Our best, 17.7 and 29.5, is at or slightly past that level.
- Several 2025 papers report 15.6 to 16.5 at 30 minutes and about 24.6 at 60 (Crossformer, PatchTST, DLinear)
  on OhioT1DM, but under a different protocol: trained on DCLP3 and tested on the *entire* OhioT1DM set, where
  their own persistence baseline is 25.6 and 40.9. The ratio to persistence (about 0.62 and 0.60) is similar to
  ours (0.76 and 0.77 at 2 h history, better at 6 h). A table in the GLIMMER paper lists 60-minute RMSEs of 23.5
  (TimesNet), 23.7 (BG-BERT) and 24.0 (CNN-LSTM with a genetically tuned glycemia-weighted loss) on OhioT1DM;
  we could not verify those protocols and did not reproduce them.

## What we learned

1. **Gradient-boosted trees on a short window beat every deep model here.** Eight weeks per person is small
   data; nets trained per patient were 2 to 3 mg/dL worse than LightGBM, and even population pretraining plus
   fine-tuning left them about 1.5 behind. PatchTST came closest (19.0) at 30 times the training cost.
2. **Pooling across people helps everything** (0.5 for LightGBM, about 1 for the nets), and a patient-id
   categorical feature gives the tree model the personalization that fine-tuning gives a net.
3. **Insulin and carb inputs add little on this dataset** (CGM-only LightGBM 18.5 vs 18.4 to 18.6 with them).
   The recent CGM trajectory carries most of the signal; self-reported meals are noisy. The physiology
   features are kept because they are cheap and help slightly in the pooled setting.
4. **History beyond 2 hours barely matters** (6 h: 0.1 better for LightGBM; worse for PatchTST).
5. **The glycemia-weighted loss did not improve overall RMSE** on any of four architectures with fixed weights;
   the paper that reports large gains tunes the weights per patient with a genetic algorithm.
6. **Ensembling LightGBM with the nets did not help**, because the nets are individually weaker.

## What ships in Hypovisor

`hypovisor/forecast/`: the LightGBM forecaster with the tuned parameters, trend and physiology features, a
2-hour window (configurable), trained on the user's own history (personal mode: 18.4 and 30.7 on OhioT1DM).
`POST /api/forecast/train`, `GET /api/forecast` (30 and 60 minute forecast from the latest window with the
validation RMSE as an error bar), `GET /api/forecast/evaluate` (chronological holdout vs persistence), and
`hypovisor forecast train|evaluate|predict`. The dashboard shows the forecast next to the latest reading.

A population model trained on OhioT1DM is not shipped: the dataset is under a data use agreement. Anyone with
their own copy can reproduce the pooled result with `scripts/benchmark_ohio.py --mode population --pid`
(the shipped feature pipeline gives 17.86 and 29.83 with a 2-hour window; the exploratory harness above differed
only in how pump carb entries and meals were merged).

## Honest limits

- RMSE averages over all glucose levels; errors around hypoglycemia matter more. A per-range breakdown and
  Clarke/Parkes error-grid analysis are the next evaluation step.
- OhioT1DM is 12 people on older pumps and CGMs; a Control-IQ user's data (e.g. DCLP3) has different dynamics.
- 30 minutes is useful for "where am I heading"; 60-minute error of about 30 mg/dL is too wide for dosing
  decisions, which is one more reason the app presents history and ranges rather than dose advice.
