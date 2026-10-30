# Configurable training, recommendations, and simulation: implementation prompts

Prepared against `b0917bf` on 2026-10-28. This file is the implementation brief;
creating it does not implement the feature or change the database.

Run the numbered prompts in order, one stage per conversation. Include the
shared preamble, the behavior contract, and the selected stage. Each stage must
leave working code, appropriate verification, and a handoff for the next stage.
Do not treat the existence of a prompt as evidence that its stage is finished.

## Decisions to confirm with the owner

The owner requested consultation. The following defaults have been proposed,
but are not confirmed choices. Resolve unanswered items in Stage 0 before
implementing behavior that depends on them; record answers in the contract and
remove superseded defaults. Independent foundation work may proceed.

| Question | Proposed default |
| --- | --- |
| Which dates determine continuous ticker membership? | Training dates and their required feature/label dependencies; freeze the universe before simulation. Do not require future survival. |
| What does a z-score target compare? | Same-day next-session returns across the fixed ticker universe. A per-ticker rolling return z-score is a different option, to add if requested. |
| What anchors “ago”? | Last completed, available data session in the selected source snapshot; support an explicit `today` anchor too. |
| What additional normalization methods are needed? | Independently selectable `none`, training-set standardization, and training-set min-max scaling for inputs and outputs. |
| When do optional volume/volatility filters apply? | At training-universe selection only; freeze that membership with the model. Ask whether they must also gate each recommendation. |
| Which median and what does rerun mean? | Report both eligible-universe and top-n medians; support saved-score replay and recomputation from saved settings as separate operations. |

Additional proposed defaults: simulation top-n is 1; recommendations top-n is 5;
simulation length is 10 exchange sessions; initial capital is `1000.00`; equal
allocation across selected stocks; retain fractional shares and existing fee,
rounding, missing-price, and end-of-period behavior. Date-expression days are
calendar days, years are calendar years, while lookbacks and simulation lengths
are exchange sessions. Raw model returns remain fractions (`0.01` = 1%), with
percentages used only in explicitly labeled presentation. Confirm any owner
objection rather than silently changing these units.

## Repository findings and design direction

- Current implementation is under `src/stockguidance`. Reuse the shared
  dataset builder, typed training/inference APIs, portfolio planner, simulator,
  artifact storage, and reporting. Keep command parsing outside domain code.
- `features/specification.py` currently supports relative/log closes and an
  optional entry-opening channel. Stored rolling z-scores are not model inputs.
  `zscores` already computes population price and price-delta z-scores for
  windows such as 20 and 60. Price delta means `close(s)/close(s-1)-1`.
- `targets/specification.py` currently accepts only raw next-session
  close-to-close returns. `training/engine.py` currently trains with L1/MAE,
  not RMS. `models/specification.py` has configurable widths and one shared
  hidden activation, but requires an unrestricted final activation.
- Current eligibility uses previous-session transaction counts, default 200;
  this is different from average daily share volume and volatility.
- Both requested schedules already exist: `alternate_sessions` and
  `staggered_halves`. The simulator already preserves independent cash pools,
  calendar cadence, historical cutoffs, and missing-price accounting.
- Current report viewers accept explicit report files. Extend them to inspect
  saved runs rather than restoring the retired training-controller design.
- Existing transformer components still use the configured fixed input/output
  layout. Their presence does not establish support for stock identity vectors,
  variable ticker ordering, or a variable number of stocks.
- The pre-2026-10-10 implementation is available at `20d9895` (2026-03-04).
  Relevant paths include `simulate_trading.py`, `StockRecommenderFromModel.py`,
  `trainer/loss_functions/`, `trainer/label/transform/`,
  `trainer/file_structure_single_run_handler.py`, and `viewers/`.
  Use `git show 20d9895:<path>` to inspect behavior; do not check out old files
  over the current working tree or import retired modules into the package.
- The versioned simplified database is the active design. Respect its selected
  table-name registry, completion/readiness states, correction dependencies,
  pending-operation coordination, and writer locks. Older docs sometimes
  describe superseded storage behavior; verify against current code.

## Shared preamble — include with every implementation prompt

