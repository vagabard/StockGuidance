# Training, recommendation, and simulation implementation contract

Status: Stage 0 accepted contract, 2026-10-28. This document freezes the
interfaces and semantics for the staged implementation described in
training_recommendation_simulation_prompts.md. It does not change schemas,
train a model, refresh data, create runs, or execute a database operation.

## 1. Accepted product decisions

The owner accepted these decisions during Stage 0:

| Area | Accepted decision |
| --- | --- |
| Training universe | Require complete raw observations across training entry dates and all required feature/label dependencies. Do not require future survival through simulation. Freeze the ordered universe before simulation. |
| Return targets | Offer raw close-to-close return, per-ticker rolling return z-score, and same-day cross-sectional return z-score as distinct target versions. |
| Per-ticker target z-score | Use population moments over the previous 20 or 60 mature close-to-close returns for that ticker; default window is 20. The current future return is scored against that prior baseline, not against a z-score of z-scores. |
| Cross-sectional target z-score | On each target date, compare next-session returns of the complete frozen universe using population mean and variance. Tied returns produce zero transformed targets when variance is zero. This is an explicit alternative, not the default. |
| Existing stored z-scores | Existing per-ticker rolling price and price-delta z-score materializations remain separate input-feature options. They are not silently substituted for return-target transforms. |
| Relative dates | Calendar-year and calendar-day expressions use a typed anchor. A signed duration from an explicit date is positive-forward or negative-ending-at-anchor. N exchange sessions means exactly N sessions including the snapped anchor. |
| Ago anchor | Relative ago expressions default to the last completed available session in the selected source snapshot. Explicit today is supported. |
| Normalization | Inputs and outputs independently support none, training-set population standardization, and training-set min-max scaling. Default is none. Input state is per flattened coordinate; output state is one shared scalar transform. |
| Universe filters | Average-volume and volatility filters apply only during training-universe selection. Their result is frozen with the model and is not checked again at recommendation time. The legacy minimum-transaction rule remains separately named. |
| Recommendation medians | Report the median over all eligible scores and the median over returned top-n scores. Empty valid selections have empty results and null medians. |
| Recompute operations | rescore reuses a saved artifact without retraining and creates a child run for revised data or an explicitly changed compatible use. rerun is a fresh run using saved parameters, with artifact reuse versus retraining explicitly selected. Neither is called replay. |
| Replay | replay is reserved for exact offline portfolio/accounting replay from saved scores, eligibility, execution quotes, marks, and fees. It does not retrain or contact a provider/database. |
| Defaults | Recommendation top-n is 5; simulation top-n is 1; simulation horizon is 10 exchange sessions; initial capital is 1000.00; allocation is equal across selected stocks; fractional shares and existing fee, rounding, missing-price, delayed-exit, and end-period rules remain. |
| Units | Date-expression days are calendar days, years are calendar years, and lookbacks/horizons are exchange sessions. Raw returns remain fractions (0.01 means 1%); percentages are presentation-only. |

Accepted target identities:

~~~text
raw_return-v1
return_zscore_per_ticker-v2(window_sessions=20|60)
return_zscore_cross_sectional-v2(population_same_day_frozen_universe)
~~~

The per-ticker target uses mature return history immediately before the target
return. The cross-sectional target uses only the target date's frozen universe.
Neither transformed target can be presented as a raw future return without the
required baseline statistics.

## 2. Version and compatibility policy

Existing explicit jobs retain their current v1 meanings and remain separate from
new configurable jobs:

| Contract | Existing v1 | New configurable contract |
| --- | --- | --- |
| Features | previous-close-v1, entry-open-v1; stateless relative/log closes and optional entry opening | configured-features-v2; ordered channels, rolling windows, raw volume, and fitted preprocessing identity |
| Target | Raw close(T+1)/close(T)-1, no implicit transform | targets-v2; raw return or one explicit return-z-score transform |
| Training objective | L1/MAE | loss-v2: MAE, RMSE, or bounded within-session pairwise ranking |
| Model | stockguidance-models-v1; shared hidden activation and unrestricted final output | stockguidance-models-v2; per-hidden-layer/shared activation shorthand and selectable final activation where architecture permits |
| Artifact | stockguidance-inference-1 | Later versioned metadata binds target, scaler, model/loss, universe, and layout identities; v1 artifacts are never relabeled |
| Reports | stockguidance-training-1, stockguidance-recommendation-1, stockguidance-simulation-1 | Later report versions may add saved-run references; current reports remain loadable |
| Runs | No shared saved-run API | Stage 1 introduces stockguidance-run-1 manifests and atomic run directories |

