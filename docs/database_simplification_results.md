# Database simplification implementation handoffs

## Stage 1 — replacement contracts and dependency map

Status: complete for Stage 1 documentation and repository inspection. No
production runtime, schema, or database was changed.

### Changed files

* `docs/database_simplification_contract.md` — target inventory, field/type and
  state contract, Python/Java interchange, transaction and reader boundaries,
  dependency closure, later-stage API signatures, live-consumer destinations,
  retired-feature decisions, and recovery invariants.
* `docs/database_simplification_results.md` — this handoff.
* Python typing fixes across configuration loading, database repositories,
  market-data planning, workflows, and imported legacy/performance helpers.

The pre-existing untracked `docs/database_simplification_prompts.md` was
preserved unchanged. No commit was created.

### Decisions recorded

The current repository still creates and consumes the old archive catalog,
source-file fingerprint, partial-import, quality, and migration-completion
tables. They are documented as current behavior and later migration
destinations, not treated as already retired. The replacement keeps the eight
existing registry roles, adds logical roles for `data_states`,
`pending_updates`, and `repair_metadata`, and defers only the physical raw
state placement to Stage 2's required disposable-MySQL benchmark.

The contract uses one singleton source configuration, persistent raw/delta
completion on `transaction_dates`, one immutable pending scope per active
operation, operation-owned prepared payloads, integer relationship keys,
explicit source/calculation states, UTC `DATETIME(6)` timestamps, and atomic
per-unit rebuild completion. It preserves Decimal source precision, the
current derived formulas, z-score status/fingerprints, strict versus
retrospective availability, interpolation endpoint/timing rules, cache
invalidation, and saved artifacts.

### Verification

The following checks were run after the documentation and typing fixes:

* Focused Python database/architecture tests: **passed**, 66 tests, with 4
  existing pandas deprecation warnings:
  `.venv\Scripts\python.exe -m pytest tests/database
  tests/architecture/test_package_imports.py`.
* Affected Python mypy check: **passed**, no issues in 93 source files:
  `.venv\Scripts\python.exe -m mypy src/stockguidance/database
  src/stockguidance/market_data src/stockguidance/workflows`. The fixes
  address the shared `SourceConfig.identity` union, typed JSON configuration
  values, missing optional narrowing, repository row types, workflow variable
  inference, and imported legacy/performance helper annotations without
  changing database behavior.
* Java Maven tests: **passed** with `mvn -q -f java/pom.xml test`. Maven/JVM
  emitted existing restricted/deprecated API warnings and one expected test
  stack trace for the credential-safe connection-failure test; Maven exited 0.
* `git diff --check`: **passed** for the new untracked contract using
  `git diff --no-index --check -- NUL docs/database_simplification_contract.md`;
  an explicit trailing-whitespace check also passed for both new documents.

The ordinary `git diff --check` command does not inspect untracked files, so the
no-index form was used for the new contract and the explicit whitespace check
covered both new documents.

### Limitations and next-stage prerequisites

Stage 1 intentionally does not add schemas, repositories, planners, clearing,
rebuilding, reader guards, benchmark fixtures, or runtime cutover. The current
production database was not inspected or modified, no provider was contacted,
and no dependency was installed. Stage 2 must benchmark and select raw-state
placement. The Stage 2 handoff below records that benchmark and selection.
Stage 3 must implement the selected schema/registry and typed repositories with
explicit schema-version checks before later operational stages can use this
contract.

## Stage 2 — raw-state placement benchmark

Status: complete. The benchmark and selection gate passed on a disposable
MySQL 9.5.0/InnoDB schema. The configured `stockforecasting` database was never
selected or modified; the disposable schema was dropped after measurement.

### Changed files and artifacts

* `src/stockguidance/performance/database_simplification_stage2.py` —
  deterministic synthetic population, disposable-schema guard, equivalent
  raw-column/separate-state DDL and indexes, z-score join cases, result-digest
  verification, plans/storage/index inspection, insert/update measurements,
  alternating order, warm-up, and decision rule.
* `tools/database_simplification_stage2.py` — reproducible command-line runner
  and JSON/concise-report output.
* `tests/performance/test_database_simplification_stage2.py` — deterministic
  population, index-equivalence, fallback, and selection-rule checks.
* `docs/database_simplification_contract.md` — selected raw-column layout and
  completed Stage 2 prerequisite.
* `output/database-simplification/benchmark/stage2-measurements.json` — full
  machine-readable measurements (ignored generated output).
* `output/database-simplification/benchmark/stage2-report.md` — concise
  generated report (ignored generated output).

### Dataset and decision

The synthetic fixture is explicitly labeled and pinned to seed `20261026`: 504
weekday-only synthetic sessions, 1,500 tickers, and 756,000 raw/z-score rows
per layout. Both layouts received identical ticker/date keys, 15,120 `missing`
placeholders, the same `good`/`bad`/`missing`/`interpolated` distribution, and
the same z-score values/statuses. Query cases were a narrow ticker lookback,
a broad training range, and a recent quality-filtered range.

All three query cases returned identical row counts and SHA-256 result digests.
Each candidate had 20 measured repetitions, two warm-ups, and alternating
candidate order. The geometric mean of separate-state/raw-column median query
ratios was `1.029464`; separate-state therefore did not improve median query
latency by more than 10%. Its insert median ratio was `1.661146` and update
median ratio was `22.533847`; its p95 ratios were also above the allowed 1.10
limit. The selected layout is `raw_column`, with `state_id` on
`raw_stock_data`.

### Verification