```text
Work in C:\Projects\StockGuidance. Read applicable AGENTS.md instructions,
docs/training_recommendation_simulation_prompts.md, and the completed handoffs
in docs/training_recommendation_simulation_results.md if that file exists.
Implement only the selected stage and its necessary tests/documentation.
Inspect prerequisites in code. If a prerequisite is incomplete, report it
precisely instead of pretending the stage succeeded. Do not commit.

Preserve existing user changes. At authoring time these include dbinfo.id,
src/stockguidance/database/migration_sql.py,
tests/database/test_migration_sql.py, and training-job.json. Recheck git status;
do not reset, overwrite, or incorporate unrelated edits.

Use the configured project interpreter and existing toolchain. Discover them
from the current environment/IDE configuration; do not copy stale interpreter
workarounds blindly. Keep imports and --help free of application side effects.
Keep domain workflows independent of CLIs and viewers. Extend existing shared
contracts instead of building another training or simulation implementation.
Follow the future-model guidance below only through small changes within the
selected stage. Defer substantial independent restructuring and future features.

User requirements in this brief supersede conflicting exclusions in older
prompt documents: normalized features, new targets/losses, and configurable
final activations are now requested work. Preserve existing v1 semantics under
their original versions; introduce explicit new versions for changed semantics.

Database setup, population/backfill, selection, calculation, and persistence
must be independently executable and debug-friendly. Follow the database
contract below. Never create/migrate schema as a side effect of reading,
training, recommending, replaying, or viewing. Explicit preparation may invoke
named preparation steps, but each step must also work on its own.

This implementation prompt authorizes code and disposable-fixture work, not a
live database migration, live data refresh, or real trading. Produce standalone
operator commands and SQL for later use. Do not print credentials or embed them
in saved configs, SQL exports, reports, exceptions, or handoffs.

Run focused meaningful tests, affected type checks, and git diff --check. Use
MySQL fixtures for MySQL behavior; do not claim SQLite proves it. Record actual
results and unavailable checks. Do not require training-quality improvements
from a tiny random fixture as proof of implementation correctness.

Append a stage handoff to docs/training_recommendation_simulation_results.md:
status, changed interfaces, accepted decisions, commands/results, remaining
limitations, and next-stage prerequisites. Give exact rerunnable commands for
new preparation/debugging tools. Implement the selected stage, not just a plan.
```

## Behavior contract — applies to all stages

### Dates, samples, and ticker identity

- Accept either two date expressions or one anchor plus a signed duration.
  Expressions support absolute ISO dates and offsets from first data, last
  data, or today. Cover: first data +3 years to last data -10 years; 5 years
  ago to 2 years ago; first data +10 years followed by +5 years; and an end
  date with a negative duration. Keep these as typed structured settings;
  natural-language parsing is not required.
- Resolve once per run using an explicit source snapshot and exchange calendar.
  First/last data anchors use verified source coverage, not arbitrary database
  IDs or the wall clock. Save the original expressions, anchor evidence,
  resolved calendar dates, and effective exchange sessions.
- Date boundaries are inclusive. Snap a non-session start forward and end
  backward; reject an empty/reversed/out-of-coverage interval. Calendar-year
  offsets clamp February 29 to February 28 when necessary. A positive calendar
  duration extends forward from its anchor; a negative one makes the anchor
  the end. For explicit session-count durations, N means exactly N sessions,
  including the anchor after its appropriate start/end snapping. Reject zero.
- Select tickers with complete required raw observations across the approved
  membership period, not merely observations on its first and last dates.
  Distinguish verified absence from unknown/incomplete source coverage.
  Required warm-up and mature-label dependencies extend outside the selected
  training entry dates without becoming extra training samples.
- Freeze an ordered list of stable ticker IDs and corresponding symbol labels
  in the artifact/run. Preserve the current oldest-session, ticker, feature
  layout and one output per ticker in exactly that order. IDs are not vector
  offsets. Missing inputs must not shrink or reorder the network.
- Preserve existing availability policies. Training labels and all fitted
  statistics use only the fitting period and cutoff. Holdout/simulation data
  never determines scalers, model selection, or the frozen universe by default.
  Retrospective research remains explicit and distinguishable from strict
  point-in-time evidence; production recommendations retain strict semantics.
- For purchase at close T, ordinary features end at T-1's close. Labels are
  `close(T+1)/close(T)-1`. Do not include T's close in a decision that fills
  at that same close. Existing entry-open behavior remains a separate channel.

### Future stock identity vectors and sequence models

- Future development may supply a stock descriptor vector alongside each
  stock's recent history, present stocks in different orders, and use a
  transformer as the model or one of its components. This direction does not
  expand the current implementation scope.
- Distinguish stable stock identity, presentation order, and tensor position.
  Preserve the mapping from feature rows, target columns, and predicted scores
  to stable ticker IDs. A database ID is an identity, not a numerical stock
  descriptor or an embedding coordinate.
