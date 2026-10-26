# Database simplification: Luna 5.6 implementation prompts

Run one numbered prompt per conversation, in order. Include the shared preamble
and the selected prompt. Use Luna 5.6 at the effort setting shown. These prompts
describe implementation work; writing this document does not perform it.

Stages 1–11 implement and verify the replacement on disposable databases.
Stage 12 prepares the production migration and rehearses it; applying it to the
real database is a separate operator action. Do not drop production tables as
part of these prompts.

## Shared preamble — include with every prompt

```text
Work in C:\Projects\StockGuidance. Read applicable AGENTS.md instructions and
docs/database_simplification_prompts.md. Inspect git status and preserve existing
user changes, including training-job.json. Implement only the selected stage
and necessary tests/documentation. Do not commit.

This brief supersedes older migration/schema prompt documents where they
conflict. Inspect current code, not just historical documentation. Read the
earlier stage handoffs in docs/database_simplification_results.md. Do not assume
a prerequisite is complete merely because its prompt exists.

Use the configured project interpreter/toolchain and existing dependencies.
Discover available environment tools when necessary; do not assume a named
skill or tool exists. Keep Python and Java contracts consistent. Respect the
selected table-name registry, parameterize SQL values, and validate identifiers.

Use fake providers and explicitly disposable fixture databases for writes and
benchmarks. Never mutate stockforecasting, replace its configuration, download
live market data, delete original archives, or print credentials. Read-only
production inspection is permitted when necessary; avoid expensive full scans.
No automatic schema creation or migration in ordinary readers or training.

Run focused tests and affected type checks, plus git diff --check. Use the
project's pytest/mypy and Maven configurations; report commands and actual
results. SQLite tests do not establish MySQL transaction or performance behavior.
Do not install dependencies or claim unrun checks passed. If a required check is
unavailable, record the blocker and leave that acceptance gate incomplete.

Append a stage handoff to docs/database_simplification_results.md: completion
status, changed interfaces/files, decisions, test results, limitations, and next
stage prerequisites. Implement the stage; do not return only a proposed design.
```

## Accepted behavior and constraints

- The database owns the values used for training/prediction. One configuration
  governs the entire database, including price adjustment, market, interval,
  and source family. Reject incompatible imports rather than mixing sources.
- For missing raw coverage, inspect the exact canonical file at
  `data/market_summary/YYYY/YYYY-MM-DD.ndjson`; download only if needed.
  Completed database data does not require the original file to remain present.
  File edits, sizes, and modification times do not trigger implicit replacement.
- Preserve decimal precision, ticker identity, trading calendars, current
  calculation formulas, and existing training/prediction specifications.
  General feature-vector expansion, volume z-scores, and new models are excluded.
- Preserve interpolation and its acceptance/timing rules. Explicit known-missing
  raw observations have NULL values and a missing state. An absent ticker/date
  alone does not prove an error or justify interpolation.
- `pending_updates` covers initial imports, forced replacements, derived-only
  repairs, and recovery. Persist the full affected/rebuild scope before clearing.
  Clear all dirty descendants before rebuilding any of them. A committed
  `affected_rows_cleared` flag separates these phases.
- Persisted scope survives deletion of dependency metadata. Dependency closure
  includes interpolation endpoints, adjacent deltas, cross-ticker rankings,
  rolling windows, completion state, and relevant cache invalidation. Use actual
  calculation dependencies, not an arbitrary fixed number of calendar days.
- During rebuilding, each calculation unit commits atomically. Presence after
  clearing means that unit was rebuilt; explicit non-ready results also count
  as completed calculations. Missing, invalid, and not-yet-available outcomes
  must never become zero values or perpetual retry loops.
- Serialize writers with the existing database advisory-lock approach. Once
  clearing starts, the operation scope is immutable. New requests wait and are
  planned against the state after the active operation completes.
- Block reads overlapping affected ranges while an operation is unfinished.
  Unrelated reads remain available using a consistent snapshot and pending-state
  check. A missing row is not a safe substitute for this reader guard.