* Full disposable benchmark: **passed**, including equal results, plans,
  table/index sizes, insert/update costs, environment, cache limitations, and
  20 repetitions:
  `.venv\Scripts\python.exe tools\database_simplification_stage2.py
  --dbinfo-file replacement-dbinfo.id --schema
  stockguidance_disposable_stage2_20261026 --output
  output\database-simplification\benchmark\stage2-measurements.json`.
* Focused Stage 2 tests: **passed**, 4 tests:
  `.venv\Scripts\python.exe -m pytest
  tests/performance/test_database_simplification_stage2.py -q`.
* Affected mypy check: **passed**:
  `.venv\Scripts\python.exe -m mypy
  src\stockguidance\performance\database_simplification_stage2.py`.
* Python compilation and deterministic population/fallback checks: **passed**.
* `git diff --check`: **passed**; explicit no-index whitespace checks for the
  new untracked files found no whitespace errors.
* Post-run disposable-schema existence check: **passed**; the generated schema
  no longer existed. No provider calls or production writes occurred.

### Limitations and next-stage prerequisites

The fixture uses a synthetic weekday-only calendar and local MySQL cache state;
buffer-pool/page-cache eviction was not forced, although both candidates were
warmed and measured in alternating order. The result is a layout-selection
benchmark, not a production capacity claim. Generated large data and reports
remain outside source control.

Stage 3 may implement the selected `raw_stock_data.state_id` layout, add the
new registry/schema-version/repository contracts, and retain the benchmark
artifacts as the Stage 2 performance evidence. It must not switch ordinary
production entrypoints or implement any later stage in this handoff.

## Stage 3 — schema and repositories

Status: complete for the Stage 3 implementation and acceptance gates. No
production entrypoint was cut over, no production database was selected or
modified, and no commit was created.

### Changed files and interfaces

* `src/stockguidance/database/table_names.py` and `table_names.json` add the
  replacement registry roles `schema`, `data_states`, `pending_updates`, and
  `repair_metadata`. Legacy `tables()` serialization remains available so
  existing artifact/manifest fingerprints do not acquire replacement-only
  roles; replacement setup uses the explicit replacement role set.
* `src/stockguidance/database/simplified_schema.py` adds the explicit,
  versioned `initialize_simplified_schema`/`require_simplified_schema` path.
  It creates the selected raw-column `raw_stock_data.state_id` layout, source
  states, session completion fields, repair metadata, pending updates, and
  retained z-score metadata/output tables. It rejects legacy or incomplete
  databases rather than performing additive migration.
* `src/stockguidance/database/simplified_contract.py` defines immutable typed
  Python contracts for source configuration, source/calculation states,
  session completion, repair metadata, prepared payloads, pending scopes,
  failures, and operation updates. Scope JSON is canonical, versioned, strict
  about fields/timestamps/IDs, and preserves Decimal values as strings.
* `src/stockguidance/database/repositories/simplified.py` adds independent
  source-configuration, data-state, session-completion, repair-metadata, and
  pending-update repositories. Pending registration, loading, failure detail,
  clear-flag transition, and completed-row deletion preserve the stored scope;
  incompatible source configuration and multiple active operations are
  rejected.
* `java/src/main/java/stockguidance/database/SimplifiedContracts.java` adds
  matching Java enums/records and strict scope encoding/decoding. Java table
  registry loading accepts both historical registries and the new optional
  replacement roles; legacy migration registry comparison remains compatible
  with either form.
* `tests/database/test_database_simplification_stage3.py`,
  `tests/documentation/test_zscore_documentation.py`,
  `java/src/test/java/stockguidance/database/SimplifiedContractsTest.java`,
  and `tools/database_simplification_stage3.py` provide focused unit,
  cross-language contract, and disposable-MySQL acceptance coverage.
* `docs/database_simplification_contract.md` records the Stage 3 schema and
  compatibility boundary. No temporary adapter or dual-write path was added.

### Decisions and limitations

The target operational schema is version `1` and records the selected registry
digest in `stockguidance_schema_version`. All new repositories require that
metadata and the complete base target table set before operating. The source
singleton stores the semantic configuration and full source identity, with
`id=1` checks and repository-level compatibility rejection. The raw-column
layout was used as selected by Stage 2; the separate state-table benchmark
candidate was not added.

The repository layer does not acquire the later maintenance advisory lock and
does not implement planning, dependency closure, clearing, rebuilding,
readiness guards, cache invalidation, or runtime cutover. Those remain Stage 4
and later work. The SQLite unit fixture does not establish MySQL concurrency
behavior; the disposable MySQL gate below verifies the schema, foreign-key
state rejection, singleton check, source mismatch, version mismatch, session
completion, and pending lifecycle only.

### Verification