- Preserve explicit time, ticker, and feature dimensions and their ordering in
  sample specifications. Keep flattening/reshaping within existing array/model
  adapters so structured inputs can be reconstructed from the saved layout.
  Keep sample construction and fitted preprocessing independent of model
  architecture; no new generic adapter framework is required.
- Treat future stock descriptors as a separate input from recent time-series
  features. Defer descriptor contents, encoding, storage, fitting rules, and
  unknown-stock policy until that feature is requested.
- Current models/artifacts still require their saved ticker order. Preserve
  rejection of reordered inputs; a transformer layer alone does not make a
  model order-independent. Future variable-order support requires its own
  versioned input/model contract and explicit output-to-stock mapping.
- Do not add descriptor tables, embeddings, variable-size universes, attention
  masks, permutation-invariant/equivariant models, or new transformer training
  paths now. Any substantial independent changes wait for that future feature.

### Features, targets, filters, and models

- Let the user select ordered feature channels and a number of model lookback
  sessions. Initial new channels: price z-score 20/60, price-delta z-score
  20/60, raw volume, and optionally volume z-score 20/60. Any subset of these
  windows/families can be combined; retain existing close channels.
- Rolling statistical windows and model lookback are distinct. Calculate exact
  dependencies: L feature timesteps using a W-session price z-score need
  L+W-1 closes; price-delta z-scores need one additional predecessor close.
  Reuse existing rolling population formulas, precision, quality rules, and
  unready/zero-variance statuses rather than inventing replacements.
- Optional universe filters default off in new jobs. Average daily volume is
  arithmetic mean shares per exchange session, not dollars or transaction
  count. Volatility is population standard deviation of fractional daily
  close-to-close returns, not annualized. Each enabled filter has an explicit
  window and optional lower/upper bounds; require at least one bound. Propose
  20 trailing sessions at the training cutoff as the default window. Retain
  the existing transaction-count rule as a separately named legacy option.
- Support raw return and the confirmed z-score target. A cross-sectional
  target uses the fixed full model universe, with population variance; all
  outcomes must be mature. If all returns tie, transformed targets are zero.
  Raw returns remain available for reporting and portfolio accounting.
- Additional input/output normalization is separate from rolling z-score
  feature construction and target transformation. Default is none. Fit only
  on eligible training samples; freeze and export fitted state. Proposed
  input normalization is per flattened input coordinate; output normalization
  uses one shared scalar transform across all training dates and tickers to
  preserve within-day ordering. Standardization uses population variance;
  min-max maps to [0,1]. Constant fitted dimensions map to zero. Do not clip
  future values to the training range. Record axis, method, parameters,
  versions, and constant-dimension behavior explicitly.
- Inference reverses optional output normalization before ranking. Scores
  remain in the target's units: fractional return or z-score. Cross-sectional
  z-scores cannot be converted to predicted raw returns using an unknown
  future day's mean and variance; never present them as raw returns.
- Extend existing model configuration with hidden widths/count, activations
  per hidden layer (one shared activation may expand to all), and a selectable
  final activation. Input/output widths derive from the frozen sample spec.
  Support existing architecture choices without forcing every new option
  onto an incompatible architecture. Reject unsupported combinations clearly.
- New jobs expose RMSE and pairwise ranking loss; preserve v1 MAE behavior
  under v1 jobs. RMSE means sqrt(mean((prediction-target)^2)), not MSE. Use
  numerically safe zero-error gradients without changing reported units.
  Proposed ranking loss is mean softplus(-sign(y_i-y_j)*(s_i-s_j)) over
  non-tied unordered ticker pairs within each session. Never compare tickers
  from different dates. Form ranking comparisons in the target score space
  after optional output-scale inversion. Exclude tied pairs; skip pairless
  dates for that loss and reject an entirely pairless training set clearly.
  Block pair evaluation to avoid a batch-by-ticker-by-ticker memory allocation.
- Record objective loss separately from MAE/RMSE and ranking evaluation
  metrics. Keep raw-return metrics unavailable when predictions only have
  z-score/ranking meaning. Do not relabel existing MAE fields as RMS.

### Recommendations, simulation, and runs

- Recommendation inference uses the model's exact saved universe, transforms,
  and lookback on the latest completed feature sessions. The requested
  training end/gap is distinct from feature lookback and label maturity.
  Since the owner has already updated data, add a validate-only refresh mode
  that does not contact the provider. Retain existing explicit refresh paths.
- Return symbols, stable IDs, scores, score units, top-n, eligible count,
  median across eligible scores, and median across returned recommendations.
  Default n=5; fewer available eligible tickers return fewer with an explicit
  count; an empty valid selection has empty results and null medians. Preserve
  errors for genuinely unavailable required inputs. Use deterministic ties.