Dimensions alone never establish compatibility. Feature order, ticker order,
target version/window, scaler state, source/calendar identity, model version,
loss, availability policy, and data revision are all compatibility inputs.
Stable database IDs are identities, not vector offsets, descriptors, embeddings,
or presentation orders. Current models continue to require their saved ticker
order.

## 3. Typed configuration contracts

These are Stage 0 public field names. Later stages may split modules, but must
preserve their meanings.

### 3.1 Source, dates, and resolved sessions

~~~python
DateExpression = (
    AbsoluteDate(date)
    | RelativeDate(
        anchor="first_data" | "last_data" |
               "last_completed_available" | "today",
        offset_value=int,
        offset_unit="calendar_days" | "calendar_years",
    )
)

DateRange = ExplicitRange(start=DateExpression, end=DateExpression)
         | AnchorDuration(
             anchor=DateExpression,
             duration_value=int,
             duration_unit="calendar_days" |
                           "calendar_years" |
                           "exchange_sessions",
         )
~~~

duration_value cannot be zero. Explicit ranges are inclusive. A non-session
start snaps forward and a non-session end snaps backward. Empty, reversed,
out-of-coverage, and unknown/incomplete-source ranges fail. Calendar-year
arithmetic clamps February 29 to February 28. Resolution injects a clock,
source coverage snapshot, source identity, and exchange calendar, and occurs
once per run.

The resolved record persists original typed expressions, source snapshot and
revision, anchor evidence, inclusive boundaries, effective exchange sessions,
calendar identity/version, training/label/simulation cutoffs, and all warm-up,
model-lookback, target-baseline, and horizon dependencies. first_data and
last_data mean verified source coverage, never arbitrary database IDs or the
wall clock. An ago expression uses last_completed_available unless its anchor
explicitly says today.

### 3.2 Frozen universe and identity mapping

~~~python
UniverseSpec(
    membership_period=DateRange,
    ordered_ticker_ids=tuple[int, ...] | None,
    ordered_ticker_labels=tuple[str, ...] | None,
    selection_mode="stable_id" | "explicit_subset",
    filters=UniverseFilters(...),
)
~~~

Automatic selection uses complete required raw coverage across the approved
training membership period plus dependencies. It distinguishes verified absence
from unknown/incomplete coverage, records an exclusion reason per ticker, and
orders selected stable IDs deterministically. An explicit ordered subset is
allowed only after validating complete coverage and identity. Symbol labels are
display labels; IDs remain stable identities.

New filters are independently optional:

~~~text
average_volume: {window_sessions: 20, lower: number|null, upper: number|null}
volatility:    {window_sessions: 20, lower: number|null, upper: number|null}
legacy_transactions: {minimum: integer}
~~~

Enabled filters require a window and at least one bound. Volume means arithmetic
mean shares per exchange session. Volatility means population standard deviation
of fractional close-to-close returns and is not annualized. Disabled filters do
not authorize reads of their optional dependencies.

### 3.3 Ordered feature channels and axes

~~~python
FeatureSpecV2(
    ticker_ids=tuple[int, ...],
    lookback_sessions=int,
    channels=(
        "relative_close" | "log_close" |
        PriceZScore(window=20 | 60) |
        PriceDeltaZScore(window=20 | 60) |
        "raw_volume" | VolumeZScore(window=20 | 60) |
        "entry_open_return",
    ),
    signal_mode="previous_close" | "entry_open",
    layout="session_oldest_first,ticker,feature:C",
    missing_history="unavailable",
    adjustment=...,
)
~~~

The numerical shape is (time, ticker, feature), flattened only by the existing
model adapter in C order. A price z-score window W with L model steps requires
L+W-1 closes. A price-delta z-score adds its predecessor dependency. Rolling
window length and model lookback are never conflated. Existing v1 close and
opening channels keep their original versions and timing.

### 3.4 Targets and preprocessing

~~~python
TargetSpecV2(
    kind="raw_return" |
         "return_zscore_per_ticker" |
         "return_zscore_cross_sectional",
    horizon="next_exchange_session",
    entry_price="close",
    exit_price="close",
    return_formula="close(T+1) / close(T) - 1",
    rolling_window_sessions=20 | 60 | None,
)

