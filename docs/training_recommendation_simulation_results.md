# Training, recommendation, and simulation implementation handoffs

## Stage 0 — accepted contracts and gap map

Status: complete for Stage 0 documentation, owner consultation, and repository
inspection. No production database, provider, model training, schema migration,
trade submission, or commit was performed.

### Changed files

- docs/training_recommendation_simulation_contract.md — accepted product
  decisions, versioned typed contracts, v1 compatibility, sanitized v2 job
  examples, end-to-end identity flow, implementation gap map, database/SQL
  boundary, and deferred future-model work.
- docs/training_recommendation_simulation_results.md — this handoff.

The pre-existing user changes in dbinfo.id,
src/stockguidance/database/migration_sql.py,
tests/database/test_migration_sql.py, and training-job.json were preserved.
The untracked authoritative prompt was also preserved unchanged. No commit was
created.

### Accepted decisions

- Training membership is complete across training dates and required feature /
  label dependencies, without future-survival selection; the universe is frozen
  before simulation.
- Target choices are raw close-to-close fractional return, per-ticker rolling
  return z-score with 20/60-session windows (default 20), and same-day
  cross-sectional return z-score. Existing stored per-ticker price/delta
  z-scores remain separate input options.
- Relative ago defaults to the last completed available source session, with
  explicit today; signed date durations use positive-forward and
  negative-ending-at-anchor semantics.
- Input/output normalization independently supports none, population
  standardization, and min-max scaling; default is none.
- Volume/volatility filters apply only to frozen training-universe selection and
  are not rechecked for recommendations. The legacy transaction-count rule is
  separate.
- Reports contain eligible-universe and returned top-n medians.
- rescore reuses an artifact without retraining and creates a child run; rerun
  is a fresh run with explicit artifact-reuse versus retraining; replay is
  reserved for exact offline portfolio/accounting replay.
- Defaults are recommendation top-n 5, simulation top-n 1, 10 exchange-session
  simulation horizon, 1000.00 initial capital, equal allocation, fractional
  shares, existing accounting behavior, and fractional raw-return units.

### Repository findings

The inspected current implementation provides reusable typed foundations:

- features.specification, datasets.building, datasets.training, and verified
  close repositories preserve explicit time/ticker/feature layout, availability
  cutoffs, stable ticker IDs, and shared training/inference sample construction.
- targets.specification and targets.construction currently implement only raw
  close(T+1)/close(T)-1 targets.
- training.engine currently fits/evaluates L1/MAE; models.specification and
  models.factory currently use one hidden activation and require final activation
  none.
- workflows.simplified_readiness provides target-schema completion,
  pending-operation guards, source/revision evidence, and read-only
  close/liquidity repositories, but simplified_training still constructs
  TargetSpec().