- Remove completed pending entries. Keep persistent raw/delta completion on
  transaction_dates and existing z-score completion metadata; date presence
  alone does not establish complete ticker coverage or derived readiness.
- Use integer relationship keys and readable states. Do not remove fingerprints
  needed by existing model/artifact/cache reproducibility contracts merely to
  eliminate long strings. Hashes are not a replacement for completion state.
- Do not delete saved models. Corrections invalidate affected dataset/prediction
  caches and prevent stale cache reuse; explicitly identify existing artifacts
  trained on an older data revision without silently retraining them.

## 1. Record the replacement contracts — high

```text
Implement Stage 1: establish the exact replacement contract and dependency map.

Inspect database/update_schema.py, table_names.*, repositories, market_data
archive/canonical_store/quality/legacy_derived modules, update/maintenance
workflows, and Java migration writers and z-score calculations. Trace active
training, recommendation, simulation, validation, and maintenance entrypoints.
Distinguish live consumers from historical migration/benchmark-only consumers.

Create docs/database_simplification_contract.md with: the target table inventory;
exact fields/types/state meanings; Python/Java operation interchange; transaction
and reader-lock boundaries; completion granularity; and the dependency edges
each clearing rule must follow. Use one pending row per operation, an integer
ID, timestamps, affected_rows_cleared, failure details, and versioned structured
scope containing explicit work units/ranges. Do not introduce a permanent
per-row dependency graph or a parallel job framework.

Define compact session raw/delta completion fields on transaction_dates and keep
source configuration as a singleton id=1, enforced against additional rows.
Use UTC database datetime fields for new timestamps. Distinguish ingestion time,
market observation time, and historical availability; do not replace one with
another. Preserve both supported strict and retrospective research behavior.

Specify that source payloads needed for reconstruction must be validated and
durably available before clearing. Pending scope identifies prepared payloads;
these are operation-owned temporary artifacts, not a new permanent file catalog.
Resume must not mistake a pre-refresh file for the intended replacement.

Define data_states IDs/codes good, bad, missing, interpolated. Separate source
state from derived calculation statuses such as insufficient history and zero
variance. Define structured repair metadata for exceptional observations with
original/effective values, invalid fields, interpolation endpoints, and timing.
Preserve future-leakage detection and quality propagation across input windows.

Record exact API signatures/types for the planner, pending repository, clearer,
rebuild runner, and readiness guard, to be implemented in later stages. Record
all remaining design decisions here rather than leaving later stages to infer
them. Do not change production runtime or schemas in this stage.
Acceptance: every retired table has a replacement or an explicit retired feature;
every active consumer has a migration destination; recovery invariants are exact.
```

## 2. Measure data-state placement — high

```text
Implement Stage 2: benchmark state on raw rows versus a separate state table.
Read Stage 1 contracts. Build a reproducible MySQL benchmark using disposable
databases, the same ticker/date populations, missing placeholders, state
distribution, and z-score values in both layouts. Include realistic large-table
sizes, narrow ticker lookbacks, broad training ranges, and quality filtering.

Compare z-score joins to raw_stock_data against joins to a ticker/date/state
table. Test sensible equivalent indexes, including the best covering raw-table
index; do not compare an indexed option against an unindexed one. Verify both
queries return identical rows. Measure median and p95 time, plans, index/storage
size, and insert/update cost. Alternate candidate order, warm both candidates,
run at least 20 measured repetitions per query, and record environment/dataset
sizes and cache limitations. Production remains read-only and is not a benchmark
target. Synthetic data must be labeled as such.

Choose the column on raw_stock_data unless the separate table improves median
query latency by more than 10% on the geometric mean of query-case ratios and
does not worsen p95 or measured update latency by more than 10% on any case.
When results are inconclusive, use the raw-column layout. A missing MySQL runtime
is an incomplete measurement gate, not evidence of equality.

Write the benchmark runner, machine-readable measurements, and a concise report
under output/database-simplification/benchmark; record the chosen layout in the
contract and handoff. Exclude generated large fixtures from source control.
Acceptance: reproducible measurements, equal results, and a recorded decision.
```

