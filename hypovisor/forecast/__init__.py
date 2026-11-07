"""Glucose forecasting (30 and 60 minutes ahead).

Model selection was done on OhioT1DM (12 people, official train/test split); see
docs/research/02-glucose-forecasting-benchmark.md. The winner was gradient-boosted trees (LightGBM) on a
2 to 6 hour window of CGM plus insulin/carb physiology features, predicting the change from the last reading.
Deep models (LSTM, CNN-LSTM, transformer, N-BEATS, DLinear, PatchTST) were slower and no better on this data.
"""