* **Passed:** `.venv\Scripts\python.exe tools\database_simplification_stage3.py --dbinfo-file replacement-dbinfo.id --schema stockguidance_disposable_stage3_20261026 --windows 20 60`. The runner created and dropped only the explicitly prefixed disposable schema.
* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database -q` — 72 tests, with 4 existing pandas deprecation warnings. The focused Stage 3 file contributes 7 passing tests.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database` — no issues in 33 source files.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database tools\database_simplification_stage3.py` — no issues in 34 checked files.
* **Passed:** `mvn -q -f java\pom.xml test`. Maven completed successfully, including the new contract tests; it emitted the existing expected credential-safe connection-failure stack trace and JVM warnings.
* **Passed:** `git diff --check` and explicit no-index whitespace checks for the new untracked Stage 3 files.

Stage 4 prerequisite: implement the pure dependency planner against these
contracts, including complete persisted clear/rebuild scope and independently
verified dependency closure. Do not treat this Stage 3 schema/repository gate
as evidence that later recovery, reader locking, numerical rebuilding, or
production migration is complete.

## Stage 4 — dependency closure planner

Status: complete for the pure Stage 4 planner and acceptance gates. The planner
is database-neutral; it performed no database writes, provider calls, file
access, schema initialization, or production inspection. No commit was created.

### Changed files and interfaces

* `src/stockguidance/database/dependency_planner.py` adds the immutable
  `UpdateRequest`, `SnapshotSession`, `InterpolationInterval`,
  `ZScoreSeriesSnapshot`, `CacheEntry`, `CacheRevision`, and `DatabaseSnapshot`
  inputs (including prepared replacement ticker IDs) plus the concrete `DependencyPlanner.plan()` implementation. The
  planner emits the existing versioned `PendingScope` contract, with separate
  clear and rebuild units for raw sessions, repair rows, delta rows, z-score
  sessions, and raw/delta/z-score completion records.
* `tests/database/test_database_simplification_stage4.py` adds independent
  closure expectations for corrections, consecutive changes, interpolation
  endpoints, derived-only holes, holiday/session boundaries, ranking breadth,
  latest incomplete targets, cache bounds, deterministic deduplication, and
  scope serialization.
* `docs/database_simplification_contract.md` records the concrete Stage 4
  planner inputs, bounded cache behavior, and formula-window closure.
* `docs/database_simplification_results.md` records this handoff.

### Decisions recorded

The planner treats exchange-session sequence numbers, never calendar-day
offsets, as the dependency coordinate. A raw change plans only its raw session,
the actual predecessor/current/follower delta keys, and the full actual
predecessor population when the follower-session top-100 boundary can change.
Interpolation closure is limited to persisted exceptional rows inside the
affected endpoint interval. Missing or withdrawn keys are not expanded into a
Cartesian ticker/date set.

Configured z-score series are read from the snapshot. A price window of `W`
plans scored sessions from the changed close through `W-1` following sessions;
a price-delta window plans through `W` following sessions because it consumes
the predecessor close. Derived-only delta holes affect price-delta series, while
missing z-score completion records are planned explicitly for the requested
session. Future-dependent legacy label columns remain in delta closure and are
not treated as feature inputs.

The durable scope is sorted and deduplicated by unit kind, session, integer
relationship IDs, series key, and window. Cache entries use their recorded
dependency dates; aggregate cache revisions are bounded using the existing
feature lookback, normalization warm-up, and target horizon. The planner is
pure and does not infer relationships from hashes.

### Verification

* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database\test_database_simplification_stage4.py -q` — 8 tests.
* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database -q` — 80 tests, with the 4 existing pandas deprecation warnings.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database\dependency_planner.py tests\database\test_database_simplification_stage4.py` — no issues in 2 files.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database` — no issues in 34 source files.
* **Passed:** `git diff --check`; an explicit trailing-whitespace scan found no
  trailing whitespace in the changed tracked and untracked files.

### Limitations and next-stage prerequisites

Stage 4 does not register pending operations, acquire the maintenance lock,
clear rows, rebuild calculations, guard readers, invalidate caches, or switch
any runtime entrypoint. `DatabaseSnapshot` is the coordinator-facing evidence
boundary; later code must populate it from one consistent database snapshot and
must provide date IDs and actual ticker keys before registration. Stage 5 must
persist this complete scope before clearing, validate prepared reconstruction
payloads, coordinate recovery, and implement idempotent bounded clearing and
reader exclusion. SQLite/unit planner tests do not establish MySQL transaction
or concurrency behavior.

## Stage 5 — pending-operation coordination and idempotent clearing

Status: complete for the Stage 5 coordination, clearing, recovery, cache
invalidation, and reader-guard acceptance gates. No production entrypoint was
cut over, no production database was selected or modified, and no commit was
created.

### Changed files and interfaces

* `src/stockguidance/database/operation_recovery.py` adds
  `PendingOperationCoordinator`, `DatabasePendingClearer`,
  `DatabaseReadinessGuard`, `consistent_read_transaction`, prepared-payload
  validation, `ClearResult`, `ReadScope`, `Readiness`, and the later-stage
  `RebuildRunner`/`RebuildResult` boundary. The coordinator recovers the one
  durable pending row before planning another request, registers complete
  scope before deleting, and does not implement numerical rebuilding.
* `src/stockguidance/database/repositories/simplified.py` makes
  `mark_cleared` verify every persisted raw, repair, delta, z-score, and
  completion unit before committing `affected_rows_cleared = 1`. The scope and
  source/dictionary rows are never rewritten or deleted.
* `src/stockguidance/workflows/market_lock.py` retains the existing advisory
  lock name and adds connection-identity heartbeats with `IS_USED_LOCK` so
  lock loss aborts clearing before another chunk or the flag transition.
* `tests/database/test_database_simplification_stage5.py` covers registration
  recovery, bounded interruptions, failure before the flag commit, idempotent
  repeats, unrelated data, cache invalidation, lock loss, reader overlap, and
  the no-rebuild boundary.
* `tools/database_simplification_stage5.py` is a disposable-MySQL acceptance
  runner for transaction visibility and advisory-lock contention.
* `docs/database_simplification_contract.md` records the Stage 5 boundaries;
  this handoff records the implementation evidence.

### Decisions

The clearer commits one bounded unit-kind chunk at a time. Raw and delta
completion remain on `transaction_dates` and are reset rather than deleting
the session row. Z-score rows and completion metadata are removed by their
integer date/ticker or series/date keys; source configuration, state
dictionary, ticker/date dictionaries, prepared payload descriptors, and saved
models are preserved. Cache invalidation is an injected idempotent sink and is
performed only after database chunks succeed; a sink failure leaves the
durable clear flag false for safe retry.

The pending row is reloaded before clearing and its canonical scope must match
the registered scope. `mark_cleared` independently verifies that all scoped
database evidence is absent, so a process interruption before or during the
flag transaction cannot make recalculation reachable. Recovery revalidates
prepared payload existence and SHA-256 content before repeating deletes. A
Stage 5-only coordinator raises `RebuildRequiredError` once clearing is
committed instead of silently accepting a second update; a supplied later
runner is invoked only after the true flag is visible.

Readiness is checked through the caller's open repeatable-read transaction.
The guard compares the requested date-level or explicit work-unit scope with
both clear and rebuild units. A pending overlap returns
`unfinished_operation`; an unrelated read remains available from its own
consistent snapshot. Missing rows are not used as a readiness signal.

### Verification

* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database\test_database_simplification_stage5.py -q` — 7 tests.
* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database -q` — 87 tests, with the 4 existing pandas deprecation warnings.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database src\stockguidance\workflows\market_lock.py tools\database_simplification_stage5.py` — no issues in 37 source files.
* **Passed:** `.venv\Scripts\python.exe tools\database_simplification_stage5.py --dbinfo-file replacement-dbinfo.id --schema stockguidance_disposable_stage5_20261026` — disposable MySQL coordination and visibility acceptance passed; the prefixed schema was dropped in cleanup.
* **Passed:** Python compilation for the new Stage 5 module and runner.
* **Passed:** `mvn -q -f java\pom.xml test` — Java regression suite passed; Maven emitted the existing JVM warnings and expected credential-safe connection-failure test trace.
* **Passed:** `git diff --check` before this handoff update; the final check is rerun after documentation changes.

