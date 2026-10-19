# Schema names and z-score implementation prompts

This is the implementation brief for the approved schema customization and
z-score storage design. Run one numbered prompt at a time, in order, using
Luna 5.6 with the suggested medium or high reasoning setting. Include the shared
preamble with each prompt. Each prompt requires reading this whole brief, so its
accepted decisions remain available in a new conversation.

The implementation described here has not been performed by writing this brief.
Stage 1 must actually create the authoritative table-names file, and Stages 2
and 3 must make all supported Python and Java SQL consumers use it. Complete
Stages 1-3 before initializing the user's unmigrated database. Complete Stages
4-8 for the independent z-score storage release. Run implementation acceptance
checks against disposable fixtures; the operator initializes their real database
after reviewing the names and generated DDL.

## Shared preamble: include with every prompt

```text
Work in C:\Projects\StockGuidance. Read applicable AGENTS.md instructions,
docs/schema_zscore_implementation_prompts.md, docs/operations_guide.md,
docs/database_validation.md, and the current files relevant to this stage.
Implement only the selected stage and its necessary tests and documentation.
Inspect git status first and preserve unrelated edits. Do not commit.

Consult the python-tools skill and call get_python_environment before any
Python toolchain command. Use PyCharm's configured interpreter and package
manager. Do not revive historical PYTHONHOME/base-installation workarounds or
install dependencies blindly. Use existing NumPy and database dependencies.

Use small typed modules, thin CLI frontends, explicit source identity, injected
clocks and repositories, and bounded database operations. Preserve the existing
read-only validation contract, actual-receipt availability rules, raw
close-to-close targets, trading rules, and existing training behavior.

Use fake providers and disposable fixture storage. Do not contact live providers,
initialize or mutate the production database, migrate existing tables, delete
user data, or send messages. Keep credentials out of output and metadata.

The selected table-names JSON file is mandatory and authoritative. Create it in
Stage 1. Never embed physical table-name defaults, default templates, fallback
dictionaries, dataclass name defaults, or Java default lists in production code.
A missing or incomplete selected file must fail, never regenerate itself or
fall back to another mapping. Defaults are values in that file only.

Inspect adjacent tests, then add meaningful regression coverage for this stage.
Run focused tests, affected type checks, and git diff --check. Report the actual
commands and results. If checks cannot run, record the cause without claiming a
pass. Append a handoff under this brief's implementation record: stage status,
files changed, accepted semantics, verification, limitations, and prerequisites
for the next stage. Do not mark incomplete work complete or implement later
stages opportunistically.
```

## Accepted design

### One authoritative table-names file

Stage 1 creates `src/stockguidance/database/table_names.json` with this initial
content. Existing physical names are preserved; the logical roles are stable.

```json
{
  "schema_version": 1,
  "tables": {
    "source": "stockguidance_source",
    "sessions": "transaction_dates",
    "tickers": "ticker_symbol",
    "prices": "raw_stock_data",
    "deltas": "daily_delta_stock_data",
    "imports": "stockguidance_imports",
    "zscore_series": "stockguidance_zscore_series",
    "zscore_sessions": "stockguidance_zscore_sessions"
  },
  "zscore_templates": {
    "price": "zscores_price_{window}",
    "price_delta": "zscores_price_delta_{window}"
  }
}
```

The JSON values are the authority; examples in this document are not runtime
fallbacks. The registry owns physical table names, not column names or SQL
behavior. Existing repositories continue to own data access. Use a frozen,
typed `TableNames` registry and logical table roles rather than physical-name
comparisons to decide how to decode records or classify findings.

Add optional `table_names_file` to database configuration. A nonempty explicit
path is relative to the database configuration file's directory unless absolute.
Omission selects the mandatory packaged JSON resource, loaded through package
resources rather than the process working directory. An explicitly selected
file must never fall back to the packaged resource. Neither loader creates files.

Require exactly the versioned structure above, all required roles and templates,
and unique JSON keys. Reject unsupported versions, missing or unknown fields,
non-string values, empty paths, and invalid names. Physical identifiers must
match `[A-Za-z_][A-Za-z0-9_]*` and be at most 64 characters. Reject table-name
collisions case-insensitively. Each template must contain exactly one literal
`{window}` placeholder and no other format expressions; validate generated names
and all collisions for the requested window set before database access.
Window values must be actual integers, not booleans, and at least 2.