PreprocessingSpec(
    input_method="none" | "standard" | "min_max",
    output_method="none" | "standard" | "min_max",
    input_axis="flattened_input_coordinate",
    output_axis="one_shared_scalar",
    fit_scope="eligible_training_samples_only",
    version="preprocessing-v2",
)
~~~

rolling_window_sessions is required for the per-ticker target and forbidden
for raw/cross-sectional targets. Population variance is used. A constant fitted
dimension maps to zero; min-max does not clip future values. Output inversion
happens before ranking, but cross-sectional scores remain z-scores and are never
renamed as raw returns. Fitted state includes methods, axes, parameters,
constant-dimension behavior, exact feature order, ticker order, target identity,
and data revision.

### 3.5 Model and loss

~~~python
ModelSpecV2(
    architecture="mlp" | "conv_trans",
    hidden_widths=tuple[int, ...],
    hidden_activations=tuple[Activation, ...] | None,
    shared_hidden_activation=Activation | None,
    final_activation="none" | Activation,
    ...existing architecture fields...
)

LossSpec(
    objective="mae" | "rmse" | "pairwise_ranking",
    version="mae-v1" | "loss-v2",
    pair_block_size=int | None,
)
~~~

Input/output widths derive from the frozen sample specification. Unsupported
architecture/activation combinations fail clearly. RMSE reports square-root
units, not MSE. Ranking uses the mean
softplus(-sign(y_i-y_j)*(s_i-s_j)) over non-tied unordered pairs within each
date, after output-scale inversion. Pairless dates are skipped; an entirely
pairless training set fails. Pair evaluation is block-bounded and keeps dense
reference weighting. Objective loss is reported separately from MAE, RMSE, pair
count, and ordering accuracy.

## 4. Recommendations, simulation, and saved operations

Recommendation inference uses the artifact-owned universe, channels, target,
preprocessing, and lookback on the latest completed feature sessions. It returns
symbols, stable IDs, scores, score units, requested top-n, eligible count,
eligible-universe median, returned median, ordered full scores, eligibility, and
exclusions. Ties break by stable ID. Validate-only refresh is read-only and does
not contact the provider.

Historical simulation trains once before evaluation by default, then evaluates
the next 10 exchange sessions with the same frozen artifact/scores.
Chronological retraining remains explicit. alternate_sessions and
staggered_halves retain the current Decimal ledger, independent cash pools,
fractional shares, delayed proceeds, missing-entry/exit behavior, stale marks,
and no-forced-liquidation boundary. Simulation top-n, capital, horizon, and
recommendation top-n are independent.

Saved operations mean:

~~~text
inspect  read a manifest/report; no database, network, Torch, or training
rescore  reuse an artifact, score again, and create a child run
rerun    start a fresh run with saved parameters; explicitly reuse or retrain
replay   reproduce saved portfolio accounting offline from bounded evidence
~~~

An exact replay is allowed only when saved scores, decision eligibility, quotes,
marks, fees, and revisions cover the requested selection and schedule. Changed
top-n or schedule requires quote coverage for every potentially selected ticker.
A parent run is immutable; each rescore/rerun/replay result is a new child with
changed settings and data revisions recorded.

## 5. Run manifest identity

Stage 1 will implement a configurable runs root, defaulting to
<project>/runs/<run_id>/, with collision-free creation and atomic status
transitions. A run manifest is sanitized and versioned. It contains no
credential contents and no unrelated dirty-tree files.

Required identity sections are:

~~~text
run_id, parent_run_id, operation, status, timestamps
original sanitized configuration and separately resolved configuration
source identity, source snapshot, calendar identity/version, data revisions
date expressions, anchor evidence, cutoffs, effective sessions, dependencies
ordered stable ticker IDs and symbol labels, universe selection evidence
feature, target, preprocessing, model, loss, and execution specifications
seed, software/package/framework versions, Git revision, dirty-tree flag
artifact/checkpoint references with relative paths, hashes, and external refs
full predictions, eligibility, exclusions, metrics, reports, trades, equity
failure details and incomplete-but-usable simulation state where applicable
~~~

Relative paths must remain inside the run directory. External references are
explicit and hash-bound. Artifacts/checkpoints are separate from the manifest.

## 6. Standalone database preparation and SQL boundary

