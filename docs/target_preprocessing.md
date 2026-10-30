# Stage 6 targets and fitted preprocessing

Stage 6 adds explicit target and normalization contracts without changing the
v1 raw-return behavior. Omitted targets remain `TargetSpec()` and produce
`raw_return-v1` labels. New configurable jobs carry `TargetSpecV2`:

```python
TargetSpecV2(kind="raw_return")
TargetSpecV2(kind="return_zscore_per_ticker", rolling_window_sessions=20)
TargetSpecV2(kind="return_zscore_cross_sectional")
```

The per-ticker transform compares the current raw return
`close(T+1) / close(T) - 1` with the population mean and variance of the
previous 20 or 60 mature returns for that ticker. The cross-sectional transform
uses the same-day raw returns for the complete frozen universe and population
moments. A zero variance produces zero transformed values. Raw labels are
always retained in `TargetValues.raw`; transformed labels are in
`TargetValues.transformed`. Cross-sectional moments and per-ticker baseline
moments are outcome metadata only and are never feature or inference inputs.

`required_session_scope` and `build_sample` include target baseline sessions as
label dependencies when labels are requested. They do not create additional
training entries. Missing or unready dependencies remain explicit target
issues; they are not filled with zero.

## Fitted normalization

`PreprocessingSpec` independently selects `none`, `standard`, or `min_max`
for inputs and outputs. `fit_preprocessing` consumes only finite eligible
training arrays. Input parameters are fitted per flattened input coordinate;
output parameters are one shared scalar across all training dates and tickers.
Population variance is used for standardization. Constant dimensions map to
zero, and min-max transforms do not clip future values. `FittedPreprocessing`
provides `transform_inputs`, `transform_outputs`, and `inverse_outputs`, plus
strict JSON-safe serialization.

When a `TrainingDataset` carries a fitted state through
`with_preprocessing`, `training_arrays` applies that state without refitting.
The state records the exact ticker order, feature order, target identity, fit
scope, and source revision. Dataset revisions and v2 checkpoint identities
include its digest, so a reordered universe or changed target cannot reuse it.

The standalone checks are:

```powershell
.venv\Scripts\python.exe -m pytest tests\targets tests\preprocessing tests\workflows\test_database_simplification_stage9.py -q
.venv\Scripts\python.exe -m mypy src\stockguidance\targets src\stockguidance\preprocessing.py src\stockguidance\datasets src\stockguidance\workflows\simplified_readiness.py src\stockguidance\config\training.py src\stockguidance\training
```

No target columns or fitted scaler state are written to the database. Artifact
metadata binding for the new target/scaler contract remains Stage 8 work.