- Historical simulation trains once before evaluation by default and reuses
  that model. Keep existing chronological retraining as a separate explicit
  option. Default horizon is 10 exchange sessions after the fitting boundary,
  initial capital `1000.00`, and simulation n=1, configurable independently.
  Ensure training outcomes are mature before the first simulation decision;
  an end date alone is not proof of that chronology.
- Reuse `alternate_sessions`: buy T at close, sell T+1 at close, buy T+2.
  Reuse `staggered_halves`: split initial cash once; A buys T, sells T+1,
  buys T+2; B buys T+1, sells T+2, buys T+3. Never rebalance the halves.
  Retain equal per-stock budgets, fractional shares, decimal rounding/fees,
  proceeds resting, skipped orders, delayed exits, and marks from the current
  engine. The 10-session horizon includes its first evaluation session; do
  not read an eleventh close to force final liquidation. Show remaining marks
  and open positions explicitly.
- Every training, recommendation, simulation, replay, and rerun attempt is
  recorded under configurable `runs/`, default `<project>/runs/<run_id>/`.
  IDs must not collide or overwrite prior runs. Paired schedule comparisons
  share a parent identity and identical frozen model/scores/settings but have
  independent portfolios/results. Never replace a parent's outputs.
- A run stores versioned original/resolved configuration; anchors and cutoffs;
  ordered ticker IDs/symbols; feature/target/scaler/model/loss specifications;
  seed and software/Git revision including dirty-tree indication; source and
  data revisions; model/checkpoint references and hashes; full predictions
  before top-n selection; eligibility and exclusions; metrics; and the report,
  fills, trades, and equity where applicable. Keep useful failure manifests.
  Never serialize credential file contents. Runtime artifacts are ignored by
  Git; checked-in example configs and prompt/handoff docs remain tracked.
- Separate report inspection, saved-score replay, and rerun. Inspection needs
  no database. Replay uses saved ordered scores, original cutoffs/eligibility,
  and saved execution quotes/evidence/marks, without training or provider
  access. Persist those bounded execution inputs so original accounting can
  be reproduced offline. Replay changes to top-n or schedule require sufficient
  quotes for every potentially selected ticker; otherwise fail with the
  missing scope or require a separately named data-backed replay operation.
- Rerun uses saved settings and pinned resolved dates by default; re-resolving
  relative dates is an explicit option. Record a new child run and any source
  revision difference. Saved hashes alone do not make changed database contents
  reproducible: verify revisions and fail an exact rerun when dependencies are
  unavailable, or require an explicit current-data rerun recorded as such.
  Reuse artifacts for inference/simulation when requested; retraining is a
  separate rerun choice, never an implicit effect of opening a run.

### Standalone database preparation and SQL debugging contract

These requirements come from the owner's follow-up. They apply to every new
table, field, backfill, materialization, and expensive query introduced here.

- First establish whether existing data/tables suffice. Do not create a target
  column for every experiment or persist run-specific scaler state globally.
  Price/delta z-scores already exist; volume z-scores may need an additive
  family/registry/schema change. Document the exact reason for each addition.
- Put SQL in named, inspectable query modules or SQL templates. Expose a small
  typed operation API plus a standalone CLI/tool; do not bury data preparation
  inside a training callback or force the owner to run the whole pipeline.
- Expose separate steps for inspect/plan, explicit schema setup if needed,
  bounded select/export, calculation, apply/upsert, validation, and status/resume.
  Allow selecting one operation, source, date range, ticker scope, and batch
  size. Steps that do not apply to an operation need not be empty commands.
- Separate typed input rows, pure calculation, and typed output rows with
  persistence. A Python/NumPy implementation and a SQL implementation must
  satisfy the same formulas, keys, NULL/status, precision, and timing contracts.
  Prefer a simple correct reference path first; optimize only with evidence.
- The select/export step must emit its SQL, non-secret parameters, result
  columns/types/ordering, dependency scope, and small diagnostic sample. Allow
  bounded results to be calculated outside SQL, reviewed, then applied without
  repeating the entire expensive selection. Verify dependency revisions at
  apply time so stale exported results cannot be declared current.
- Provide render/export SQL for SQL-backed steps, with a database-interface
  script containing editable non-secret variables, resolved validated table
  names, ordered statements, transaction/session requirements, and verification
  SELECTs. Do not leave Python `%s` placeholders in executable GUI scripts.
  SQL rendering must not execute the statements. Validate and quote identifiers;
  application execution still uses parameterized values.
