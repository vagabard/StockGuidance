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

## Stage 4 — frozen universe selection and optional filters

Status: complete for the typed frozen-universe selector, simplified-database
read adapter, read-only SQL rendering/inspection CLI, Stage 1 evidence hook,
tests, and operator documentation. No production database, provider refresh,
schema migration, model construction, trade submission, credential output, or
commit was performed.

### Changed interfaces

- `stockguidance.universe.selection` adds the versioned
  `stockguidance-universe-selection-1` contracts: `UniverseFilters`,
  `UniverseSelectionRequest`, `UniverseSourceData`, typed ticker/session/raw
  evidence, `UniverseSelectionResult`, per-ticker `TickerExclusion`, and
  filter measurements.
- `UniverseSelectionRequest.from_dependency_plan(...)` derives membership,
  Stage 2 warm-up/label dependencies, and training-only filter sessions. Average
  volume uses exactly `W` trailing sessions; volatility uses `W + 1` closes for
  `W` predecessor-backed returns. Evaluation/simulation sessions are not read.
- `assess_universe` blocks unknown/incomplete whole-session coverage, records
  verified absence and per-ticker raw gaps separately, applies enabled filters
  with inclusive bounds, and uses population volatility. `select_universe`
  raises `EmptyUniverseError` with its complete exclusion result before any
  model/network construction. Stable-ID ordering and validated explicit ordered
  subsets are both supported.
- `persist_universe_evidence` and `persist_universe_failure_evidence` update the
  Stage 1 `universe` and `results.exclusions` sections; no second run store was
  added.
- `stockguidance.database.repositories.universe.MySQLUniverseRepository` reads
  registry-selected ticker/session/raw rows and simplified raw completion,
  revision, and information-cutoff evidence without creating or migrating
  schema. Optional volume and transaction columns are omitted when their
  filters are disabled.
- `stockguidance.database.universe_selection` adds bounded
  `UniverseSqlScope`/`RenderedUniverseSql` output for `plan`, `select`, and
  `validate`. SQL is read-only, uses validated custom registry names, contains
  no Python `%s` placeholders, and clearly identifies Python as the reference
  calculation route.
- `stockguidance.cli.select_universe` and the `select_universe` project script
  render SQL without a database. `docs/universe_selection.md` and
  `examples/stage4/universe-selection-sql.json` provide the standalone commands
  and sanitized scope.

### Accepted decisions and semantics

- Training membership is selected from the Stage 2 training range plus its
  required feature/target warm-up and mature-label dependencies. Future
  simulation survival and evaluation observations do not select the default
  universe.
- `COMPLETE` whole-session evidence is required. Verified `MISSING` sessions
  become explicit exclusions; `INCOMPLETE`, `INCONSISTENT`, `UNVERIFIED`, and
  `NOT_YET_AVAILABLE` whole-session states block selection rather than silently
  shrinking the candidate set.
- Average daily volume is the arithmetic mean of shares. Volatility is the
  population standard deviation of fractional close-to-close returns and is not
  annualized. Every enabled filter requires a positive window and at least one
  nonnegative bound; lower/upper comparisons are inclusive.
- `legacy_transactions` remains a separately named training-cutoff count filter
  and is never treated as volume. Disabled filters authorize no optional
  dependency reads.
- Automatic selection sorts stable IDs and stores symbol labels separately.
  Explicit ordered subsets preserve their requested order only after identity,
  coverage, and filter validation. IDs remain identities, not tensor positions
  or descriptors.

### Verification commands and actual results

1. `git status --short` before authoring — clean; all Stage 4 changes are new
   scoped files plus the `select_universe` entry in `pyproject.toml`. No existing
   user changes were reset or incorporated.