Centralize identifier validation and MySQL backtick quoting. Interpolate only
validated identifiers and keep values parameterized. Engine checks use the same
resolved unquoted names as parameters. Names must resolve before opening a
connection; importing a module must not eagerly open files or connections.
Production assembly resolves the registry once per operation and injects the
same immutable object throughout that operation. Test constructors may accept
an explicitly injected registry; tests do not introduce production fallbacks.

Package the canonical JSON in Python distributions. Java's build copies that
same source file into its runtime resources; do not maintain a second source
copy. Java loads the selected external file or its mandatory packaged resource
with equivalent validation and configuration-relative path behavior. Missing
resources fail clearly. A rebuilt distribution captures the registry at build
time; use the same explicit external file when running separately installed
Python and Java distributions that must share subsequent customizations.

Changing names changes the addressed tables. It does not rename, convert, or
adopt old tables automatically. Initialization remains an explicit operation.
Keep physical names out of feature and model semantic identities; source,
calculation, data revisions, and layout determine those identities.

### Numerical and storage contract

- Families are `price` and `price_delta`. The first release supports closing
  prices only. Delta inputs must be read from the existing delta table's
  `close_ratio_to_prior` column, subtracting 1 to obtain close-to-close returns.
- Each ticker has independent inclusive rolling windows of N consecutive
  exchange sessions, with N >= 2. Compute `(current - mean) / population_std`
  using `ddof=0`. Price windows require N closes; delta windows require N+1.
- Window size is normalization history, separate from model lookback history.
  Never bridge missing sessions, shorten a window, normalize across stocks,
  clip scores, or zero-fill missing inputs. Calendar holidays are not gaps.
- Use verified raw closes, source-bound manifests, actual receipt evidence, and
  the current publication rules. Verify the selected stored delta against its
  two closes using existing derived-value comparison conventions. A missing or
  inconsistent stored delta is unavailable or invalid, not silently recomputed
  as a replacement. Never depend on future-label columns or a legacy
  whole-row dependency identity that requires a following session.
- Use stable float64 rolling calculations, then quantize ready values to
  float32 for persistence as MySQL `FLOAT`. Reject non-finite results. Constant
  windows have undefined scores, not zero scores. Do not classify a nonconstant
  window as constant using an arbitrary standard-deviation cutoff.
- Each generated table contains `date_id INT`, `ticker_id INT`, nullable
  `zscore FLOAT`, and `status TINYINT`, with primary key `(date_id, ticker_id)`
  and InnoDB storage. The identifiers and status are NOT NULL. No surrogate ID,
  repeated prices, per-row revision strings, or speculative secondary indexes.
- Define stable status codes: 0 ready, 1 insufficient history, 2 missing input,
  3 unverified input, 4 invalid input, 5 zero variance. Only ready rows have a
  non-NULL finite score. At equal keys, invalid input takes precedence over
  unverified input, then missing input, then insufficient boundary history;
  zero variance applies only to a complete verified window.
- Publish rows for tickers present in the current session's raw records. A
  requested ticker absent from that session is explicitly missing or evidenced
  absent in the read result; never leave its old score behind. Session-wide
  missing mappings/evidence remain visible in completion metadata and reports.
- Shared series metadata identifies source scope, calendar/version, family,
  window, calculation version, delta definition, precision, and missing-value
  policy. Shared session metadata identifies series/date, dependency digest,
  actual input availability, completion time, row/status counts, and result
  content digest. Keep these records separate from compact numerical rows.
- Dependency digests cover the exact ordered input sessions, raw content and
  receipt/manifest evidence, ticker mappings, and selected delta inputs for the
  delta family. Do not hash unrelated future-label columns or the full legacy
  derived receipt as a substitute. A future-label-only edit must not invalidate
  either series. The same digests support unavailable results becoming ready
  when missing data or evidence arrives.
- Scores are rebuildable current materializations, not historical revision
  archives. Readers verify current dependencies and reject stale results even
  before a rebuild. Expose computation time separately from input availability;
  a recent computation is not proof of historical availability. Do not claim an
  overwritten source revision can be reconstructed.

### Extension boundaries

This release does not connect z-scores to network inputs or alter model output,
targets, recommendation ranking, or portfolio behavior. Retain stable ticker IDs
as storage identities. Do not add inactive embedding classes or encoding enums.