## 3. Add schema and repositories — high

```text
Implement Stage 3 using the Stage 2 selected layout. Add explicit versioned schema
initialization, registry entries, and typed Python/Java contracts for singleton
configuration, data_states, pending_updates, session completion, source states,
and exceptional repair metadata. Extend existing tables where specified rather
than inventing replacements for raw/delta/z-score data tables.

Implement pending registration, scope loading, failure recording, flag transition,
and completion deletion. Scope becomes immutable once clearing begins. Enforce
the singleton configuration and incompatible-import rejection. Use dates and
integer identifiers for relationships. Preserve existing artifact fingerprints.

Provide explicit schema-version checks so old and new operational code cannot
accidentally share one partially migrated database. Keep current production
entrypoints on the old implementation until the coordinated cutover stage.
Temporary adapters must have a documented removal stage; do not dual-write.

Test schema initialization on a fresh disposable MySQL database, registry
customization, invalid state references, duplicate singleton rows, immutable
scope, transition atomicity, and mismatched schema versions.
Acceptance: new repositories work independently; existing operations still pass.
```

## 4. Implement dependency closure — high

```text
Implement Stage 4: a pure dependency planner producing the durable pending scope.
Follow the dependency map from Stage 1 and verify it against current formulas.
Include interpolation intervals affected by changed endpoints or observations,
both predecessor/follower delta dependencies, session-wide ranks, rolling price
and price-delta windows, completion records, and affected cache revisions.

Resolve transitive closure before deleting any dependency evidence. Include
future-dependent label columns without allowing them into feature inputs.
Use exchange sessions, existing series definitions/windows, and ticker IDs.
Conservative whole-session invalidation is acceptable for cross-sectional
calculations; avoid erasing unrelated raw observations or all subsequent history
when actual dependency windows are bounded. Deterministically deduplicate units.

Persist explicit clear/rebuild work units sufficient to resume without querying
deleted lineage. Do not use hashes to infer missing dependency relationships.
Plan initial loads and derived-only holes as well as replacements.

Test a single correction, consecutive corrections, a changed interpolation
endpoint, a newly filled gap, withdrawn ticker, top-100 boundary change, overlapping
windows, holiday boundaries, and the latest incomplete target. Independently
check expected affected sets rather than duplicating planner logic in tests.
Acceptance: a serialized scope round trip retains every affected unit.
```

## 5. Implement clearing and operation recovery — high

```text
Implement Stage 5: pending-operation coordination and idempotent clearing.
Acquire the existing maintenance writer lock and recover unfinished work before
accepting new update operations. Register durable scope before the first delete.
Validate reconstruction inputs before entering clearing; defer no required
source download until after the only database copy has been erased.

Delete affected records/completion evidence in bounded transactions. Preserve
the entire scope, configuration, dictionary rows, and required source payloads.
If clearing fails, leave affected_rows_cleared false and safely repeat clearing
on restart. Commit true only after all affected tables are clear; recalculation
must be unreachable until that commit succeeds. Implement the overlapping-read
guard with consistent transaction visibility, not a check outside the read
transaction. Include cache invalidation without deleting saved models.

Test interruptions after registration, after individual delete chunks, before
and after the flag commit, and writer-lock loss. Verify no rebuild calls occur
while false, repeated clearing is harmless, and unrelated data/readers survive.
Use disposable MySQL tests for concurrency and transaction visibility.
Acceptance: recoverable clearing and reader exclusion across process boundaries.
```

## 6. Rebuild raw data and interpolation — high