2. `.venv\Scripts\python.exe -m pytest tests\universe tests\database\test_universe_selection_sql.py -q` — passed, 13 tests in 2.14s, 4 existing pandas timezone warnings.
3. `.venv\Scripts\python.exe -m pytest tests\universe tests\database\test_universe_selection_sql.py tests\runs tests\exchange_calendar\test_date_ranges.py tests\documentation -q` — passed, 42 tests in 16.85s, 24 existing pandas timezone warnings.
4. `.venv\Scripts\python.exe -m pytest tests\architecture tests\recommendations tests\database\test_registry_consumers.py tests\workflows\test_database_simplification_stage9.py -q` — passed, 127 tests in 72.91s, 20 existing pandas timezone warnings.
5. `.venv\Scripts\python.exe -m mypy src\stockguidance\universe src\stockguidance\database\repositories\universe.py src\stockguidance\database\universe_selection.py src\stockguidance\cli\select_universe.py` — passed with no issues in 5 source files.
6. `.venv\Scripts\python.exe -m py_compile src\stockguidance\universe\selection.py src\stockguidance\universe\__init__.py src\stockguidance\database\repositories\universe.py src\stockguidance\database\universe_selection.py src\stockguidance\cli\select_universe.py` — passed.
7. `.venv\Scripts\python.exe -m pytest tests\database -q` — passed, 151 tests in 15.89s, 4 existing pandas timezone warnings.
8. `.venv\Scripts\python.exe -m stockguidance.cli.select_universe --help` — passed without database/provider side effects.
9. `.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step select --sql-output output\stage4-universe-select.sql` — passed and rendered bounded read-only SQL using the configured registry names.
10. `.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step validate --sql-output output\stage4-universe-verify.sql` — passed and rendered verification SELECTs.
11. `git diff --check` — passed; Git emitted only the existing LF/CRLF conversion warning for `pyproject.toml`.

The Stage 4 SQL scripts were rendered only; no live or disposable MySQL
execution was performed for this stage. SQLite was not used as evidence for
MySQL behavior. The typed adapter is covered by strict typing and scoped query
construction; a disposable MySQL read rehearsal remains an operator-level
follow-up if database execution of this new read path is desired.

### Remaining limitations and next-stage prerequisites

Stage 4 does not connect selected channels to shared samples, fit scalers,
construct targets, train models, recommend, simulate, or change artifacts.
Selection evidence is ready for Stage 1 manifests, but later training
orchestration must supply the resolved Stage 2 plan and invoke the Stage 4
adapter/selector before deriving model dimensions. Stage 5 is next: it must
consume this frozen ordered universe and connect existing price/delta plus
prepared volume channels to samples, preserving exact axes and identity maps.
It must not add descriptors, embeddings, variable ticker ordering, permutation
handling, or new transformer paths.

Standalone SQL debugging commands:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step plan --sql-output output\stage4-universe-plan.sql
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step select --sql-output output\stage4-universe-select.sql
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step validate --sql-output output\stage4-universe-verify.sql
```

## Stage 5 handoff — configured channels and shared samples

### Status

Stage 5 is complete. Configured ordered feature channels now have a typed
repository/read boundary, exact dependency and warm-up planning, shared sample
construction, readiness evidence, diagnostics, and read-only simplified-DB
consumption. Existing v1 close and entry-open feature behavior remains
unchanged.

### Implemented

- Added `FeatureSpecV2` with stable ticker identity order, explicit
  `session,ticker,feature` axes, and versioned channels for relative close,
  log close, price z-scores at 20/60, price-delta z-scores at 20/60, raw
  volume, volume z-scores at 20/60, and entry-open return.
- Added typed `FeatureChannelQuery`, `FeatureChannelObservation`,
  `FeatureReadinessEvidence`, and `FeatureChannelRepository` contracts.
  Repository data must cover the exact dependency/scored session and ticker
  keys; non-ready values remain unavailable rather than becoming zeroes.
- Added configured feature construction for price-only, delta-only,
  volume-only, mixed, and both-window specifications. The builder preserves
  population-z-score semantics and propagates missing, insufficient,
  unverified, late, invalid, zero-variance, stale, pending, and incomplete
  states as explicit issues.
- Connected feature dependency sessions to simplified readiness and shared
  sample construction. The read-only model/sample path requires injected
  prepared channel data; explicit preparation remains a separate operation.
- Added `MySQLFeatureChannelRepository`, which reads custom registry-selected
  tables and Stage 3 completion metadata through bounded read transactions,
  rejects overlapping unfinished operations, and does not create, repair, or
  migrate schema. Prepared z-score rows are accepted only with compatible
  series metadata, completion metadata, row coverage, and explicit statuses.
- Added channel/value/exclusion details to feature diagnostics and documented
  the contract in `docs/feature_channels.md`.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m py_compile src\stockguidance\features\specification.py src\stockguidance\features\channels.py src\stockguidance\features\construction.py src\stockguidance\features\history_window.py src\stockguidance\database\repositories\features.py src\stockguidance\datasets\building.py src\stockguidance\workflows\simplified_readiness.py tests\features\test_configured_channels.py` — passed.
2. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed; no issues in 302 source files.
3. `.venv\Scripts\python.exe -m pytest tests\features tests\datasets tests\training tests\history tests\diagnostics -q` — passed; 135 tests, 16 pandas timezone warnings, 18.90s.
4. `.venv\Scripts\python.exe -m pytest tests\database -q` — passed; 151 tests, 4 pandas timezone warnings, 14.98s.
5. `.venv\Scripts\python.exe -m pytest tests\documentation tests\architecture -q` — passed; 19 tests, 8 pandas timezone warnings, 16.15s.
6. `git diff --check` — passed; Git reported only LF/CRLF conversion warnings for modified text files.
7. `.venv\Scripts\python.exe -m pytest tests\features tests\workflows\test_database_simplification_stage9.py -q` — passed; 15 tests, 4 pandas timezone warnings, 3.60s.
8. `.venv\Scripts\python.exe -m pytest tests\documentation -q` — passed; 7 tests in 1.24s.
9. `.venv\Scripts\python.exe -m mypy src\stockguidance` after the final interface tightening — passed; no issues in 302 source files.

No live data was downloaded, no production database was modified, no schema
was migrated, and no trade was submitted. The Stage 3 volume preparation
commands remain standalone and rerunnable:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step plan
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step select --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step calculate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step apply --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step validate --operation-id <operation-id>
```

### Remaining limitations and prerequisites

Stage 5 does not add job parsing, target/scaler fitting, model-factory
support, artifact codecs, recommendations, historical simulation, saved runs,
or viewer orchestration. `training/data.py` deliberately rejects the new
feature specification until the later versioned artifact/model contract is
implemented. The MySQL adapter was not run against a live database in this
stage; a disposable read rehearsal remains an operator-level follow-up.

Stage 6 is next and must consume these exact shared samples for target and
scaler work. Future descriptor vectors, variable ticker ordering, embeddings,
permutation-invariant models, and new transformer paths remain out of scope.

## Stage 6 — targets and fitted input/output normalization

Status: complete for the versioned target contracts, target construction and
readiness propagation, job parsing, pure fitted preprocessing, dataset/cache/
checkpoint compatibility identities, focused tests, and documentation. No
production database, provider, live-data refresh, schema migration, model
artifact migration, trade submission, credential output, or commit was
performed.

### Changed interfaces

- `stockguidance.targets.specification.TargetSpec` remains the raw
  `raw_return-v1` contract. `TargetSpecV2` adds explicit raw v2, per-ticker
  return z-score (20/60-session), and same-day cross-sectional return z-score
  identities. `TargetSpecification` is the shared typed union.
- `construct_targets` computes raw close-to-close labels first and retains them
  in `TargetValues.raw`. It applies population-moment transforms to
  `TargetValues.transformed`, records outcome-only `TargetStatistics`, handles
  ties and one-ticker universes as zero transformed scores, and preserves
  explicit missing/unready issues.
- `build_sample` and `required_session_scope` carry per-ticker baseline close
  dependencies as label dependencies without turning them into training
  entries. `SimplifiedTrainingJob`, `TrainingJob`, `HistoryTrainingRequest`,
  and `inspect_features` now propagate the requested target instead of
  replacing it with `TargetSpec()`.
- `stockguidance.preprocessing` adds `PreprocessingSpec`,
  `FittedTransform`, `FittedPreprocessing`, `fit_preprocessing`, and
  `fit_training_preprocessing`. Input state is per flattened input coordinate;
  output state is one shared scalar. Standardization uses population variance,
  min-max does not clip, and constant dimensions map to zero. State has strict
  JSON-safe round-trip, feature/ticker/target identity validation, and inverse
  output scaling.
- `TrainingDataset.with_preprocessing` and `training_arrays` apply an already
  fitted state without refitting. Dataset revisions, v2 sample-cache keys, and
  v2 checkpoint identities include the preprocessing/target identities. v1
  dataset/cache/checkpoint identity payloads remain unchanged when no v2 state
  is present.
- `docs/target_preprocessing.md`, `docs/close_samples.md`, and
  `docs/model_training.md` document the formulas, axes, leakage boundary,
  serialization, and Stage 8 artifact limitation.

### Accepted decisions and semantics

- The Stage 0 accepted target choice is implemented as same-day
  cross-sectional next-session-return z-score using the full fixed universe;
  the separately accepted per-ticker rolling return z-score uses the prior
  mature 20 or 60 returns. Existing stored price/price-delta z-score channels
  remain input features and are not substituted for target transforms.
- Raw fractional returns remain available for accounting and reporting. A
  transformed target is not relabeled as a raw return. Cross-sectional and
  rolling moments are outcome metadata, never inference inputs.
- Normalization defaults to none and independently supports standard and
  min-max methods. Fitting is explicitly scoped to eligible training arrays;
  holdout/inference data only uses transform or inverse operations.
- Stable ticker IDs and declared feature order are required in fitted state;
  reordered or changed specifications cannot reuse it. No descriptors,
  embeddings, variable-order models, or transformer paths were introduced.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\targets tests\preprocessing tests\datasets tests\training tests\workflows\test_database_simplification_stage9.py tests\configuration tests\documentation -q` — passed, 154 tests, 8 existing pandas timezone warnings, 14.97s.