### Limitations and next-stage prerequisites

The current maintenance and update CLIs still use the historical workflow by
design; coordinated runtime cutover remains Stage 8. Stage 5 supplies no raw
loader, interpolation rebuild, delta calculator, z-score writer, cache store,
or production migration. The default cache sink is intentionally inert until
the later cache implementation supplies a real idempotent invalidator; callers
that own caches must inject it before relying on invalidation. SQLite tests
cover failure injection, while the disposable-MySQL runner covers actual
transaction visibility and advisory-lock behavior; neither is evidence about
production capacity or migration readiness. Stage 6 must consume the prepared
payloads and only start rebuilding after the committed clear flag.

## Stage 6 — raw observations and interpolation

Status: complete for the Stage 6 raw/payload/interpolation acceptance gates.
No production entrypoint was cut over, no production database was selected or
modified, no live provider was contacted, and no commit was created.

### Changed files and interfaces

* `src/stockguidance/database/raw_rebuild.py` adds `RawPayloadPreparer`,
  `FilePreparedPayloadReader`, `read_canonical_file`, `RawRebuildRunner`, and
  the validated-payload/error contracts. Preparation reads the exact canonical
  file without archive-catalog or file-stat invalidation, uses an injected
  provider only when the canonical input is absent or a forced replacement is
  requested, and stages a fresh hash-bound artifact before clearing.
* `src/stockguidance/database/simplified_contract.py` allows original repair
  payload fields to retain nonnumeric source text as well as Decimal values;
  effective and endpoint values remain Decimal-precision fields.
* `src/stockguidance/database/repositories/simplified.py` decodes preserved
  nonnumeric original repair values without losing the source payload.
* `tests/database/test_database_simplification_stage6.py` covers canonical and
  provider preparation, forced refresh, adjustment mismatch, malformed and
  duplicate inputs, exact source identity, missing placeholders, no Cartesian
  expansion, bounded interpolation with and without endpoints, resume skip,
  and rollback/retry after an interruption.
* `tools/database_simplification_stage6.py` runs the raw/interpolation gate on
  an explicitly prefixed disposable MySQL schema.
* `docs/database_simplification_contract.md` records the concrete Stage 6
  payload and runner boundary; this handoff records the evidence.

### Decisions and limitations

Canonical NDJSON is decoded by the existing `decode_archive` path, so complete
response counts, duplicate ticker rejection, source timestamps, Decimal
precision, and OHLC/field validation remain shared with the current source
implementation. Provider fallback responses are normalized through the same
complete-response checks. No-trading responses complete the session without
creating rows; explicit zero placeholders or ignored provider keys create only
their own NULL-valued `missing` rows. Other invalid records retain safe,
representable raw fragments and the complete original values in repair metadata
with state `bad`.

Raw sessions commit rows, repair metadata, availability/observation timing, and
raw completion together. Repair commits are separate atomic work units and
preserve the existing `linear-trading-session-v1` interpolation formula. The
injected calendar supplies session ordinals; explicit missing rows block an
interpolation interval, and absent rows never become inferred placeholders.
Completed raw revisions and stable repair revisions are skipped during resume.
The runner deliberately returns later delta/z-score units as deferred, so it is
not yet a complete operation runner and is not wired into the Stage 5
coordinator. Stage 7 must compose it with the existing numerical kernels before
the pending operation can be deleted. The existing Stage 4/5 scope model still
requires prepared payload descriptors for any raw unit that reaches this
runner.

### Verification

* **Passed:** `.venv\Scripts\python.exe -m pytest
  tests\database\test_database_simplification_stage6.py -q` — 8 tests.
* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database -q` — 95
  tests, with the 4 existing pandas deprecation warnings.
* **Passed:** `.venv\Scripts\python.exe -m mypy
  src\stockguidance\database tools\database_simplification_stage6.py` — no
  issues in 37 source files.
* **Passed:** Python compilation for `raw_rebuild.py` and the Stage 6 runner.
* **Passed:** `.venv\Scripts\python.exe tools\database_simplification_stage6.py
  --dbinfo-file replacement-dbinfo.id
  --schema stockguidance_disposable_stage6_20261026` — raw rows, source states,
  interpolation metadata, MySQL Decimal values, and repeat-resume behavior
  passed; the prefixed disposable schema was dropped in cleanup.
* **Passed:** `mvn -q -f java\pom.xml test` — the Java regression suite passed;
  Maven emitted the existing JVM warnings and expected credential-safe
  connection-failure test trace.
* **Passed:** the Stage 6 SQLite interruption fixture verified that a raw-batch
  failure rolls back both rows and `raw_complete`, and a retry completes it.
* **Passed:** no provider calls were made by the acceptance runner and no
  production database/configuration was selected.

### Next-stage prerequisites

Stage 7 must consume the committed raw/repair units before rebuilding deltas
and z-scores, preserve the current numerical kernels and quality propagation,
and handle deferred future-dependent outcomes without retry loops. It must
compose the Stage 6 runner with the later calculation units and only remove a
pending operation after all persisted scope units validate. Runtime maintenance
cutover remains Stage 8; this stage intentionally does not modify those paths.

## Stage 7 — numerical rebuilds

Status: complete for the Stage 7 numerical rebuild implementation and
acceptance gates. No supported maintenance entrypoint was cut over, no
production database was selected or modified, and no commit was created.

### Changed files and interfaces

* `src/stockguidance/database/numerical_rebuild.py` adds
  `SimplifiedNumericalRebuildRunner` (also exported as `NumericalRebuildRunner`).
  It composes `RawRebuildRunner`, validates the simplified schema, orders raw /
  repair, delta, and z-score units, and returns the existing `RebuildResult`
  contract without deferred calculation units.
* The Python writer uses `legacy_rows` for all sixteen delta columns and
  `calculate_zscores` for price and price-delta windows. Delta units atomically
  publish states, quality/provenance, and `delta_complete`; z-score units merge
  unaffected rows and atomically publish rows plus completion metadata. Resume
  checks completion evidence and preserves completed units.
* `java/src/main/java/stockguidance/database/SimplifiedNumericalKernel.java`
  and `SimplifiedNumericalWriter.java` provide the matching Java kernel/writer
  surface. The kernel delegates the existing legacy formula implementation;
  the JDBC writer uses the `TableNames` registry, parameterized values, and
  per-unit transactions.
* `tests/database/test_database_simplification_stage7.py`,
  `java/src/test/java/stockguidance/database/SimplifiedNumericalKernelTest.java`,
  and `tools/database_simplification_stage7.py` cover offline edge cases,
  Java formula parity, and a disposable MySQL fresh/resume/latest-date run.
* `src/stockguidance/database/repositories/mysql_zscores.py` accepts both the
  existing textual status-count encoding and the valid JSON-object encoding
  written to the target JSON column. The Stage 7 target writer emits the JSON
  object form.
* `docs/database_simplification_contract.md` records the Stage 7 runner and
  atomic-publication boundary. No Stage 8 runtime wiring was added.

### Decisions

Raw and repair work is delegated first, then each delta session commits before
its dependent z-score unit is considered. A latest current close may therefore
remain numeric while future-dependent delta labels are NULL and the row is
marked `not_yet_available`; it is completed rather than retried indefinitely.
Missing, invalid, warm-up, zero-variance, and unavailable inputs remain
explicit statuses with NULL numeric outputs. Interpolated evidence carries
repair revision, endpoint timing, gap length, and quality flags through the
delta and z-score windows. Price-delta reads are limited to completed delta
sessions and checked against raw close ratios. Target-schema completion uses
`delta_complete` or z-score metadata with a matching physical row count.

### Verification

* **Passed:** `.venv\Scripts\python.exe -m pytest
  tests\database\test_database_simplification_stage7.py tests\database
  tests\zscores -q` — 138 tests, with 12 existing warnings.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database` —
  no issues in 37 source files.
* **Passed:** `mvn -q -f java\pom.xml test` — the Java suite passed, including
  the new legacy-kernel/latest-target test; Maven emitted existing JVM warnings
  and the expected credential-safe connection-failure trace.
* **Passed:** `.venv\Scripts\python.exe tools\database_simplification_stage7.py
  --dbinfo-file replacement-dbinfo.id
  --schema stockguidance_disposable_stage7_20261026` — fresh, resumed, and
  latest-date numerical checks passed on disposable MySQL; cleanup dropped the
  explicitly prefixed disposable schema.
* **Passed:** Python compilation of the Stage 7 disposable runner and
  `git diff --check`, plus an explicit trailing-whitespace scan of all new
  source/test files.
* **Passed:** no provider calls or production database/configuration writes
  were used.

### Limitations and next-stage prerequisites

The Java JDBC writer was compiled and exercised through the Maven regression
suite but was not independently driven against MySQL; the disposable MySQL
transaction gate covers the Python target writer. The full repository suite
and production-capacity/performance checks remain outside this stage. Existing
maintenance, validation, training, and prediction entrypoints still use their
historical workflow. Stage 8 must wire the shared coordinator and both numerical
writers into the supported maintenance paths, retain the reader/completion
guards, and remove old workflow assumptions. No Stage 8 implementation was
started here.