```text
Implement Stage 6: rebuilding raw observations and data states after clearing.
Use validated prepared payloads, direct canonical-file lookup, and an injected
provider fallback. Preserve decimal parsing, complete-response checks, timestamps,
and adjustment validation. Completed database dates require neither file stats
nor an archive catalog. Forced refresh prepares the intended replacement before
clearing; new attempts cannot silently reuse the old file.

Persist known missing rows as NULL observations with a missing state. Do not
create placeholder rows across the Cartesian product of all tickers/dates.
Distinguish nontrading/unlisted securities, unavailable provider data, and actual
invalid observations using existing coverage rules. Preserve original bad values
and repair metadata according to the contract. Implement current interpolation
and timing semantics using the new state representation.

Commit a raw session/repair work unit with its state and completion metadata in
one transaction. On resume skip completed units, including explicit bad/missing
results. A partial batch must not look like a complete source session. Do not
mistake source bad/missing states for temporary update state.

Test missing/local/downloaded inputs, malformed files, duplicate tickers, exact
decimals, gaps with/without endpoints, replacement recovery, and interruptions
within a raw batch. Compare effective values with the existing implementation.
Acceptance: catalog-free raw loading and preserved interpolation on fixtures.
```

## 7. Rebuild deltas and z-scores — high

```text
Implement Stage 7: adapt Python and Java numerical writers to the new operation
scope, states, and session completion. Rebuild raw/repair units before deltas and
deltas before dependent z-scores. Reuse existing numerical kernels and precision.
Do not create a second numerical implementation merely for the new coordinator.

Commit each planned calculation unit atomically, including row states, aggregated
input quality, and completion. When resuming after clearing, preserve completed
rebuilt units instead of deleting them again. Complete explicit non-ready results
without inventing numeric values. Defer genuinely unavailable future-dependent
targets without leaving every latest-date operation permanently unfinished.

Propagate bad/interpolated provenance over the actual input set. Joining a z-score
only to its same-date raw state is insufficient. Retain enough endpoint/timing
information to enforce current historical policies. Invalidation must clear
affected z-score completion entries and dataset/prediction caches consistently.

Test restart between batches and between rows/completion writes, clean and
interpolated inputs, window warm-up, zero variance, missing predecessor/follower,
cross-sectional rank changes, and numerical equality with independent references.
Acceptance: fresh and resumed runs produce equivalent values/states/completion.
```

## 8. Cut over maintenance and validators — high

```text
Implement Stage 8: switch supported update_market_data, maintain_daily, and
update_zscores paths together to the new coordinator and schema version. Remove
their dependence on archive_files, daily_source_files, imports, old in-progress
markers, migration completion, verification runs, and old quality repositories.
Use the shared contracts in Python and Java; do not leave an alternate CLI able
to write using old assumptions. Preserve documented CLI options where their
meaning still applies; clearly reject retired options with actionable messages.

Normal updates inspect database completion first, fill missing coverage through
files/provider, repair missing derived units from database input, and resume
pending operations. Do not refresh completed history because a file was touched.
Remove successful pending entries only after planned work passes validation.

Adapt independent read-only validators to state/completion semantics. Report an
unfinished operation distinctly from a bad observation or expected non-ready
z-score. Validators must not repair or initialize schemas. Produce useful date,
ticker, phase, and reason diagnostics without opaque keys as the primary output.

Test each CLI and Python/Java handoff, fileless completed sessions, forced
refresh, unfinished-operation recovery, schema-version mismatch, and no unwanted
provider calls. Acceptance: all supported maintenance paths use one update model.
```

## 9. Integrate training, prediction, and historical evaluation — high

```text
Implement Stage 9: connect existing job frontends to the simplified readiness
workflow. Expand requested ranges for current feature lookback, normalization
warm-up where applicable, and label dependencies. Before model computation,
ensure required database coverage, retrieving missing raw dates through local
files/provider and rebuilding missing derived units. Preserve read-only behavior
of low-level feature/query repositories; orchestration owns writes.

Use current feature/target definitions. Do not add generalized SQL feature
vectors, volume features, new z-score families, or model architectures. Keep
training and inference on the same feature construction path and preserve
strict versus retrospective availability policies and interpolation exclusions.

Run prediction on the requested latest x trading sessions through existing job
settings. Historical evaluation compares predictions against mature subsequent
actual outcomes and displays existing result metrics. Keep evaluation targets
outside fitting inputs and prevent train/evaluation overlap or future leakage.
Pending affected ranges must block computation; report coverage failures clearly.

Test training coverage filled locally and via fake provider, fileless completed
database history, latest inference without future outcomes, historical scoring,
insufficient history, and stale cache rejection after corrections. Preserve
saved artifacts and identify older data revisions without automatic retraining.
Acceptance: the user's workflow works end-to-end with current feature support.
```