2. `.venv\Scripts\python.exe -m pytest tests\training\test_checkpoints.py tests\preprocessing tests\targets -q` — passed, 7 tests, 4 existing pandas timezone warnings, 2.06s; this includes v2 checkpoint identity binding.
3. `.venv\Scripts\python.exe -m pytest tests\architecture tests\artifacts tests\recommendations tests\simulation -q` — passed, 198 tests, 23 warnings (existing pandas timezone and Torch transformer warnings), 36.99s.
4. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed, no issues in 303 source files.
5. `.venv\Scripts\python.exe -m py_compile src\stockguidance\preprocessing.py src\stockguidance\targets\specification.py src\stockguidance\targets\construction.py src\stockguidance\datasets\building.py src\stockguidance\datasets\training.py src\stockguidance\training\jobs.py src\stockguidance\workflows\simplified_training.py src\stockguidance\cli\inspect_features.py` — passed.
6. `git diff --check` — passed; Git reported only the existing LF/CRLF conversion warnings and no whitespace errors.

No live or disposable MySQL rehearsal was needed for this stage: target
transforms and fitted state are code/artifact concerns, and no database schema
or target column was added. No provider requests or live data downloads were
made.

### Standalone commands

```powershell
.venv\Scripts\python.exe -m pytest tests\targets tests\preprocessing -q
.venv\Scripts\python.exe -m mypy src\stockguidance\targets src\stockguidance\preprocessing.py src\stockguidance\datasets src\stockguidance\workflows\simplified_readiness.py src\stockguidance\config\training.py src\stockguidance\training
```

### Remaining limitations and next-stage prerequisites

Stage 6 does not automatically fit preprocessing inside the end-to-end job
orchestration, change model/loss controls, or extend the standalone inference
artifact codec. `TrainingDataset` can carry a fitted state and the numerical
adapter applies it, but Stage 8 must bind target/scaler/universe metadata into
standalone artifacts and inference loading. Target v2 training through the
existing v1 artifact codec is intentionally not claimed complete.

Stage 7 is next and must consume the target-space/preprocessed arrays for
RMSE/ranking objectives while preserving v1 MAE. Stage 8 must compose fitted
preprocessing with configured training, checkpoint/artifact export, and strict
compatibility validation before recommendations or simulation consume these
new semantics.

## Stage 7 — model controls, RMSE, and ranking objectives

Status: complete for the versioned model controls, typed loss configuration,
bounded pairwise objective, target-space metric aggregation, focused tests, and
documentation. No database preparation or migration, provider request, live
data download, trade submission, credential output, or commit was performed.

### Changed interfaces

- `ModelSpec` accepts `stockguidance-models-v2` per-hidden-layer activation
  tuples, `shared_hidden_activation` shorthand, and a typed selectable final
  activation. `resolved_hidden_activations`, `resolved_final_activation`, and
  `to_mapping()` keep dimensions derived from the explicit feature sample and
  preserve the strict v1 serialized model shape.