- If a calculation remains in code, say so explicitly. Export its select SQL,
  input/output contract and bounded result-import/upsert script; do not imply
  a complete SQL-only implementation exists. Where a SQL implementation is
  supplied or replaces a select/calculate/update loop, export the complete
  ordered SQL sequence and prove parity with the reference calculation.
- Manual SQL execution must obey existing writer-lock, pending-operation,
  invalidation, completion, and provenance rules, including a safe recoverable
  failure state. Supply the required setup/finalization SQL or a documented
  standalone wrapper around the manual sequence. A direct UPDATE that leaves
  completion metadata stale is not an acceptable debugging workflow. List
  DDL auto-commit implications separately from transactional data steps.
- Writes must be rerunnable and bounded: use stable keys/upserts, explicit
  transactions/checkpoints, validation before completion, and dependency-aware
  correction invalidation. Restart a process without duplicating rows or
  falsely marking an interrupted batch ready. Preserve rejected/unready rows
  as explicit statuses rather than substituting zero or retrying forever.
- Supply EXPLAIN-compatible queries, timing/row counts for each phase, and
  selectable SQL versus code execution for operations with both implementations.
  Benchmark representative bounded fixtures before adding indexes or changing
  batching. Inspect indexes through the registry-selected schema. Report memory,
  transferred rows, rows examined where available, and correctness as well as
  elapsed time; do not infer production speed from tiny fixtures.
- Keep dependency scope, warm-up reads, affected writes, and completion updates
  distinct. Batches may read prior sessions for a rolling calculation but must
  not accidentally overwrite values outside their declared write interval.

## Stage 0 — Establish accepted contracts and a gap map

```text
Implement Stage 0: turn this brief into the accepted implementation contract.

Read current feature/target specifications, training engine/configuration,
model factory, simplified readiness/training workflow, artifact codecs,
recommendation eligibility, simulation engine, reports/viewers, and active
database schema/registry. Read current docs/close_samples.md,
docs/model_training.md, docs/recommendations.md, docs/simulation.md, and the
database simplification contract/results. Inspect the specific historical
files listed above if a missing capability needs clarification.

Consult the owner on the unresolved decisions table. Batch related questions;
do not ask for facts available in the repository. Record confirmed choices,
defaults, numerical definitions, timing, compatibility versions, and new job
configuration examples in docs/training_recommendation_simulation_contract.md.
Do not implement dependent product behavior using an unconfirmed interpretation
of a high-impact decision. Foundation work and the gap inventory may proceed.

Map each requested capability to reuse, extension, or missing implementation.
Define the minimal typed contracts for range expressions/resolution, universe
selection, feature channels, fitted preprocessing, target/loss configuration,
run manifests, and preparation operations. Show one end-to-end data-flow
example and exact persisted identities needed by inference and replay.
Decide public field names and versions here; subsequent stages follow them.
Specify v1 compatibility and v2 defaults explicitly, including optional filters
off rather than accidentally retaining the legacy minimum-transactions gate.
Apply the future-model guidance to these contracts: retain identity mappings
and explicit axes without designing descriptors or variable-order models.
Identify any larger future restructuring as deferred, not a stage prerequisite.

Acceptance: all owner decisions resolved or marked as blocking only their
dependent stages; coherent example configs; an inspected implementation map;
and a handoff that distinguishes completed code from planned capabilities.
```

## Stage 1 — Saved-run foundation

```text
Implement Stage 1: a shared, versioned saved-run API used by later workflows.

Create validated run config/manifest records and a run-directory service with
collision-free creation, atomic status transitions, parent/child references,
and failure recording. Establish contracts for pending/running/completed/failed
states and for incomplete simulation reports separately from process failure.
Store the original sanitized settings and a separately resolved configuration.
Record code/environment identities and dirty-tree indication without embedding
unrelated working-tree contents or secrets. Keep artifacts/checkpoints separate
and content-address their references. Add a runs-root override and ignore the
default runtime runs directory in Git.

Expose read/list/validate operations that need no database, Torch import,
network, or training controller. Reserve manifest sections for the later stages;
do not invent fake successful predictions or migrations. Define a saved run as
portable relative paths within its directory plus explicit external references.

Acceptance: concurrent/same-time creation cannot overwrite; failure retains
usable metadata; atomic reads reject incomplete/corrupt manifests; path handling
works on Windows; sanitized round-trip and parent/child integrity are verified.
```

## Stage 2 — Structured date ranges and dependency planning

