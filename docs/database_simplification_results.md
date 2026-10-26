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