- The MLP factory now constructs the resolved activation sequence and final
  activation. The existing conv/transformer architecture explicitly rejects
  incompatible v2 activation overrides; no stock-token, variable-order, or
  permutation handling was added.
- `LossSpec` and `LossObjective` add versioned `mae-v1` compatibility plus
  `loss-v2` RMSE and pairwise ranking objectives. Job decoding accepts the
  typed loss block and defaults omitted loss settings to legacy MAE.
- `stockguidance.training.losses` provides exact RMSE, bounded pairwise
  `softplus(-sign(target_delta) * score_delta)`, pair counts, ordering counts,
  and aggregate metrics. Pair blocks do not sample pairs and aggregate by
  total squared error/count or total pair loss/count across dates and batches.
- `fit_epoch_metrics` and `evaluate_metrics` calculate MAE/RMSE in target
  units, invert the shared fitted output scale before objective/metric work,
  skip pairless dates, reject an entirely pairless ranking dataset, and keep
  finite zero-error RMSE gradients. Existing scalar `fit_epoch`/`evaluate`
  wrappers remain available.
- `TrainingResult`, epoch metrics, and `training_payload` now separately
  record objective loss/version/units, target-space MAE/RMSE, non-tied pair
  count, ordering accuracy, and raw-return metrics. Raw-return metrics remain
  null for transformed targets that cannot be inverted to raw returns.
- v1 artifact model metadata and checkpoint identities retain their legacy
  shape; v2 model/loss settings are included in compatibility identities.

### Accepted decisions and semantics

- Existing jobs retain shared hidden activation, unrestricted output, v1 MAE,
  and their original serialized meanings. Changed model controls require
  `stockguidance-models-v2`; RMSE and ranking require `loss-v2`.
- Ranking is within each decision-session target row over non-tied unordered
  ticker pairs only. Pairless rows are skipped; an entirely pairless training
  or evaluation set fails clearly. `pair_block_size` bounds temporary work and
  is not a silent approximation or sampling policy.
- RMSE is `sqrt(total_squared_error / total_value_count)`, never an average of
  per-batch RMSE values. Ordering accuracy counts strictly correctly ordered
  non-tied pairs, so predicted ties are not counted as correct.