Document a future versioned feature adapter that records family, normalization
window, calculation version, precision, availability policy, and input layout
in sample/cache/artifact identities. It must extend the warm-up scope by the
normalization window rather than confuse it with the number of model timesteps.
Training, inference, recommendations, and simulation must eventually share that
adapter. Document a later artifact-owned mapping from stable ticker IDs to a
stock vocabulary and encoding adapter. Choose vector encoding and unknown-stock
policy when that feature is designed; database IDs never become vector offsets.

## 1. Create the authoritative registry and schema preview

Suggested reasoning: **medium**. Prerequisite: none.

```text
Implement Stage 1 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect database/configuration.py,
database/update_schema.py, config/source.py, pyproject.toml, and adjacent
database configuration and updater schema tests.

Actually create src/stockguidance/database/table_names.json with the complete
initial content specified in this brief. Add a small typed registry module,
strict loader, template resolver, identifier quoting, and optional
table_names_file configuration support. Implement every accepted naming-file
rule, including missing-file failures and no production table-name defaults.
Expose the selected registry through database configuration without printing
credentials. Preserve existing database/schema alias and driver behavior.

Package the JSON. Keep loading lazy until assembly, with explicit registry
injection available for tests. Make updater DDL generation a pure function of
the registry and use those statements in explicit initialization. Require the
same registry in source/engine assertions used by initialization. Preserve the
existing six-table schema and current column definitions.

Expose a read-only schema preview command as stockguidance.cli.preview_schema
and the preview_schema console entry point. With optional --table-names-file,
load the explicit registry or mandatory packaged resource and print resolved
names and updater DDL without opening a database connection. Z-score DDL is
added in Stage 5. Do not add z-score calculation or migrate existing tables.

Tests: complete valid registry; each missing/unknown/duplicate field; malformed
JSON; absent/unreadable files; invalid versions/types/names/templates; collisions;
booleans and invalid windows; explicit path relative to configuration while cwd
differs; explicit path never falling back; packaged loading outside the checkout;
imports without file I/O; quoted DDL uses custom names; preview makes no DB calls.
Make tests load names from a fixture registry instead of adding a code fallback.

Run focused checks and record the Stage 1 handoff. Warn in the handoff that full
custom-name operation depends on Stages 2-3; do not initialize the live database.
```

## 2. Make every Python storage consumer use the registry

Suggested reasoning: **high**. Prerequisite: Stage 1 complete.

```text
Implement Stage 2 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect database/repositories/,
database/update_schema.py, cli/validation_setup.py, cli/update_setup.py,
cli/update_openings.py, training/jobs.py, and the recommendation, simulation,
and signal-comparison runtime assemblies. Search all supported Python SQL
consumers, including diagnostics and legacy download comparison paths.

Replace physical table-name literals, required-table lists, query fragments,
and schema/engine checks with the resolved registry. Replace physical-name
behavior dispatch in validation_rows.py with logical roles. Diagnostic text
should use the relevant resolved name or an unambiguous logical description.
Do not accidentally replace column names, source identities, or historical docs.

Resolve one registry before connection creation at each production assembly
boundary and pass it to all repositories/helpers, including inventory and
validation within the same run. Ensure cli/validate_database.py's production
path uses it without weakening its no-write/no-download behavior. Propagate a
missing/invalid names-file failure as an actionable sanitized configuration
error; do not swallow it as an ordinary unknown-history or inventory finding.
Preserve direct API use through consistent resolution or explicit injection.
Do not introduce global registry caches that hide file changes between runs.

Test the existing six-table paths under default file values and a fixture
registry that renames every table. Cover initialization, ingestion, delta
replacement, date inventory, source/engine inspection, snapshot reading, orphan
and duplicate reporting, and verified close reads. Check the runtime assemblies
propagate the same registry and do not create connections on loader failures.
Maintain fixture behavior without production fallback names or live access.

Audit supported Python production sources for remaining physical-name literals.
All production defaults/templates must now come from the JSON resource; tests
may contain independent custom fixtures and expected SQL. Run affected existing
stage tests and type checks. Record the Stage 2 handoff.
```

## 3. Make Java share the authoritative names

Suggested reasoning: **high**. Prerequisites: Stages 1-2 complete.