```text
Implement Stage 2: shared range resolution for training and evaluation.

Implement the accepted expression schema and anchor-plus-signed-duration form.
Keep it separate from the existing integer date-ID resolver unless a versioned
adapter preserves all old meanings. Inject clock, calendar, coverage snapshot,
and source identity. Resolve original expressions once and persist anchor
evidence and effective sessions through Stage 1. Add a read-only preview command
that shows training/evaluation entry dates, warm-up dependencies, label exits,
and earliest fitting/simulation cutoffs without loading a model.

Do not approximate years as 365 days, sessions as 24-hour intervals, or derive
session chronology from consecutive database IDs. Keep a training-end gap,
model lookback, z-score window, and simulation horizon as distinct settings.

Acceptance: cover every date example in the behavior contract; positive and
negative durations; leap years, weekends, holidays, early closes, invalid or
empty ranges, incomplete coverage, pinned reruns, and explicitly re-resolved
reruns. Assert exact session counts and dependency boundaries.
```

## Stage 3 — Standalone preparation framework and additive schema

```text
Implement Stage 3: the preparation operation API and independently executable
database tools required by later feature/universe work.

Inventory existing schema, readiness, rolling families, SQL helpers, writer
coordination, and correction propagation. Add only the schema/registry changes
actually required by the accepted contract, normally volume z-scores if enabled.
Reuse existing price/delta series; targets and experiment scalers stay in code
or artifacts unless a measured requirement proves persistence is necessary.
Keep Python/Java registry and schema validation consistent for affected tables;
Java model training or duplicate feature algorithms are not required.

Implement the database debugging contract above with named bounded operations,
preview/render-sql, select/export, calculate, apply, validate and status/resume
where relevant. Provide a small complete worked operation with scripts the
owner can paste into the database interface and a code-calculation alternative
with a reviewable intermediate result format. Define lifecycle/provenance
handling for both routes, including interrupted manual execution recovery.
Do not execute changes against the owner's configured production database.

Acceptance: disposable MySQL rehearsal; rerun and crash/resume; source changed
between export and apply; SQL rendering is read-only; SQL/code result parity
where both routes exist; pending readers remain guarded; names respect a custom
registry. Include an operator runbook and representative EXPLAIN/timing output.
```

## Stage 4 — Frozen universe selection and optional filters

```text
Implement Stage 4: automatically determine the ordered training universe.

Use Stage 2's resolved membership and dependency periods. Select complete valid
raw coverage using exchange sessions and source completion states, not MIN/MAX
dates alone. Unknown whole-session coverage must block selection rather than
silently reduce the universe. Produce exclusions and reasons per ticker.
Apply approved optional average-volume and volatility filters using only
approved training information. Compute each rolling statistic using its exact
session window and required predecessors. Disabled filters require no reads
of their optional dependencies. Keep transaction counts separately named.

Use deterministic stable-ID ordering by default; persist ordered symbols as
labels, not identities. Allow an explicit validated ordered ticker subset as
an override for controlled experiments. Empty selection fails before building
a network. Integrate selection/explanation SQL with Stage 3, and save full
universe-selection evidence via Stage 1.

Acceptance: an interior data gap despite matching endpoints; newly listed and
delisted tickers; noncontiguous date IDs; missing versus incomplete coverage;
each filter off/on and combined; exact inclusive thresholds; warm-up shortage;
transaction count versus volume distinction; deterministic ordering; and no
simulation observations used to select the default training universe.
```

## Stage 5 — Rolling feature adapters and volume preparation

```text
Implement Stage 5: connect requested stored/derived channels to shared samples.

Extend the versioned ordered feature specification with price and price-delta
z-scores for 20/60 sessions, raw volume, and accepted volume-z-score channels.
Read through injected typed repositories with revision/readiness evidence.
Implement any new volume preparation through Stage 3 and the existing quality,
correction, completion and pending-operation lifecycle. Reuse current numerical
population z-score semantics. Do not accept stale materializations merely
because the row exists or fill unready values with zero.

Integrate exact warm-up planning with simplified readiness and the shared sample
builder. Read-only model assembly consumes prepared data; an explicit prepare
step may orchestrate the standalone preparation commands. Extend any feature
diagnostics needed to display channel names, windows, values, and exclusions.
Preserve existing close/entry-open feature contracts under their versions.
Retain explicit time/ticker/feature axes in the specification and keep array
flattening in the existing adapters. Preserve identity mappings through sample
assembly without adding stock descriptors or changing accepted input ordering.

Acceptance: price-only, delta-only, volume-only, both windows, and mixed channels;
hand-computed numerical fixtures; L/W dependency arithmetic; zero variance,
missing/unverified values, late receipts, correction invalidation; custom table
names; exact saved shape/order; and equal construction for training/inference.
```