- Metrics and ranking operate after optional output normalization inversion;
  transformed target scores are reported as `target_score` rather than
  fractional raw returns. Stable ticker order and the existing tensor layout
  remain unchanged.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\training\test_stage7_objectives.py tests\models\test_stage7_controls.py -q` — passed, 9 tests.
2. `.venv\Scripts\python.exe -m pytest tests\models tests\training tests\artifacts tests\reporting tests\configuration tests\preprocessing tests\targets -q` — passed, 189 tests in 16.61s, with 15 existing pandas timezone/Torch transformer warnings.
3. `.venv\Scripts\python.exe -m pytest tests\architecture tests\recommendations tests\simulation -q` — passed, 154 tests in 35.03s, with 20 existing pandas timezone warnings.
4. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed, no issues in 304 source files.
5. `.venv\Scripts\python.exe -m py_compile src\stockguidance\models\specification.py src\stockguidance\models\mlp.py src\stockguidance\models\factory.py src\stockguidance\artifacts\decoding.py src\stockguidance\training\configuration.py src\stockguidance\training\losses.py src\stockguidance\training\engine.py src\stockguidance\training\service.py src\stockguidance\training\result.py src\stockguidance\reporting\training.py` — passed.
6. `git diff --check` — passed; only existing LF/CRLF conversion warnings were emitted.

### Standalone commands

```powershell
.venv\Scripts\python.exe -m pytest tests\training\test_stage7_objectives.py tests\models\test_stage7_controls.py -q
.venv\Scripts\python.exe -m mypy src\stockguidance
```

These commands are local CPU/unit checks and do not create a database, contact
a provider, fit against live data, or submit trades.

### Remaining limitations and next-stage prerequisites

Stage 7 does not compose automatic date/universe/preparation orchestration,
bind transformed targets/scalers/loss metadata into a new standalone
inference-artifact schema, or create saved training runs. The current artifact
codec can round-trip the model-control record, but Stage 8 must define the
versioned artifact metadata that binds model/loss controls to target,
preprocessing, universe, and layout identities. Stage 7 also does not add
raw-return reconstruction for z-score targets; those metrics correctly remain
unavailable.

Stage 8 is next: compose the completed Stages 1–7 services into configured
training, readiness/preparation, automatic universe selection, v2 artifact and
checkpoint compatibility, and saved-run export. Recommendations, simulation,
replay, and visualization remain later stages and were not implemented here.

## Stage 8 — configured training and standalone artifacts

Status: complete for the configured training resolver/runner, explicit
preparation hooks, verified date and coverage composition, automatic stable-ID
universe selection, fitted preprocessing integration, v2 standalone artifact
metadata, semantic checkpoint resume, saved-run recording, preview validation,
and focused tests. No production database, provider, live-data refresh, schema
migration, trade submission, credential output, or commit was performed.

### Changed interfaces

- `ConfiguredTrainingJob` and `load_configured_job` implement the sanitized
  `training-job-v2` contract. `resolve_configured_training` is a pure preview
  path; `validate_configured_preparation` and `prepare_configured_training`
  expose independently callable injected preparation/readiness steps.
- Configured execution composes the Stage 2 dependency plan, explicit coverage,
  Stage 4 universe selection, Stage 5 sample construction, Stage 6 fitted
  preprocessing, Stage 7 model/loss controls, typed training/export, and the
  Stage 1 run directory. Run failures are retained with resolution,
  preparation, universe, training, or export stage labels.
- Automatic universes use sorted stable IDs; explicit `ordered_ticker_ids`
  retain their declared order. `FeatureSpecV2`, `ArtifactUniverse`, and fitted
  preprocessing preserve the exact ticker-to-feature/output mapping and reject
  same-width reorderings.
- `stockguidance-inference-2` archives strictly bind feature layout, target,
  scaler, model, loss, universe labels/evidence, availability, and provenance.
  `stockguidance-inference-1` remains loadable with its original metadata
  contract. Configured checkpoints use semantic/universe-bound schema 3;
  v1/v2 preprocessing-only checkpoint compatibility remains explicit.
- `train_model --preview` resolves a v2 job without fitting, database access,
  provider access, or artifact export. Requested artifact paths remain available
  as a copy of the run-owned artifact while all attempt outputs live below the
  configured runs root.

### Accepted decisions and semantics

- Training-end gaps, fitting cutoffs, mature label exits, and holdout label
  cutoffs are separate typed values. Holdout decisions follow the fitting
  cutoff, and normalization is fitted only from eligible training samples.
- Database-backed readiness remains injected and typed. The existing bounded
  standalone preparation/SQL rendering tools remain separate from code-side
  calculation; Stage 8 does not execute SQL or alter schemas by itself.
- V1 explicit-ticker jobs retain v1 defaults and artifact readability. No stock
  descriptors, variable ticker ordering, embeddings, permutation-invariant
  models, or new transformer paths were added.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\training\test_stage8_orchestration.py -q` — passed, 4 tests, 8 existing pandas timezone warnings.
2. `.venv\Scripts\python.exe -m pytest tests\training tests\artifacts tests\models tests\runs tests\cli\test_opening_frontends.py -q` — passed, 155 tests, 31 existing pandas/Torch warnings, 23.62s.
3. `.venv\Scripts\python.exe -m pytest tests\architecture tests\documentation tests\configuration tests\preprocessing tests\targets -q` — passed, 66 tests, 8 existing pandas timezone warnings, 7.11s.
4. `.venv\Scripts\python.exe -c "from pathlib import Path; from stockguidance.training.jobs import load_job; job=load_job(Path('examples/stage8/configured-training.json')); print(job.contract_version, job.target.version, job.configuration.model.implementation_version)"` — passed and printed `training-job-v2 raw_return-v2 stockguidance-models-v2`.
5. `.venv\Scripts\python.exe -m stockguidance.cli.train_model --config examples\stage8\configured-training.json --preview` — passed with exit 0 and printed the resolved `stockguidance-training-preview-1` plan; no provider/database access occurred.
6. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed, no issues in 305 source files.
7. `.venv\Scripts\python.exe -m py_compile` over the affected Stage 8 artifact, CLI, inference, training, workflow, recommendation, and simulation modules — passed.
8. `git diff --check` — passed; only Git’s existing LF/CRLF conversion warnings were emitted.