```text
Implement Stage 3 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect StockGuidance/java/pom.xml,
its database package, validation/ValidatorMain.java, StreamingValidator.java,
other supported SQL consumers, Python validation/java_backend.py, and existing
Java configuration, SQL identifier, read-only transaction, and validator tests.

Make the Maven build copy the canonical Python table_names.json into Java
resources using an explicit include. Do not create a second handwritten file
or hardcoded default list. Add a Java typed loader with the same strict JSON,
path, identifier, version, and collision rules. An external table_names_file
is relative to its database configuration, not cwd. Missing selected files or
packaged resources fail before connection creation.

Use resolved names throughout supported Java SQL, schema checks, generic
date-range queries, and findings. Preserve read-only transactions and existing
report contracts. Ensure Python-launched Java uses the effective selected
registry, including when Python and Java packaged defaults were built at
different times: pass an explicit --table-names-file override at that boundary,
materializing the selected package resource only for the subprocess lifetime
when necessary. Standalone Java may omit that override and use the database
configuration or mandatory built resource. Never pass credentials in arguments.

Test renamed tables, invalid/missing external and packaged registries,
configuration-relative paths from another cwd, Python subprocess arguments and
resource lifetime, and default/custom Python-Java report parity. Prove no
write-capable behavior was added. Confirm Maven uses the canonical source.
Audit Java production code for remaining physical-name defaults and templates.

Run focused Java/Python boundary checks and record the Stage 3 handoff. At this
point all supported naming paths must be ready for operator-reviewed schema
initialization; do not execute it against the real database.
```

## 4. Implement pure rolling z-score calculation

Suggested reasoning: **high**. Prerequisites: Stages 1-3 complete.

```text
Implement Stage 4 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect calendar contracts,
market_data/closes.py, legacy_derived.py, availability.py, features/history_window.py,
and existing numerical tests. Create a small zscores subpackage for typed
specifications, inputs/results, statuses, and pure computation. Keep database,
CLI, training, and device dependencies outside numerical modules.

Implement the accepted inclusive per-ticker population z-score semantics and
status codes. Define an explicit calculation version and ordered dependencies.
Input contracts distinguish unsupported boundary history from missing interior
sessions, invalid values, unverified evidence, and actual availability. Preserve
actual session order and ticker identities. Delta values are explicit inputs
from the delta table; computation must not silently manufacture missing deltas.

Calculate float64 results with a stable rolling algorithm and quantize ready
results to float32, rejecting non-finite values. Support multiple windows with
bounded memory, shared source data, and approximately linear work per window;
do not build a full overlapping-window tensor or use cancellation-prone raw
sum-of-squares variance. Include a small independent two-pass reference in tests.
This stage introduces no schema writes, CLI, or training feature changes.

Tests: price [1,2,3] at N=3 produces sqrt(1.5) for its last score; independent
tickers; multiple windows; N=2; constant values; missing sessions versus holidays;
boundary history; invalid/non-finite data; status precedence; float32 rounding;
large nearly constant values; and inputs that change after the scored session
leaving earlier outputs unchanged. Include a nonconstant delta example requiring
N+1 closes in its declared dependencies. Compare optimized outputs to the
independent reference, not another call to the production implementation.

Run focused checks and record the Stage 4 handoff with numerical semantics and
actual performance evidence or an explicit unmeasured limitation.
```

## 5. Implement compact storage, verification, and freshness

Suggested reasoning: **high**. Prerequisites: Stages 1-4 complete.

```text
Implement Stage 5 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect existing database read/write
protocols, repositories/market_data.py, verified close projection, manifests,
ingestion.py, validation/derived.py, and updater transaction tests.

Add an injected z-score repository protocol and MySQL implementation for bounded
input loading, series/session metadata, atomic result publication, and verified
reads. Load the two families' required inputs in bounded batches; read only the
selected legacy delta column for the new delta dependency, and verify it against
the corresponding verified current/prior closes. Do not gate these inputs on
future-label completeness or change legacy delta repair semantics.

Implement the exact compact per-window row layout and metadata responsibilities
from this brief. Define the metadata schema and version explicitly. A unique
series definition must prevent silent mixing of source, calendar, calculation,
window, and precision. A session completion record means its computation and
statuses were published atomically; it does not imply every score is ready.
Reject incompatible preexisting tables or conflicting definitions.

Replace all rows for a family/window/session and its metadata in one transaction,
removing scores for tickers no longer present. Revalidate dependencies inside
the write transaction using locking current reads of dependency receipts/date
mappings/selected delta rows in deterministic order, so publication cannot race
a conflicting source/delta write. Roll back on mismatch or failure. Recheck
manifest evidence before publication and on every verified read; file and DB
storage do not provide a joint atomic transaction. Reject stale dependencies
on read regardless of whether a later update command has run. Never claim to
have restored an old source revision from current materializations.

Create schema only through an explicit initializer. Extend preview_schema with
optional --windows, printing updater, metadata, and generated z-score DDL for
those windows without any DB connection. Ordinary updater schema checks must
not require optional z-score tables. Resolve all table names from the registry.

Tests: renamed tables/templates; exact FLOAT/status/key DDL; rollback after row
or metadata failures; idempotency; vanished ticker rows; conflicting definitions;
correction/backfill/evidence arrival invalidation; stale reads before rebuilding;
delta-only edits; future-label-only edits leaving freshness unchanged; concurrent
dependency changes; malformed metadata; bounded reads and executemany writes.
Use disposable fixtures and record unsupported native-MySQL locking checks.

Run focused checks and record the Stage 5 handoff.
```