The active simplified database contract remains authoritative: selected registry
names, target schema/version guards, source singleton, completion/readiness
states, correction dependency closure, pending scope, writer lock, and resumable
operation lifecycle. Reading, training, recommendation, replay, and viewing do
not initialize or migrate schemas.

Stage 3 preparation operations expose independently executable steps:

~~~text
inspect/plan -> optional explicit schema setup -> bounded select/export
-> pure code calculation or complete SQL calculation -> review/apply
-> validate -> status/resume
~~~

Each operation records source/configuration and dependency revisions, bounded date
and ticker scope, batch size, selected registry names, SQL parameters, result
columns/types/order, intermediate result identity, completion state, and failure
state. SQL rendering is read-only and emits validated identifiers, editable
non-secret variables, ordered statements, transaction/DDL requirements, and
verification SELECTs. Runtime application continues to use parameterized values.

If calculation is in Python/NumPy, exported SQL is explicitly only the bounded
input select plus result-import/upsert contract. A SQL implementation must prove
formula/key/NULL/status/precision/timing parity with the reference calculation.
Writes are stable-key/upsert based, bounded, checkpointed, dependency-aware, and
resumable. DDL auto-commit is documented separately from transactional data work.
No production database is used by staged acceptance.

The existing contract establishes the current table/registry boundary: target
roles are source, schema, sessions, tickers, prices, deltas, data_states,
pending_updates, repair_metadata, zscore_series, and zscore_sessions, with
registry-selected z-score table templates. Existing price/delta z-scores are
reused. Volume z-scores require an additive versioned preparation decision in
Stage 3/5; targets and run-specific scalers remain code/artifact state, not
global database columns.

## 7. Implementation and gap map