- artifacts/* provides strict standalone stockguidance-inference-1 model
  archives; recommendations/* provides transaction-count eligibility and
  deterministic ranking; simulation/* and portfolio/* provide both cash-rest
  schedules and Decimal accounting.
- reporting/* and viewers/* load explicit v1 report files. There is no shared
  saved-run service or runs-root viewer yet.
- The active simplified database contract/results establish the selected target
  registry, schema/version guard, completion/readiness states, pending scope,
  writer lock, correction closure, and resumable SQL migration boundary.

### Verification

Commands run during Stage 0:

1. Get-ChildItem -Recurse -Filter AGENTS.md -File and
   rg --files -g AGENTS.md — no applicable AGENTS.md found.
2. git status --short and git diff --stat — confirmed the four existing
   user-modified files and the untracked authoritative prompt; no user change
   was reset or overwritten.
3. Complete reads of the authoritative prompt, current close-sample/training/
   recommendation/simulation documentation, database simplification contract
   and results, and the implementation surfaces listed in the Stage 0 brief.
4. .venv\Scripts\python.exe -m pytest tests\documentation -q — passed, 7
   tests in 0.93s.
5. .venv\Scripts\python.exe -m mypy src\stockguidance\features
   src\stockguidance\targets src\stockguidance\training
   src\stockguidance\models src\stockguidance\recommendations
   src\stockguidance\simulation src\stockguidance\reporting — passed, no
   issues in 63 source files. No source implementation changed in Stage 0.
6. git diff --check — passed; Git emitted only existing LF/CRLF conversion
   warnings for user-modified files. The explicit trailing-whitespace check for
   both new documents also passed.

### Limitations and next-stage prerequisites

Stage 0 does not implement date resolution, saved runs, new feature channels,
universe selection, target transformations, fitted scalers, new losses/model
controls, training orchestration, recommendation changes, simulation
recomputation, viewer changes, or standalone preparation tools. Those remain
ordered prerequisites for Stages 1–12.

Stage 1 must implement the run-directory/manifest API before later workflows can
persist attempts. Stage 2 must implement the typed date/range resolver before
training or evaluation can use relative expressions. Stage 3 must implement the
standalone bounded database operation and SQL-rendering API before volume
preparation or later database additions. No live database or provider action is
authorized by this handoff.

## Stage 1 — saved-run foundation

Status: complete for the shared saved-run API, filesystem service, validation,
tests, and documentation. No database, provider, Torch, model, schema
migration, live-data refresh, trade submission, or commit was performed.

### Changed interfaces

- `stockguidance.runs.RunConfig` validates typed operations and stores a
  JSON-safe sanitized settings mapping.
- `stockguidance.runs.RunManifest` implements the versioned
  `stockguidance-run-1` manifest with original/resolved configuration,
  lifecycle status, report state, provenance, reserved future sections,
  artifact references, results placeholders, and failure/incomplete-report
  records.
- `stockguidance.runs.RunDirectoryService` creates collision-free run
  directories, atomically transitions status, verifies parent/child links,
  lists invalid directories without reading application data, validates local
  content-addressed artifact/checkpoint files, and records external hash-bound
  references.
- `stockguidance.cli.runs` provides database-free `list`, `read`, and
  `validate` operations. `--runs-root` is the explicit root override; the
  default is `<project>/runs`, which is Git-ignored.

### Accepted decisions and compatibility

- The manifest schema is `stockguidance-run-1`; process states are pending,
  running, completed, and failed.
- Incomplete simulation reporting is independent of process failure: a
  completed simulation may have `report_state="incomplete"` with explicit
  incomplete details, while a process failure has `status="failed"` and a
  sanitized failure record.
- Parent runs are immutable. Child operations use `parent_run_id`; creation
  validates the parent without modifying it.
- Original settings are sanitized recursively. Credential-shaped keys and
  credential-style `key=value` text are redacted; date/time/decimal/path
  values use explicit JSON type tags. Manifests never contain credential file
  contents or unrelated dirty-tree contents.
- Artifact and checkpoint payloads remain separate from the manifest. Local
  references are relative to the run directory and SHA256-validated; external
  references are explicit and also require a digest.
- No predictions, migrations, reports, or successful workflow results are
  fabricated by run creation. Reserved result fields remain null until later
  stages populate them.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\runs -q` — passed, 8 tests.
   Coverage includes sanitized round-trip, concurrent/same-time creation,
   explicit collision rejection, atomic lifecycle transitions, failure
   metadata, incomplete simulation state, parent immutability, corrupt and
   incomplete manifests, Windows-safe paths, CLI inspection/help, and artifact
   hash validation.
2. `.venv\Scripts\python.exe -m mypy src\stockguidance\runs src\stockguidance\cli\runs.py` — passed with no issues in 4 source files.
3. `.venv\Scripts\python.exe -m py_compile src\stockguidance\runs\contracts.py src\stockguidance\runs\service.py src\stockguidance\cli\runs.py` — passed.
4. `.venv\Scripts\python.exe -m pytest tests\runs tests\architecture\test_package_imports.py tests\architecture\test_boundaries_and_viewers.py -q` — passed, 17 tests in 15.15s, with 8 pre-existing pandas timezone deprecation warnings.
5. `.venv\Scripts\python.exe -m pytest tests\documentation -q` — passed, 7 tests in 2.12s.
6. `git diff --check` — passed. Git emitted only existing LF/CRLF conversion warnings for user-modified files.

Exact standalone inspection commands:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs list
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs read <run-id>
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs validate <run-id>
```

These commands are read-only and do not initialize or migrate a database.

### Remaining limitations and next-stage prerequisites

Stage 1 does not resolve dates, select universes, build new features, fit
models, generate predictions, implement recommendation/simulation/replay
orchestration, inspect reports in viewers, or implement database preparation
and SQL rendering. It provides only the persistence boundary those stages
will use.

Stage 2 must add the typed date/range resolver and dependency evidence. Later
stages must populate resolved configuration, identity, specification, result,
report, trade, and equity sections through this service rather than creating a
second run store. Stage 3 remains the prerequisite for standalone bounded
database preparation and SQL debugging.

## Stage 2 — structured date ranges and dependency planning

Status: complete for the shared typed resolver, source-coverage evidence,
dependency planner, Stage 1 persistence hook, preview CLI, tests, and
documentation. No database, provider, model, Torch, schema migration, live
data refresh, trade submission, or commit was performed.

### Changed interfaces

- `stockguidance.calendar.ranges` adds the versioned
  `stockguidance-date-resolution-1` contract: `AbsoluteDate`, `RelativeDate`,
  `ExplicitRange`, `AnchorDuration`, typed calendar/session units, and parsers
  for the accepted JSON shape.
- `SourceCoverageSnapshot` and `CoverageObservation` carry source identity,
  snapshot/revision identity, verified/absent/unknown/incomplete session states,
  and optional availability evidence. Every resolved session and planned
  dependency must be verified; known absence and unresolved coverage fail with
  separate errors.
- `DateRangeResolver` injects a `SessionCalendar`, coverage snapshot, source,
  and clock. It uses exchange-calendar chronology, inclusive boundary snapping,
  signed calendar/session durations, leap-year clamping, source-backed anchors,
  and persisted anchor evidence.
- `DependencySpec` and `DependencyPlan` keep model lookback, rolling z-score
  window, price-delta predecessor, target baseline, label horizon, training-end
  gap, and simulation horizon independent. The plan records training/evaluation
  entries, warm-up sessions, label exits, information/decision cutoffs, and the
  final evaluation session without requiring an eleventh simulation close.
- `persist_resolution` writes the resolved mapping through Stage 1's reserved
  `resolution` section and `resolved_configuration`; it does not create a
  second run store. `resolve_rerun` returns the saved object unchanged for
  pinned reruns and only resolves relative expressions for explicit
  `RerunResolutionMode.RERESOLVE`.
- `stockguidance.cli.preview_date_ranges` and the `preview_date_ranges` script
  provide database/provider/model-free JSON preview. The checked-in fixture is
  `examples/stage2/date-preview.json`; usage is documented in
  `docs/date_ranges.md`.

### Accepted decisions and semantics

- Absolute and relative expressions follow the Stage 0 accepted names and
  units. Calendar days/years are not approximated by session counts; exchange
  session durations count exactly N inclusive sessions.
- Start dates snap forward and end dates snap backward only within the
  verified source/calendar scope. Requested ranges that extend beyond verified
  source coverage fail instead of succeeding after an accidental boundary
  snap.
- `first_data` and `last_data` come from verified snapshot sessions. The
  default `last_completed_available` candidate is the latest verified session
  closed and available by the injected clock; `today` is the injected clock's
  date in the calendar timezone.
- Automatic evaluation begins after the final mature training label plus the
  configured training-end gap. The default generated evaluation range is the
  configured simulation horizon; the final evaluation entry is represented as
  an open-position boundary and has no required next-session exit dependency.

### Verification commands and actual results

1. `Get-ChildItem -Force; git status --short; rg --files -g AGENTS.md` — no
   applicable `AGENTS.md`; existing user changes in `.gitignore`, `dbinfo.id`,
   `pyproject.toml`, `src/stockguidance/database/migration_sql.py`,
   `tests/database/test_migration_sql.py`, and `training-job.json` were
   preserved. Existing Stage 1 untracked files were also preserved.
2. `.venv\Scripts\python.exe -m pytest tests\exchange_calendar\test_date_ranges.py -q`
   — passed, 14 tests; 20 existing pandas timezone deprecation warnings.
3. `.venv\Scripts\python.exe -m pytest tests\exchange_calendar tests\runs tests\documentation -q`
   — passed, 36 tests; 28 existing pandas timezone deprecation warnings.
4. `.venv\Scripts\python.exe -m pytest tests\architecture\test_package_imports.py tests\architecture\test_boundaries_and_viewers.py -q`
   — passed, 10 tests; 8 existing pandas timezone deprecation warnings.
5. `.venv\Scripts\python.exe -m mypy src\stockguidance\calendar src\stockguidance\cli\preview_date_ranges.py`
   — passed with no issues in 10 source files.
6. `.venv\Scripts\python.exe -m py_compile src\stockguidance\calendar\ranges.py src\stockguidance\cli\preview_date_ranges.py`
   — passed.
7. `.venv\Scripts\python.exe -m stockguidance.cli.preview_date_ranges --config examples\stage2\date-preview.json`
   — passed with `stockguidance-date-preview-1`; output resolved the fixture
   training range, one-session feature warm-up, mature label exits, and three
   later evaluation sessions without a database or model.
8. `.venv\Scripts\python.exe -m pytest -q -x --tb=short` — stopped at the
   first broader repository failure after 74 passed: existing
   `tests/cli/test_market_data_update.py::test_unknown_history_boundary_has_no_network_or_database_side_effects`
   expected `supported-history`, while the current updater emitted its existing
   generic setup/storage error. The new Stage 2 files are not imported by that
   updater path. An unrestricted `pytest -q` was stopped after the same broad
   run accumulated additional failures and spent several minutes in existing
   performance/integration tests.
9. `git diff --check` — passed; Git emitted only existing LF/CRLF conversion
   warnings for user-modified files.

### Limitations and next-stage prerequisites

Stage 2 does not select tickers, add feature channels, prepare volume data,
construct transformed targets, fit scalers, change models/losses, train,
recommend, simulate, inspect reports, or perform database/SQL operations.
Coverage snapshots are supplied by callers; no database adapter or provider
refresh was added. The preview CLI uses an explicit fixture snapshot rather
than discovering source coverage.

Stage 3 remains the next stage and must add the standalone bounded database
preparation/SQL-rendering lifecycle. Later training and evaluation workflows
must use this resolver and persist its resolution through Stage 1 before
universe selection or model construction. The unrelated existing updater
failure above remains outside Stage 2's scope.

## Stage 3 — standalone preparation framework and additive schema

Status: complete for the bounded preparation API, additive volume z-score
schema/registry, SQL rendering, CLI lifecycle, disposable MySQL rehearsal,
tests, and operator documentation. No production database, provider refresh,
live data download, trade submission, credential output, or commit was
performed.

### Changed interfaces

- `TableNames` and the shared Python/Java registry accept the additive
  `preparation_templates.volume` template and resolve a third per-window
  `volume` table. The target registry digest is version 3. Existing target
  databases are incompatible until an explicitly authorized setup/migration;
  no read path silently extends a schema.
- `ZScoreFamily.VOLUME`, `ZScoreSpecification`, and
  `ZScoreSeriesDefinition` carry the explicit volume family. Existing
  price/price-delta repository routes remain unchanged; the legacy MySQL
  z-score repository rejects volume reads/publication so volume cannot be
  mistaken for price delta.
- `stockguidance.database.preparation` provides typed volume requests/plans,
  dependency-session planning, typed input/result documents, pure calculation,
  bounded select/export, pending-operation apply/upsert, validation, atomic
  manifest storage, status, and resumable interruption handling.
- `stockguidance.cli.prepare_database` exposes `plan`, `create`,
  `render-sql`, `select`, `calculate`, `apply`, `validate`, `status`, and
  `resume`. Operation artifacts live below the configured operations root and
  include sanitized failure state. Custom table-name files are honored.
- `tools/database_preparation_stage3_mysql.py` is a disposable MySQL
  rehearsal runner. It keeps credentials in memory, creates only an explicitly
  prefixed disposable database, and drops that database in `finally`.

### Accepted decisions and semantics

- Existing price/delta materializations are reused. Stage 3 adds only the
  volume z-score family because the accepted later feature contract requires
  optional volume z-scores. Targets and experiment scaler state remain in code
  or run artifacts; no experiment-specific database columns were added.
- Volume uses the existing rolling population z-score kernel, current-value
  inclusive windows, float32-compatible ready scores, NULL unready scores,
  existing status precedence, and explicit dependency/availability digests.
  A request writes only its bounded date/ticker/window scope and requires
  `W-1` warm-up sessions.
- SQL rendering is read-only for plan/select/calculate/validate and emits
  quoted registry identifiers, result columns/types/order, dependency scope,
  bounded inputs, and an EXPLAIN path. Python is the authoritative reviewed
  calculation; the rendered MySQL window query is a debug/calculation route,
  not a hidden second feature algorithm. Apply SQL explicitly requires the
  lifecycle wrapper.
- Apply reselects the exact input scope and rejects a changed source digest
  before registering work. Pending clear/rebuild units use the existing
  maintenance lock, readiness guard, correction propagation, stable-key
  upserts, completion metadata, and durable interrupted state.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\database -q` — passed, 149
   tests in 16.10s, with 4 existing pandas timezone deprecation warnings.
2. `.venv\Scripts\python.exe -m pytest tests\documentation -q` — passed, 7
   tests.
3. `.venv\Scripts\python.exe -m pytest tests\architecture -q` — passed, 12
   tests in 15.04s, with 8 existing pandas timezone deprecation warnings.
4. `.venv\Scripts\python.exe -m pytest tests\zscores tests\database\test_preparation.py tests\documentation -q` — passed, 51 tests in 18.22s, with 12 existing warnings.
5. `.venv\Scripts\python.exe -m mypy src\stockguidance\database\repositories\mysql_zscores.py src\stockguidance\database\preparation.py src\stockguidance\cli\prepare_database.py src\stockguidance\database\table_names.py` — passed with no issues in 4 source files. The MySQL rehearsal tool separately passed `mypy` with `MYPYPATH=src` and no issues in 1 file.
6. `.venv\Scripts\python.exe -m py_compile tools\database_preparation_stage3_mysql.py src\stockguidance\database\preparation.py src\stockguidance\cli\prepare_database.py` — passed.
7. `mvn -q -f java\pom.xml test` — passed with exit code 0. Maven/JDK emitted the existing restricted-access, Unsafe, vector-module, and credential-safe invalid-host test diagnostics.
8. `.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --help` — passed. `.venv\Scripts\python.exe -m stockguidance.cli.preview_schema --windows 2` — passed and emitted the volume table/template and registry version-3 DDL.
9. `.venv\Scripts\python.exe tools\database_preparation_stage3_mysql.py --dbinfo-file replacement-dbinfo.id --prefix stockguidance_disposable_preparation_20261029_03 --output-dir (Join-Path ([System.IO.Path]::GetTempPath()) 'stockguidance-stage3-mysql-03')` — passed. The disposable MySQL report verified custom registry names, 3 dependency rows, 2 result rows, SQL/code parity, source-change rejection, crash/resume, rerun, validation, EXPLAIN availability, and 2.293 ms calculation timing; the database was dropped afterward.
10. `git diff --check` — passed. Git emitted only LF/CRLF conversion warnings; no whitespace errors were reported.

### Standalone operator commands

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step plan
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step create
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step select --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step calculate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step render-sql --operation-id <operation-id> --sql-step calculate --sql-output output\stage3-calculate.sql
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step apply --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step validate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step resume --operation-id <operation-id>
.venv\Scripts\python.exe tools\database_preparation_stage3_mysql.py --dbinfo-file replacement-dbinfo.id --prefix stockguidance_disposable_preparation_YYYYMMDD_01 --output-dir (Join-Path ([System.IO.Path]::GetTempPath()) 'stockguidance-stage3-mysql')
```

### Remaining limitations and next-stage prerequisites

The checked-in SQL route is a rendered MySQL inspection/calculation route;
production operators must review and execute it separately if they choose it.
The Python apply route is the complete lifecycle-safe implementation. The
rehearsal uses a small deterministic fixture and is not evidence of training
quality or provider coverage. No production schema setup or migration is
authorized by this handoff.

Stage 4 is the next prerequisite: it must use Stage 2 resolved ranges and
Stage 3 bounded preparation/readiness to implement deterministic universe
selection, including optional volume/volatility filters, exclusions, stable
ticker ordering, and selection evidence. Stage 5 may then connect the
prepared volume channels and existing price/delta families to shared samples;
it must not duplicate the preparation lifecycle or introduce stock-descriptor
vectors, variable ticker ordering, embeddings, permutation-invariant models,
or transformer paths.