### Remaining limitations and next-stage prerequisites

Stage 8 does not score latest data for recommendations, select recommendation
medians/top-n outputs, simulate portfolios, replay saved scores, rerun child
runs, or visualize reports. Stage 9 must consume the artifact-owned ticker,
target, and preprocessing metadata without rediscovering model dimensions and
must save recommendation failures and complete score/eligibility outputs.

## Stage 9 — artifact-owned recommendations, validate-only readiness, and saved attempts

Status: complete for the requested Stage 9 recommendation workflow, including
artifact-owned current-model semantics, validate-only refresh/readiness,
versioned score/report contracts, deterministic eligibility ranking, target
space statistics, saved successful/failed attempts, focused tests, and
documentation. No production database, live provider, schema migration,
trade submission, notification, credential output, or commit was performed.

### Changed interfaces

- `RecommendationJob`, `RecommendationRequest`, the recommendation CLI, and
  configuration decoding accept the explicit `refresh`/`validate-only` mode.
  Refresh remains the strict default. Validate-only performs bounded source,
  completion, receipt-cutoff, and timing validation without invoking the
  provider or update writer; the simplified target-schema path uses the same
  read-only validation contract.
- Current runtime assembly loads the selected model/ensemble before preparing
  the request and uses its exact feature, target, ordered ticker, and fitted
  preprocessing semantics. Configuration cannot override artifact dimensions
  or rediscover the universe from current survivors. Versioned artifacts must
  carry stable ticker labels; v1 compatibility remains unchanged.
- `RankedTicker` optionally carries its stable symbol. Versioned reports use
  `stockguidance-recommendation-2`, explicit score units, ordered scores,
  labels, eligibility evidence, top-n ranking, eligible count, and null-safe
  eligible/returned medians. Ties remain deterministic by stable ticker ID.
  Versioned jobs may return fewer than `top_k`, including an empty valid
  selection; v1 raw-return jobs retain their prior nonempty-result failure.
- The public recommendation workflow now creates a
  `RunOperation.RECOMMENDATION` attempt, records external artifact identity,
  writes `reports/recommendation.json`, fills saved-run result sections with
  all scores and eligibility/exclusions, and marks both successes and
  sanitized failures under the configured runs root. No order or notification
  side effect was added.

### Accepted decisions and semantics

- Scores are reported after artifact-owned output preprocessing is inverted,
  in the declared target space: `fractional_return` for raw returns and
  `z_score` for transformed targets. Medians use all eligible scores and the
  returned top-n scores, with the usual odd/even median and null for an empty
  set.
- Validate-only is a read-only readiness mode. It does not refresh provider
  data, repair preparation, initialize schema, migrate schema, or silently
  change a production database. Existing strict refresh behavior remains the
  production path.