## Stage 8 — maintenance and validator cutover

Status: implemented for the supported Python entrypoints and the retired Java
daily-writer guard. No production database, production configuration, provider
request, or commit was used.

### Changed files and interfaces

* `src/stockguidance/database/simplified_runtime.py` is the target-schema
  runtime boundary. It reads the database-owned source singleton and one
  consistent target snapshot, registers only missing exchange sessions, stages
  raw payloads only for planned missing/forced raw units, runs the existing
  raw/derived/z-score kernels through `PendingOperationCoordinator`, and removes
  completed pending rows only after every persisted unit is committed or
  explicitly non-ready. It refuses schema-version mismatches instead of
  falling back to legacy writers.
* `src/stockguidance/database/operation_recovery.py` now accepts a snapshot
  provider so recovery is completed before planning a new request, invokes the
  composed rebuild runner, and verifies that the returned committed/non-ready
  units exactly cover the durable scope.
* `update_market_data`, `maintain_daily`, and `update_zscores` dispatch to the
  same simplified coordinator when the versioned schema is present. Retired
  target-schema options (`--scan-quality`, `--force-verify`, `--repair-plan`,
  `--initialize-schema`, and daily `--stage`) return actionable errors.
  `validate_database` dispatches to the read-only state/completion validator.
* `validate_simplified_database` reports coverage, unfinished operations, bad
  raw observations, missing/invalid deltas, and expected non-ready z-score
  statuses with date/ticker/phase/reason fields; it never repairs or initializes
  a schema.
* `JavaDailyMaintenanceMain` is now a hard-retired entrypoint. It cannot write
  using the old archive/import/migration assumptions; the shared Python/Java
  scope and numerical contracts remain the interchange surface, while the
  supported target maintenance path uses the composed Python coordinator.

### Acceptance checks

* **Passed:** `.venv\Scripts\python.exe -m pytest
  tests\database\test_database_simplification_stage8.py -q` — 7 tests covering
  fileless completed history without provider calls, initial import, forced
  refresh, cleared-pending recovery without a provider retry, validator
  diagnostics, and incompatible-schema refusal.
* **Passed:** the Stage 3–8/database, CLI, validation, and daily-maintenance
  regression selection — 92 passed, 1 skipped, including target-schema
  dispatch tests for update, daily maintenance, z-score, and validation CLIs.
* **Passed:** focused `mypy` over the 9 changed Python source surfaces and
  `py_compile` over the changed runtime/CLI modules.
* **Passed:** `mvn -q -f java\pom.xml test`; Maven emitted its existing JVM
  warnings and expected credential-safe connection-failure trace.
* **Passed:** `java -cp java\target\classes
  stockguidance.migration.JavaDailyMaintenanceMain` returned the actionable
  retired-writer response.
* **Passed:** `git diff --check`.

The full repository run remains subject to an unrelated pre-existing
documentation guard: `tests\documentation\test_zscore_documentation.py` flags
the legacy physical-name reference in `src\stockguidance\market_data\reimport.py`.
That file was not changed as part of Stage 8. A disposable MySQL Stage 8 runner
was not executed; all checks above used disposable SQLite fixtures or offline
Java tests, so no production database was modified.

### Next-stage prerequisites

Stage 9 may consume the simplified readiness/completion boundary for training,
prediction, and evaluation orchestration. Stage 8 did not modify those paths,
remove historical migration fixtures, or implement later cleanup stages.

## Stage 9 — training, prediction, and historical-evaluation integration

Status: complete for the Stage 9 Python integration and acceptance gates. No
production database, production configuration, live provider, or commit was
used. Stage 10 was not implemented.

### Changed files and interfaces

* `src/stockguidance/workflows/simplified_readiness.py` adds the shared Stage 9
  readiness boundary. It expands exchange-session scope for feature lookback,
  optional labels, normalization warm-up, and explicit execution sessions;
  applies strict or retrospective cutoffs; verifies the database-owned source;
  delegates missing raw/derived work to the Stage 8 coordinator; and validates
  completion before model computation.
* `src/stockguidance/database/simplified_runtime.py` carries the readiness data
  revision through the existing target update result and keeps session-context
  expansion bounded at exchange-session edges for the shared planner.
* The same module adds read-only target-schema close and liquidity repositories.
  They use `consistent_read_transaction` and `DatabaseReadinessGuard`, preserve
  availability/revision evidence and interpolation exclusions, and return
  structured date/ticker/phase coverage failures. They do not initialize,
  repair, or refresh the database. A target data revision is exposed for
  result/cache identity and changes after a forced correction.
* `src/stockguidance/workflows/simplified_training.py` connects the existing
  `HistoryTrainingRequest`/`train_from_history` path to target readiness. The
  existing feature construction, target construction, training service,
  checkpoint behavior, and artifact format remain unchanged.
* `training/jobs.py` dispatches target-schema jobs through the new readiness
  workflow while preserving the legacy history-only path when the target
  schema is absent. Target jobs use the database-owned source singleton and
  reject source identity mismatch.
* `recommend.py`, `recommendation_runtime.py`,
  `recommendation_dependencies.py`, `recommendation_result.py`, and
  `market_data/opening_refresh.py` connect target-schema prediction and
  retraining to the same existing sample/eligibility/inference path. Current
  artifact and model-policy preflight remains before target probing, and
  legacy frontends retain their established behavior.
* `simulation_runtime.py` connects fixed historical scoring and chronological
  walk-forward scoring to the target close/liquidity repositories. Evaluation
  execution frames still use the existing `RepositoryExecutionPrices` path,
  while training decisions are filtered by the existing cutoff/precedence
  rules.