## 6. Add the independent z-score update command

Suggested reasoning: **high**. Prerequisites: Stages 1-5 complete.

```text
Implement Stage 6 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect existing CLI path resolution,
update/validation assembly, discovery, publication, workflow result/reporting
patterns, and pyproject.toml. Add a typed update_zscores workflow and thin
stockguidance.cli.update_zscores frontend, with update_zscores console entry.

Require --windows as comma-separated distinct integers >=2 and require
--start-date and --end-date. Use existing --data-root, --dbinfo-file, --adjustment,
optional evidenced --history-start/--history-evidence, and
--publication-delay-minutes/--publication-evidence conventions. Publication
evidence and delay are required for this write workflow. Support --initialize-schema
for optional z-score tables only; existing updater tables and source binding
must already be compatible. Use the injected current clock as the run cutoff;
do not introduce historical write mode or downloads.

Populate both families for requested exchange sessions using local data only.
Read necessary warm-up context up to the maximum window, including the extra
prior close for deltas, without writing warm-up results outside the requested
range. Do not create raw data, date mappings, or replacement delta values.
Missing dependencies remain visible with accepted statuses and report findings.

Skip matching completed results after current dependency/content verification.
Rebuild stale or absent requested results, deleting obsolete rows atomically.
Report requested/effective scope, windows, cutoff, initialized tables, written
and skipped sessions, ready/unavailable status counts, stale/rebuilt results,
and failures. Use exit 0 for complete requested computation (including expected
boundary/constant unavailability), 2 for incomplete/unverified input coverage or
invalid arguments, and 1 for execution/storage failure. Never report a wholly
unavailable or failed requested range as a successful ready dataset.

Test CLI parsing and path behavior, required evidence, cutoff handling, explicit
initialization, missing updater schema, no provider calls, multiple windows,
warm-up bounds, holidays, corrections, repeat runs, and partial failures.
Verify raw/delta/history/manifest files and records are never modified.
Do not automatically attach this command to market-data updates or training.

Run focused checks and record the Stage 6 handoff.
```

## 7. Add read-only z-score validation and cross-stage checks

Suggested reasoning: **high**. Prerequisites: Stages 1-6 complete.

```text
Implement Stage 7 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect cli/validate_database.py,
cli/validation_setup.py, workflows/validate_database.py, validation finding,
repair, and reporting contracts, plus the new z-score repository/workflow.

Add optional --zscore-windows with the same parsing rules as update_zscores,
and propagate the typed request through validation settings. When supplied,
audit both families for those windows and the effective requested scope.
Inspect definitions, schema, row keys/statuses, completion metadata, current
dependencies, actual availability, and numerical scores. Independently check
against verified inputs using float32-aware rtol=1e-5 and atol=1e-6; do not
validate values merely by rerunning the same production calculator. Read
warm-up context without expanding reported output scope.

Add explicit z-score checks and report family/window/date/ticker, expected and
stored score/status, and stale/missing/invalid/unverified reasons. Preserve
existing findings, report fields, and safe exit codes. Existing repair-plan
execution must not interpret a new z-score finding as an authorized raw/delta
repair: use manual-review guidance to rerun the explicit z-score updater, and
add repair decoding/report version changes only if the wire contract requires it.
Without --zscore-windows, missing z-score tables must not affect normal validation.

This new optional audit is Python-only. Preserve Java's existing audit scope
and naming parity; do not silently claim Java implements z-score validation.
Document that scope difference. The validator may read valid cached evidence
but must perform zero writes, DDL, downloads, repairs, or metadata persistence.

Test correct and tampered scores/statuses, missing tables/rows/metadata, stale
revisions, receipt cutoffs, expected boundary absence, constant windows, renamed
tables, config errors, and ordinary validation with no z-score tables. Prove
zero DB/file writes on success and every failure path. Run cross-stage fixture
initialization, ingestion, delta repair, z-score update, and validation under a
registry that renames every table and template. Count queries and batch sizes
to catch one-query-per-ticker/session behavior and unbounded materialization.

Run affected stage regressions, type checks, and Java naming parity checks.
Record the Stage 7 handoff and any native-MySQL/performance limitations.
```