| Requested capability | Current reuse | Stage 0 disposition | Missing work |
| --- | --- | --- | --- |
| Typed date expressions/dependencies | calendar/*, ExchangeCalendar, DecisionTiming, simplified_readiness.required_session_scope | Add a versioned range resolver | Expressions, coverage snapshot, anchor evidence, preview, exact dependency boundaries |
| Frozen continuous universe | FeatureSpec.ticker_ids, simplified ticker/raw repositories | Add a typed selector | Interior coverage, unknown-vs-absent evidence, filters, explanations, frozen symbols |
| Ordered feature channels | features.specification, datasets.building, z-score kernels/storage | Preserve v1; add v2 adapter | Stored/derived channels, volume preparation, readiness, exact dependency arithmetic |
| Targets | targets.specification, targets.construction, raw close labels | Preserve raw v1; add explicit v2 kinds | Mature return z-scores, raw-label retention, target identities/readiness |
| Fitted preprocessing | None; samples are stateless | Add pure fit/transform/inverse service | Training-only fit, serialization, compatibility, leakage tests |
| Model controls | models.specification, models.factory, MLP/conv-transformer | Preserve v1 factory; add v2 | Per-layer/shared activations, final activation, architecture validation |
| Losses/metrics | training.engine L1/MAE | Preserve v1 MAE | RMSE, ranking, safe gradients, block parity, honest metric scopes |
| Training | training.service, workflows.train_model, simplified_training | Reuse typed workflow | Target/scaler propagation, v2 artifact/checkpoint binding |
| Artifacts | artifacts/*, strict inference-1 codec | Keep v1 loadability | Versioned universe/target/scaler/loss/layout metadata |
| Recommendations | recommendations/*, HistoricalSignals, ranking | Preserve v1 behavior | Artifact-owned preprocessing, validate-only refresh, symbols/units/medians, saved failures |
| Simulation | simulation/*, portfolio/*, both schedules | Reuse accounting | Fit-once orchestration, independent controls, persisted evidence, rescore/rerun/replay |
| Saved runs | No shared run service; reports require explicit files | New Stage 1 foundation | Collision-free dirs, manifests, status/failure/parent-child, offline APIs |
| Reports/viewers | reporting/*, viewers/reports.py | Extend after saved runs | Runs-root inspection, diffs, score/median/equity/drawdown views |
| Database preparation/SQL | Simplified schema, planner, recovery, migration SQL | Reuse active lifecycle | Standalone bounded API, SQL rendering/export, parity, status/resume runbook |

Current implementation facts inspected for this map:

- FeatureSpec is v1 close/log/entry-open only; TargetSpec is raw return only.
- training.engine.fit_epoch uses L1/MAE; ModelSpec rejects every final
  activation except none.
- simplified_training still instantiates TargetSpec() instead of carrying a
  requested target through the target-schema workflow.
- Eligibility is previous-session transaction count, not average share volume or
  volatility.
- Artifacts are strict stockguidance-inference-1; reports are explicit-file v1
  documents; viewers do not inspect a runs root.
- Simplified readiness and database runtime already enforce target schema,
  completion, pending-operation, source, and revision evidence without silently
  migrating schemas.

## 8. End-to-end identity flow

~~~text
sanitized job config
    -> pinned source snapshot + calendar
    -> resolved date expressions and dependency sessions
    -> frozen ordered ticker IDs + symbol labels + exclusions
    -> FeatureSpecV2 / TargetSpecV2 / PreprocessingSpec
    -> prepared/readiness revision evidence
    -> eligible training samples and fitted scaler state
    -> model/loss training and standalone artifact
    -> full ordered predictions + decision eligibility
    -> recommendation or simulation selection
    -> quotes/fills/marks/equity and report
    -> immutable parent run plus optional rescore/rerun/replay child
~~~

The same artifact-owned feature order, ticker order, target units, scaler state,
calendar, source identity, and availability policy flow through training,
inference, recommendation, and simulation. Raw returns are retained for
portfolio accounting even when the model target is a z-score. A prediction row
always stores its tensor position and stable ticker ID mapping. No future
descriptor vector or variable-order model is introduced.

## 9. Sanitized v2 job examples

These are contract examples for later parser stages, not currently accepted CLI
files. Paths are placeholders; no credential contents are embedded.

### Training

~~~json
{
  "contract_version": "training-job-v2",
  "source": {"provider": "fixture", "market": "us-stocks", "universe": "fixture", "interval": "1d", "adjustment": "adjusted"},
  "database_config": "fixture-dbinfo.id",
  "date_range": {"start": {"anchor": "first_data", "offset": {"value": 3, "unit": "calendar_years"}}, "end": {"anchor": "last_data", "offset": {"value": -10, "unit": "calendar_years"}}},
  "membership": {"mode": "complete_dependencies", "filters": {"average_volume": null, "volatility": null}},
  "features": {"lookback_sessions": 60, "channels": ["relative_close", {"family": "price_zscore", "window_sessions": 20}, {"family": "price_delta_zscore", "window_sessions": 60}, "raw_volume"]},
  "target": {"kind": "return_zscore_per_ticker", "window_sessions": 20},
  "preprocessing": {"input": "standard", "output": "none"},
  "model": {"architecture": "mlp", "hidden_widths": [64, 64], "shared_hidden_activation": "gelu", "final_activation": "none"},
  "loss": {"objective": "rmse"},
  "training": {"epochs": 3, "batch_size": 16, "device": "cpu", "seed": 17},
  "runs_root": "runs"
}
~~~

### Recommendation

~~~json
{
  "contract_version": "recommendation-job-v2",
  "artifact": "../runs/train-001/artifacts/model.sgmodel",
  "data_mode": "validate_only",
  "date_range": {"anchor": {"anchor": "last_completed_available"}, "duration": {"value": -1, "unit": "exchange_sessions"}},
  "top_n": 5,
  "filter_policy": "artifact_frozen",
  "runs_root": "runs"
}
~~~

### Simulation

~~~json
{
  "contract_version": "simulation-job-v2",
  "artifact": "../runs/train-001/artifacts/model.sgmodel",
  "fit_policy": "fit_once_before_evaluation",
  "allocation_policy": "alternate_sessions",
  "simulation_top_n": 1,
  "horizon_sessions": 10,
  "initial_capital": "1000.00",
  "recompute": "reuse_artifact",
  "runs_root": "runs"
}
~~~

## 10. Explicitly deferred work

Out of scope until a future contract: stock descriptor vectors, embeddings,
vocabulary/unknown-stock policy, variable ticker ordering or universe size,
attention masks, permutation-invariant/equivariant models, new transformer
paths, live database migration, live data downloads, trade submission,
credential externalization, and automatic production retraining.

Stage 0 is documentation and contract establishment only. Stage 1 implements the
saved-run foundation. Stage 2 implements range resolution. Stage 3 implements
standalone preparation/SQL operations. Later feature, target, model, training,
recommendation, simulation, and viewer work must follow this contract in order.