* `tests/workflows/test_database_simplification_stage9.py` covers exchange
  session expansion, insufficient history, provider-filled target coverage,
  fileless completion reuse, correction revision changes, and pending-range
  read exclusion.

### Decisions and boundaries

The readiness workflow is the only Stage 9 orchestration layer allowed to
request missing target coverage. Feature/query repositories remain read-only;
the existing Stage 8 coordinator owns source preparation, pending scopes,
raw/derived rebuilds, recovery, and completion. The existing close/open/target
feature definitions are passed through without generalized SQL vectors,
volume features, new z-score families, or model-architecture changes.

Session dependencies use exchange-session adjacency rather than calendar-day
arithmetic and never create a ticker/date Cartesian product. Strict reads use
the decision cutoff; retrospective reads require an explicit data snapshot.
Completed raw sessions are provider/file independent, and a pending operation
overlapping any requested date blocks close and liquidity reads inside the
reader transaction. Interpolated and bad observations remain excluded by the
default clean quality policy. Result revisions include the target completion
revision so corrections cannot silently reuse an older recommendation/sample
identity; saved artifacts are not automatically retrained.

### Verification

* **Passed:** `.venv\Scripts\python.exe -m pytest
  tests\workflows\test_database_simplification_stage9.py -q` — 5 tests.
* **Passed:** focused Stage 8, Stage 9, training, recommendation, and
  readiness regressions — 52 tests, with 12 existing pandas deprecation
  warnings.
* **Passed:** affected Python directories —
  `.venv\Scripts\python.exe -m pytest tests\workflows tests\training
  tests\recommendations tests\database -q` — 327 passed, 1 skipped, with 52
  existing pandas deprecation warnings.
* **Passed:** CLI integration suite — `.venv\Scripts\python.exe -m pytest
  tests\cli -q` — 54 passed, with 24 existing pandas deprecation warnings.
* **Passed:** affected mypy check over the 10 changed/new Python source
  surfaces; no issues reported.
* **Passed:** Python compilation for the new readiness/training modules and
  modified target frontends.
* No disposable-MySQL Stage 9 run was performed; the new acceptance fixture
  uses the existing SQLite-compatible fake database. No live provider call or
  production database write was made; provider behavior was injected through
  the fake fixture.

### Limitations and next-stage prerequisites

The target frontend integration is Python-only and relies on the Stage 8
target runtime and existing model/feature services. The full repository guard
continues to have the previously documented legacy z-score documentation
failure outside this stage's changed files; the affected Stage 9 directories
passed. Stage 10 may now remove retired operational storage only after its
own scope is explicitly requested; no Stage 10 cleanup was included here.

## Stage 10 handoff — 2026-10-26

Stage 10 is complete. This stage performed source cleanup and isolation only;
it did not drop tables, migrate data, modify a production database, or
implement any later stage.

### Implemented boundary

* The target registry now exposes `target_tables()` for active readers and
  writers. The historical `tables()` serialization remains available for
  legacy manifests and regression fixtures. The target registry digest uses
  only target roles, and the target registry version is `2`.
* Fresh `initialize_simplified_schema` creation emits only the selected target
  schema. It rejects retired operational tables beside a target schema, and
  the target reader/writer guard rejects mixed target databases.
* Legacy schema creation, verification, z-score initialization and assertions,
  and legacy daily-maintenance entry are explicitly rejected when the target
  schema marker is present. Legacy archive consolidation and reimport also
  fail before reading or writing retired tables against the target schema.
* `preview_schema` is target-only. Obsolete archive, sample-verification,
  comparison, consolidation, and legacy-migration console scripts were
  removed from packaging. Historical Python/Java migration and benchmark
  modules and fixtures remain importable only as disposable evidence and
  regression support; they are labeled as historical and are not current
  operator instructions.
* Maintained README, operations, frontend, storage, maintenance,
  configuration, and validation documentation now points at the target
  setup/readiness path or labels the old schema and commands as historical.
  `docs/archive/README.md` records that archived documents preserve evidence,
  not supported procedures.

### Retired-name classification

Remaining references were searched across source, Java, tests, tools, and
documentation. They are classified as follows:

* **Migration/performance input:** `src/stockguidance/legacy_migration`,
  `src/stockguidance/performance`, Java `migration`/`validation`, and their
  tests and archived benchmark reports. These are disposable historical
  inputs, not target runtime dependencies.
* **Legacy fixtures:** old schema initializers, registry serialization,
  archive/consolidation models, and regression fixtures retained to verify
  historical behavior. Target guards prevent them from operating on the
  versioned schema.
* **Historical documentation:** `docs/archive/**` and the explicitly marked
  historical sections of storage, maintenance, and validation documentation.
* **Necessary repair metadata:** the target `repair_metadata` table retains
  structured repair evidence; no retired quality table was recreated under a
  new name.
* **Unintended live dependency:** none found in the target schema/runtime,
  target workflows, maintained target CLI path, or active target registry.

### Acceptance checks

* Passed Stage 10 and affected Stage 3/8/9 tests: `49 passed`.
* Passed affected Python suites:
  `.venv\Scripts\python.exe -m pytest tests\workflows tests\training
  tests\recommendations tests\database tests\cli -q` — `387 passed,
  1 skipped`; 76 existing pandas deprecation warnings.
* Passed Java Maven tests: `mvn -q -f java\pom.xml test`.
* Passed mypy over the changed database, target CLI, legacy isolation, and
  maintenance surfaces: `Success: no issues found in 43 source files`.