## 10. Remove retired operational storage — medium

```text
Implement Stage 10: finish source cleanup after Stages 1–9 pass. Remove new-schema
creation and runtime references for old archive/source-file/import/in-progress,
verification, migration, and quality tables replaced by the selected design.
Remove temporary adapters. Preserve only genuinely necessary exceptional repair
metadata; do not recreate the retired schema under new names.

Inspect historical migration/benchmark commands explicitly. Retire obsolete
commands with clear CLI errors or isolate legacy support so it cannot be used
against the new schema. Keep fixtures needed for regression references. Update
packaging, registries, examples, and maintained docs; label archived docs as
historical rather than rewriting their historical evidence.

Search for retired table names and classify every remaining reference as
migration input, legacy fixture, historical documentation, or unintended live
dependency. No ordinary reader/writer may require retired tables. Do not execute
DROP TABLE on the production database.
Acceptance: a freshly initialized new schema passes active workflows with none
of the retired operational tables present.
```

## 11. Run full recovery and regression acceptance — high

```text
Implement Stage 11: independent acceptance across completed stages. Add only
missing behavioral coverage, especially process-kill/restart tests on disposable
MySQL. Exercise interruption at every clearing/rebuilding boundary, repeated
restart, concurrent reader/writer behavior, overlapping requests, and a second
correction queued while the first is rebuilding.

Verify immutable saved scope, no rebuilding before clear, preservation of rebuilt
rows on resume, pending cleanup, unrelated-data stability, and exact dependency
coverage. Compare numerical values, quality, timing, and completion independently.
Cover empty/missing/invalid source inputs, source mismatch, known missing rows,
interpolation timing, holiday windows, and future-dependent final labels.

Run the full Python suite, mypy, Maven tests, and new MySQL integration checks
using the configured tools. Repeat the state-layout benchmark if implementation
changed its indexes/query shapes. Report unavailable checks honestly. Document
any changed public behavior and fix regressions within this redesign's scope.
Acceptance: publish a reproducible report with passed/failed/incomplete gates;
do not declare production migration ready with missing critical MySQL evidence.
```

## 12. Prepare and rehearse production migration — high

```text
Implement Stage 12: deliver an explicit operator migration tool and runbook,
then rehearse the whole process against a disposable old-schema fixture or
explicitly authorized restored copy. Do not apply it to stockforecasting.

Default to a read-only plan. Require explicit apply and retirement phases with
an exact target schema and verified backup/export locations. Quiesce writers
and reject unfinished legacy work until resolved. Export retired metadata;
verify backup/restore usability before destructive retirement. Parameterize
credentials through existing configuration without embedding them in output.

Migrate the singleton source configuration, session completion, source states,
interpolation originals/endpoints/timing, and derived quality. Validate that
previously complete data remains complete; unknown legacy completion must be
independently checked or scheduled for repair, never blindly marked good.
Preserve raw/delta/z-score numerical content and model/artifact files.

Migrate additively first, validate counts/key sets/states/values and representative
training/prediction results, then perform the explicit version cutover. Retire
old tables only after replacement checks pass. Make retries resumable and
document which phase can be rolled back using schema restoration versus backup.
Do not promise rollback of destructive DDL through ordinary transactions.

Update daily_maintenance.md, market_storage.md, database_validation.md, and
data_configuration.md with the final schema/workflow and recovery commands.
Provide exact operator commands, expected outputs, pause/restart behavior,
backup restoration instructions, and final acceptance checks. Record rehearsal
results and remaining production-only steps in the implementation handoff.
Acceptance: concrete migration artifacts and a successful disposable rehearsal;
the real database remains unchanged until separately authorized execution.
```