- Stable ticker IDs and explicit tensor axes remain the identity contract.
  No stock descriptors, variable ticker ordering, embeddings,
  permutation-invariant models, or new transformer path was introduced.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\recommendations\test_stage9.py -q` — passed, 6 tests, 4 existing pandas timezone warnings, 7.62s.
2. `.venv\Scripts\python.exe -m pytest tests\recommendations -q` — passed, 112 tests, 8 existing pandas timezone warnings, 37.71s.
3. `.venv\Scripts\python.exe -m pytest tests\cli\test_integrated_frontends.py -q` — passed, 8 tests, 8 existing pandas timezone warnings, 3.71s.
4. `.venv\Scripts\python.exe -m pytest tests\architecture tests\artifacts tests\recommendations tests\reporting tests\configuration tests\runs tests\documentation -q` — passed, 235 tests, 23 existing pandas/Torch warnings, 47.93s.
5. `.venv\Scripts\python.exe -m pytest tests\workflows\test_database_simplification_stage9.py tests\training\test_stage8_orchestration.py -q` — passed, 10 tests, 8 existing pandas timezone warnings, 6.01s.
6. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed, no issues in 305 source files.
7. `git diff --check` — passed; only Git’s existing LF/CRLF conversion warnings were emitted.

### Remaining limitations and next-stage prerequisites

Stage 9 does not implement historical portfolio simulation, replay/rerun
children, visualization, or entry-opening Stage 10 behavior. Versioned
stored feature channels still require the existing prepared database and
typed channel repository; validate-only reports missing/stale preparation
instead of creating it. The next selected stage must consume these saved
recommendation contracts without changing the v1 semantics.

## Stage 10 — train-then-simulate and saved-score replay

Status: complete for the requested Stage 10 historical evaluation, paired
schedule, saved-score replay, pinned-settings rerun service, versioned
simulation persistence, and focused acceptance coverage. No production
database, live provider, schema migration, trade submission, credential
exposure, or commit was performed.

### Changed interfaces

- `EvaluationPolicy.TRAIN_THEN_SIMULATE` and `TrainThenSimulateScores` fit one
  pre-evaluation artifact, validate mature labels before evaluation, and reuse
  the artifact for every evaluation decision. Existing fixed and chronological
  policies remain explicit and compatible.
- Simulation configuration accepts an optional `end_date` with a default
  ten-exchange-session horizon, independent `initial_capital` and `top_k`, and
  an optional configured `runs_root`. The simulation default is top-n 1 and
  initial capital `1000.00`; recommendation defaults remain separate.
- `simulate_paired` memoizes one forecast set and produces independent
  alternate-session and staggered-half ledgers. `persist_paired_simulation`
  records a comparison parent and child runs.
- `persist_simulation_result` writes
  `stockguidance-simulation-2` with all ordered scores, eligibility and
  exclusions, execution quotes/issues, fills/trades, equity, benchmarks,
  training provenance, and execution revisions in the run manifest/report.
- `replay_saved_simulation` and `rerun_pinned_simulation` create REPLAY/RERUN
  child runs. Saved replay is offline and exact for the original request;
  changed top-n uses full-universe score coverage and changed schedules are
  rejected unless every required decision is saved. Pinned rerun accepts typed
  replacement sources and records data revision/re-resolution settings.

### Accepted decisions and semantics

- Original simulation reports remain schema v1 by default; Stage 10 persistence
  opts into schema v2 and report inspection accepts both versions.
- Saved execution evidence is required for every replay session and exact
  ticker universe. Missing entry quotes remain unfilled, missing exits retain
  positions for later retry, and final open positions are marked without a
  forced sale, matching the existing engine contract.
- A paired run rejects chronological retraining because a schedule-dependent
  expanding model cannot be shared as one frozen forecast set. Stable ticker IDs
  and explicit tensor axes remain unchanged; no descriptors, embeddings,
  permutation-invariant models, or new transformer path were added.
- Full exception type/message is retained in local failure manifests for
  diagnosis. Run sanitization redacts credential-shaped text; public CLI error
  output remains generic.

### Verification commands and actual results

1. `.venv\Scripts\python.exe -m pytest tests\simulation\test_stage10.py -q` —
   passed, 4 tests, with 4 existing pandas timezone warnings.
2. `.venv\Scripts\python.exe -m pytest tests\simulation\test_chronology.py::test_fixed_predictions_read_only_prior_sessions_at_each_decision tests\cli\test_simulation.py::test_simulation_cli_uses_verified_sql_with_nonconsecutive_ids_without_writes -q` — passed, 2 tests.
3. `.venv\Scripts\python.exe -m mypy src\stockguidance` — passed with no issues in 308 source files.
4. `.venv\Scripts\python.exe -m py_compile` over the changed Stage 10 Python modules — passed.
5. `git diff --check` — passed; only Git's existing line-ending conversion
   warnings were emitted.
6. `.venv\Scripts\python.exe -m pytest tests\configuration tests\runs tests\recommendations -q` — passed, 163 tests, 8 existing pandas timezone warnings, 40.33s.

### Remaining limitations and next-stage prerequisites

Stage 10 provides typed replay/rerun services and persistence, but does not
add a production-data replay CLI or visualization surface. Pinned reruns still
require the caller to supply typed, explicitly resolved score and execution
sources; no live-data source is inferred. Future work can add standalone
viewer/CLI adapters and then proceed to the remaining visualization and
cross-stage rehearsal requirements without changing the v1 contracts.