* Passed `git diff --check`.
* Fresh SQLite-compatible target initialization and mixed-schema rejection
  tests confirm that no retired operational table is created in the target
  schema. No disposable MySQL Stage 10 run was performed, and no live
  provider or production database was accessed.

The separately exercised historical migration/archive fixture suite remains
outside this acceptance boundary and still contains pre-existing audit/archive
read failures in untouched legacy fixture code. Those fixtures were retained
as required and were not repaired as part of Stage 10.

## Stage 11 handoff — 2026-10-26

Status: complete for the Stage 11 recovery and regression implementation. The
acceptance runner used only the explicitly prefixed disposable MySQL schema
`stockguidance_disposable_stage11_20261026`; it did not select or modify
`stockforecasting`, download market data, print credentials, or create a
commit. Stage 12 was not implemented.

### Changed files and interfaces

* `tools/database_simplification_stage11.py` adds a reproducible disposable
  MySQL acceptance runner. It seeds a synthetic target schema, starts worker
  subprocesses, kills them after already-committed clear/delta/z-score
  transactions, restarts against the same immutable pending scope, and
  verifies reader guards, advisory-lock waiting, queued recovery, unrelated
  rows, numerical values, quality, completion metadata, future-dependent
  labels, and pending-row cleanup. It always drops its prefixed schema in the
  parent cleanup path.
* `tests/database/test_database_simplification_stage11.py` adds focused
  coverage for reporting already durable calculation units after restart and
  parameterized writer-lock wait timeouts.
* `src/stockguidance/database/numerical_rebuild.py` now reports a delta or
  z-score work unit as committed when its persisted completion evidence is
  found during resume. This lets the coordinator verify the entire immutable
  scope after a process restart; it does not recalculate or clear a completed
  unit.
* `src/stockguidance/database/operation_recovery.py` gives the simplified
  coordinator a 60-second default advisory-lock wait, with validation and an
  explicit `lock_timeout_seconds` override.
* `src/stockguidance/workflows/market_lock.py` accepts a non-negative timeout
  and binds the lock name and timeout as SQL parameters. The legacy/default
  direct lock behavior remains immediate (`timeout_seconds=0`); the target
  coordinator opts into waiting so a concurrent request is planned only after
  active recovery completes.

### Acceptance evidence

The disposable MySQL runner passed all of these process boundaries:

* kill after the first clear chunk commit: `affected_rows_cleared` remained
  false, the committed chunk was durable, the next chunk was untouched, the
  scope was unchanged, and restart cleared only its persisted scope;
* kill after delta commit: an overlapping reader was distinguished as
  unfinished, an unrelated read remained available, a second writer waited on
  the advisory lock, and its coordinator resumed the active operation before
  accepting its next empty request;
* kill after z-score commit: both calculation units and completion evidence
  survived, and restart preserved their values, states, and quality without a
  second write.

The runner also verified the final delta value `1.25` with explicit
`not_yet_available` future labels, ready price z-score `1.0`, zero quality flags,
raw/delta/z-score completion, pending cleanup, and preservation of unrelated
raw data. It emitted a machine-readable completion record and dropped the
disposable schema during cleanup:

`.venv\Scripts\python.exe tools\database_simplification_stage11.py --dbinfo-file replacement-dbinfo.id --schema stockguidance_disposable_stage11_20261026`

Additional checks:

* **Passed:** `.venv\Scripts\python.exe -m pytest tests\database\test_database_simplification_stage11.py -q` — 2 tests.
* **Passed:** focused recovery/numerical/cutover regression selection — 37
  tests, including the Stage 11 tests.
* **Passed:** `.venv\Scripts\python.exe -m mypy src\stockguidance\database\numerical_rebuild.py src\stockguidance\database\operation_recovery.py src\stockguidance\workflows\market_lock.py tools\database_simplification_stage11.py` — no issues in 4 files.
* **Passed:** `mvn -q -f java\pom.xml test` — 30 tests. Existing JVM warnings
  and the expected credential-safe connection-failure trace remain.
* **Passed:** Python compilation of the changed implementation and runner.
* **Passed:** `git diff --check` for tracked changes; final explicit whitespace
  inspection covers new files below.

### Required full-suite gates and limitations

The full Python command
`.venv\Scripts\python.exe -m pytest tests -q --tb=short` produced **1,057
passed, 3 skipped, and 9 failed**. The nine failures are outside this Stage 11
change: three legacy archive-cleanup tests stop at the pre-existing Stage 10
target-schema rejection/connection seam, and six retained historical
archive-audit/reading fixtures have the legacy `UnboundLocalError` failures
already recorded in the Stage 10 handoff. No Stage 11-targeted test failed.

The configured full `.venv\Scripts\python.exe -m mypy` command remains
**incomplete** with 237 existing errors in 21 historical performance/CLI
files, including unavailable optional `streamlit`/`psutil` stubs. The focused
Stage 11 surfaces above pass; dependencies were not installed and the full
mypy errors were not reclassified as Stage 11 regressions.

The Stage 11 MySQL evidence is disposable and synthetic, so it does not prove
production capacity or authorize migration. SQLite tests do not replace the
MySQL process/concurrency evidence. Empty, missing, invalid, interpolated,
holiday-window, source-mismatch, and final-label behavior remain covered by
the completed Stage 3–10 focused suites; no production schema or runtime
workflow was changed to broaden those behaviors in this stage.

Stage 12 remains the separate operator migration preparation/rehearsal stage.
No production migration is ready to apply based on this handoff alone.