## Stage 6 — Targets and fitted input/output normalization

```text
Implement Stage 6: the approved target transforms and fitted preprocessing.

Extend TargetSpec, target construction, job parsing and readiness propagation
so callers no longer silently instantiate TargetSpec() when a non-default target
was requested. Compute raw labels first, retain them, then apply the approved
z-score transform and optional output normalization. Implement the confirmed
normalization options with a clean fit/transform/inverse API and explicit axes.
Fit on eligible training samples only; inference/holdout never refit.

Version and serialize preprocessing state with exact feature order and ticker
universe. Add these identities to dataset, cache and checkpoint compatibility.
Cross-sectional label statistics are outcome metadata, never inference inputs.
If the owner chose rolling per-ticker targets, implement and record their
trailing baseline/window and inverse-score semantics explicitly instead of
silently substituting the cross-sectional formula.

Acceptance: known raw returns/z-scores; tied and one-ticker cases; normalization
none/standard/min-max; constant dimensions; out-of-training-range values; no
holdout leakage; inverse output scaling preserves ranking; and reordered or
changed specifications cannot reuse incompatible state.
```

## Stage 7 — Model controls, RMSE, and ranking objectives

```text
Implement Stage 7: extend model/loss configuration and the training engine.

Add per-hidden-layer activations plus shared-activation shorthand and selectable
final activation through the existing model factory. Derive widths from the
resolved sample spec. Verify supported architecture combinations explicitly.
Keep numerical sample construction and preprocessing independent of the model
architecture. Existing transformer choices retain their current ordering
contract; do not introduce stock-token models or permutation handling here.
Add a typed loss configuration for RMSE and pairwise ranking while preserving
legacy MAE jobs. Implement the accepted within-session pairwise formula in
bounded blocks with correct weighting across blocks and dates. Pair sampling
is not a silent approximation; add it only as a separately recorded option
if needed and agreed. Preserve seed/reproducibility behavior.

Report objective loss, target-space MAE/RMSE, non-tied pair count and ordering
accuracy with honest units. Aggregate RMSE from total squared errors and count,
not the arithmetic mean of batch RMSEs. Keep raw-return metrics separately
named and unavailable when inversion to return units is impossible.

Acceptance: hand-computed losses; perfect/reversed ordering; translation of
scores; tied/pairless dates; exact zero-error finite gradients; blockwise versus
dense small-fixture parity and gradients; non-finite rejection; batch aggregation;
and final/hidden activation serialization round-trip.
```

## Stage 8 — Training orchestration and standalone model artifacts

```text
Implement Stage 8: an end-to-end configured training run.

Compose resolution, explicit preparation/readiness, universe selection, sample
construction, fitted preprocessing, training and export using typed services.
Keep preparation independently callable and expose a validation/preview path
that does not fit. New jobs may select tickers automatically; resolve their
dimensions before constructing the model. Preserve old explicit-ticker jobs
and their v1 defaults. Clearly distinguish a training-end date/gap from the
actual fitting cutoff and mature label exits.

Extend artifact/checkpoint export/loading and strict codecs with new versioned
feature, target, scaler, model/loss and universe metadata. Export a model usable
without a mutable training run/controller. Bind checkpoint resumes to those
semantics, not just tensor dimensions. Integrate all attempts and outputs with
Stage 1; record excluded samples and metrics with actual units/scopes.
Preserve explicit layout and ticker-to-output mappings through export/reload.
Current artifacts must still reject reordered inputs; leave a future versioned
contract for stock descriptors and variable-order models without implementing it.

Acceptance: tiny deterministic CPU training, artifact reload predictions,
checkpoint resume compatibility, rejection of same-width reordered universes,
chronological holdout exclusion, clear incompatible versions, v1 fixture
compatibility, preserved feature/target/scaler/prediction ticker alignment,
and a complete saved training run with no credentials.
```

## Stage 9 — Latest-data recommendations with symbols and medians

```text
Implement Stage 9: recommend from a saved model and prepared latest data.

Load the artifact-owned specification and scalers; do not rediscover the ticker
universe or override its dimensions from today's survivors. Add the approved
validate-only mode for already updated data, alongside existing refresh modes.
Validate freshness, completed feature sessions, original receipt cutoffs and
decision timing before scoring. Retain current strict production policy.

Rank in the correct post-normalization target space and return ticker symbols,
stable IDs, scores/units, counts and the approved medians. Defaults: top-n=5;
deterministic ties by stable ID. Handle insufficient/zero eligible tickers as
specified without hiding missing required evidence. If dynamic filters were
approved, gate only selection and retain full model input/output ordering.
Save all ordered scores and eligibility before truncation in a new run.
No order submission or notification transport is part of this stage.

Acceptance: latest-session boundary, stale data, validate-only makes no provider
calls, exact artifact layout, shared training/inference preprocessing, ticker
labels rather than ID-only output, odd/even medians, fewer/zero eligible stocks,
ties, incompatible models, and a useful saved failure for missing inputs.
```