## 8. Document operator rollout and future input design

Suggested reasoning: **medium**. Prerequisites: Stages 1-7 complete.

```text
Implement Stage 8 of docs/schema_zscore_implementation_prompts.md.

Read the entire brief and shared preamble. Inspect all prior stage handoffs and
the implemented CLI help. Update the operations, database validation, frontend,
and feature documentation where necessary, describing implemented behavior only.

Provide the exact operator order: customize the mandatory registry or select an
external one in database configuration; preview names/DDL; explicitly initialize
compatible updater tables and ingest data using existing commands; preview the
requested z-score windows; explicitly initialize/populate them; then validate
with --zscore-windows. Show multiple windows such as 20 and 60. Explain registry
path resolution, installed-resource behavior, Java rebuilds versus shared external
files, and missing-file errors. Do not execute any live rollout commands.

Explain that names-file edits do not rename existing tables, CREATE TABLE IF NOT
EXISTS does not convert incompatible schemas, z-score population is an explicit
separate step, stale results are rejected until rebuilt, FLOAT has float32
precision, windows count exchange sessions, deltas use the stored prior-close
ratio minus 1, and NULL/status codes preserve undefined results. State that a
historical cutoff does not recover overwritten revisions or original receipts.

Document the future feature/encoding boundaries specified in this brief. Keep
ticker IDs stable; describe versioned feature adapters and artifact-owned
vocabularies without implementing them or choosing an encoding/unknown-stock
policy now. Identify additional warm-up, feature/cache/artifact identity, and
shared training/inference wiring that future work must address.

Verify the mandatory JSON exists, is packaged, and supplies every production
table name/template in Python and Java. Recheck that no code fallback names
remain and all CLI examples agree with actual help. Run the appropriate final
regressions and required checks; do not repeat passed expensive suites without
a relevant change or unresolved concern. Record actual verification and limits,
including that fixture evidence is not production performance measurement.
Finish the handoff with remaining operator actions before real database creation.
```

## Implementation record

No implementation stages are complete when this brief is first created. Append
one dated handoff per stage after executing that prompt. If a stage is partial,
record the remaining work and do not start dependent stages.

Use these fields: stage and status; changed files; accepted semantics; actual
verification commands/results; limitations; next-stage prerequisites; operator
actions, if any. Keep the approved decisions above authoritative unless the user
explicitly revises them.

### 2026-10-19 — Stage 1 — complete

- Changed files: `src/stockguidance/database/table_names.json`, `src/stockguidance/database/table_names.py`, `src/stockguidance/database/configuration.py`, `src/stockguidance/database/update_schema.py`, `src/stockguidance/cli/update_setup.py`, `src/stockguidance/cli/preview_schema.py`, `pyproject.toml`, and `tests/stage1/test_table_names.py`.
- Accepted semantics: the packaged JSON is the authoritative initial registry; explicit configuration paths resolve relative to the database configuration file; loading is strict about schema, fields, duplicate keys, types, identifiers, collisions, and templates; generated z-score names validate requested integer windows before use; updater DDL and initialization use one resolved registry; preview is read-only and does not open a database connection.
- Verification: `C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe -m pytest tests/stage1/test_table_names.py tests/stage9/test_database_configuration.py tests/stage1/test_imports.py` passed with 31 tests; the affected database/session, updater, and integrity suites passed with 118 tests; `... -m mypy src/stockguidance/database src/stockguidance/cli/preview_schema.py` passed with no issues; `... -m stockguidance.cli.preview_schema` exited 0 and printed the resolved registry and six updater DDL statements; `git diff --check` passed.
- Limitations: existing Python storage consumers still use their historical physical names until Stage 2, and Java naming parity remains a Stage 3 task. No live database was initialized or modified, and no z-score calculation or z-score DDL was added.
- Next-stage prerequisites: Stage 2 may now replace supported Python storage-consumer literals and propagate one selected registry through each production assembly; it must preserve the packaged/external loading rules and continue using disposable fixtures.
- Operator actions: none for this stage; review `preview_schema` output before any later explicit schema initialization against an operator-selected database.