## Stage 10 — Train-then-simulate and saved-score replay

```text
Implement Stage 10: historical evaluation and replay using the existing engine.

Add orchestration for fit once before evaluation, then simulate the next 10
exchange sessions by default. Require all fitting outcomes and the artifact
information cutoff to precede the first permitted evaluation decision. Preserve
existing fixed-artifact and chronological-retrain options. Retain the current
accounting implementation for both allocation schedules; do not copy the old
root simulator or replace decimal ledgers with float arithmetic.

Expose initial capital and simulation top-n independently from recommendation
top-n. A paired comparison uses the same frozen model, dates, scores, prices and
fees for independent alternate_sessions/staggered_halves portfolios. Persist
all predictions, decision-time eligibility, execution quotes/evidence/marks,
fills/trades/equity, exclusions and benchmark outcomes for offline inspection
and replay. Quotes sufficient only for original trades cannot support arbitrary
changed top-n; enforce saved-coverage checks for every replay request.

Implement explicit saved-score replay and pinned-settings rerun commands through
typed APIs. New runs link to their parent and record changed settings/data.
Do not claim bit-identical retraining on all hardware; distinguish deterministic
CPU-fixture verification from production reproducibility limitations.

Acceptance: both exact buy/sell cadences through weekends/holidays; independent
half compounding; equal stock budgets; configurable capital/top-n/horizon;
missing entries/exits and final open positions; no future feature/filter reads;
offline original replay matches fills/equity exactly; changed selection checks
quote coverage; parent stays unchanged; and revised database data is detected.
```

## Stage 11 — Run inspection and performance visualization

```text
Implement Stage 11: extend current viewers/reporting to consume saved runs.

Allow choosing a runs root, listing/filtering attempts and child runs, and opening
their typed manifests/reports. Keep explicit report-file viewing working.
Show resolved dates/universe, configuration differences, feature/target units,
normalization/loss, training versus held-out metrics, full/top-n scores and
medians, exclusions, trades and equity. Plot portfolio equity for both schedules,
subportfolio equity/cash, and drawdown; show current benchmark comparisons.
Surface incomplete marks, missing data and open positions alongside results.

Keep report inspection database/network/training-free. UI actions that request
replay/rerun must call the explicit service and create a child run; displaying
or selecting an artifact must not execute it. Check specification compatibility
when comparing runs; label differences and prevent misleading aggregation.
Use historical viewers only to recover useful display behavior, not as runtime
dependencies. Verify that the new report versions remain strictly loadable.

Acceptance: saved successful/failed/incomplete runs; legacy explicit reports;
paired schedule charts with known fixture values; configuration diff; different
target units; missing files/corrupt manifests; and viewer opening has no model,
provider or database side effects.
```

## Stage 12 — Integrated acceptance and operator handoff

```text
Implement Stage 12: verify the complete workflow and document operator use.

Create a small deterministic multi-ticker fixture with sufficient 60-session
warm-up, an interior coverage gap, a rejected filter candidate and at least
10 later simulation sessions. Rehearse explicit standalone preparation, date
preview, universe selection, mixed-channel training, artifact reload,
recommendations with validate-only data, both simulation schedules, offline
replay, and a pinned-settings rerun. Cover raw/z-score targets, normalization
off/on, RMSE and ranking without running an unnecessary full Cartesian product.

Supply checked-in sanitized runnable example job configs and copy/paste commands
for each independent phase. Include a SQL debugging walkthrough: inspect plan,
export bounded inputs, calculate in code, review/apply results, then perform the
equivalent SQL route where implemented and compare. Demonstrate interruption,
resume, and correction invalidation on disposable MySQL. Clearly distinguish
rendering scripts from executing them and list any operator-only production steps.

Run the relevant integration/architecture/regression checks and affected strict
typing. Recheck current docs and update operational guidance to the final
implementation. No live migration or provider requests are needed for acceptance.
Record exact results, unresolved blockers and all operator commands in the
handoff. Completion requires the saved run to be inspectable and its original
portfolio replay to work offline, not merely a successful training call.
```
