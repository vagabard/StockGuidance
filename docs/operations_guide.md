# Operating StockGuidance

This guide describes the current Python commands and the shared automatic
history discovery contract. Stages 1-8 of the schema and z-score implementation
brief are complete; this guide documents their operator rollout.
Validation and updating use the shared resolver. The broader maintenance prompts
are in [luna_maintenance_prompts.md](luna_maintenance_prompts.md). Start with
the Python workflow. The historical Java prototypes remain unfinished and
separate from the current Java foundation; measured Java paths are not treated
as semantically equivalent to Python. See
[java_performance.md](java_performance.md) for the reproducible offline study.

For adoption of the retained Massive NDJSON archive, use the staged
[legacy migration prompts](legacy_migration_prompts.md). They begin with a
bounded Java-thread/Python-process comparison and include numeric-precision
and case-sensitive ticker prerequisites before population. Schema initialization
alone does not make legacy rows verified, preserve fractional volume in a BIGINT
column, or distinguish ticker keys under a case-insensitive unique index.
The [initial migration results](legacy_migration_results.md) record the measured
reader comparison, passing source sample and stock-table discrepancies.


## Java and performance status

Stage 6 is complete as a bounded offline performance study. The large fixed
fixture measured raw/derived reconciliation, JSON response decoding and
hashing, typed portfolio event processing, tensor construction, and inference
separately under cold and warm conditions. Raw/derived reconciliation was the
largest measured CPU/allocation path; no SQL/query or transfer measurement was
possible because no disposable SQL service was available. No production schema,
index, provider, or database change was made.

Stage 7 is complete as a typed Java foundation under
[`StockGuidance/java/README.md`](../StockGuidance/java/README.md). The Maven
build targets Java 21, uses Apache Fory JSON and JUnit Jupiter 5.13.4, and pins
the bundled MySQL Connector/J 9.6.0. The current source tree is separate from
the historical prototypes, which are excluded from the build and remain
unsupported legacy readers.

The active database path decodes only an explicit JSON file, applies the
Python-compatible database/schema conflict rules and type checks, keeps
credentials out of errors, uses read-only repeatable-read transactions with
rollback, and binds date values through prepared statements with allowlisted
identifiers. The versioned interchange contract covers source identity,
calendar/exchange dates, ordered ticker IDs, coverage provenance, receipt times,
availability policy, nulls, revisions, errors, findings, canonical hashes and
BigDecimal money rounding. Stage 8 adds an opt-in bounded read-only Java
validator for the measured raw/derived reconciliation operation. It does not
fetch providers, write databases, repair rows, or replace the Python default.
Python remains the semantic reference and owns provider, calendar, feature,
training, and simulation policy.

From `java`, the offline Java checks are:

```powershell
mvn -o test
mvn -o -DskipTests package
```

## Recommended order

For an existing database, use **validate -> update/repair -> validate -> train
-> simulate**. The first validation records existing problems before a write;
the second checks the resulting data. Save both reports. A failed validation
does not automatically mean that an update can repair every finding.

For a new empty database, first establish source/history policy and explicitly
initialize the updater schema, then **update -> validate -> train -> simulate**.
Read-only validation never creates tables. An incompatible existing schema needs
inspection and a separate migration decision; `--initialize-schema` does not
convert it.

For routine operation after a clean baseline, use **update -> validate -> use an
existing model**, or **update -> validate -> retrain -> use the new model**.
Independent validation is useful as an audit even though the updater performs
shared reconciliation checks. Training and simulation do not update data.

Before either an initial update or a repair, back up the database and retained
source files using your normal backup procedure. Keep one writer per selected
database/data root; run audits against a consistent snapshot or while it is idle.

## Schema and z-score rollout order

Use this order for a new database or for adding the independent z-score
materializations to an existing compatible database. These examples are an
operator runbook; do not run them against a real database until the selected
names and generated DDL have been reviewed.

1. Select the authoritative registry. Keep the packaged
   `src/stockguidance/database/table_names.json` unchanged to use the standard
   names, customize that file before rebuilding an installation, or select an
   external copy from the database configuration. For example, if
   `config/dbinfo.id` contains `"table_names_file": "table_names.custom.json"`,
   that path is resolved relative to `config/dbinfo.id`, not the working
   directory. A missing, unreadable or invalid selected file fails; it never
   falls back to the packaged resource. The configuration file still owns the
   host, database, user, password, port and driver settings.

   ```json
   {
     "host": "<database-host>",
     "database": "<database-name>",
     "user": "<database-user>",
     "table_names_file": "table_names.custom.json"
   }
   ```

2. Preview the selected names and the existing updater DDL without opening a
   database connection. Use an absolute path when the preview is run from a
   different directory; `preview_schema --table-names-file` reads the path
   supplied to that command, while `table_names_file` in `dbinfo.id` remains
   configuration-relative.

   ```powershell
   $namesFile = (Resolve-Path 'config/table_names.custom.json').Path
   & $pythonExe -m stockguidance.cli.preview_schema --table-names-file $namesFile
   ```

   Omit `--table-names-file` when using the mandatory packaged resource. The
   preview must show the expected eight updater/support tables before initialization.

3. Explicitly initialize the compatible updater tables, then ingest raw and
   derived data with the existing market-data command. The updater's
   `--initialize-schema` is the only step in this sequence that creates the
   eight-table base schema; it does not rename, migrate or convert existing
   tables.

   ```powershell
   $updateArgs = @('--data-root', 'data', '--dbinfo-file', 'config/dbinfo.id', '--adjustment', 'adjusted')
   $historyArgs = @('--history-start', '<first-supported-session>', '--history-evidence', '<boundary provenance>')
   $publicationArgs = @('--publication-delay-minutes', '<documented delay>', '--publication-evidence', '<publication provenance>')
   $correctionArgs = @('--recent-window-days', '<correction window>', '--recent-refresh-hours', '<recent interval>', '--archive-refresh-days', '<archive interval>', '--correction-evidence', '<correction provenance>')
   & $pythonExe -m stockguidance.cli.update_market_data @updateArgs @historyArgs @publicationArgs @correctionArgs --initialize-schema
   ```

   Replace every placeholder with operator evidence. Existing compatible
   tables may be used without initialization; incompatible tables require a
   separate migration decision. `CREATE TABLE IF NOT EXISTS` does not convert
   an incompatible schema.

4. Preview the requested z-score windows and generated metadata/materialization
   DDL. The example uses two independent normalization windows:

   With an external registry selected in `dbinfo.id`, run:

   ```powershell
   & $pythonExe -m stockguidance.cli.preview_schema --table-names-file $namesFile --windows 20,60
   ```

   With the packaged registry, omit `table_names_file` and the option:

   ```powershell
   & $pythonExe -m stockguidance.cli.preview_schema --windows 20,60
   ```

5. Explicitly initialize and populate the selected z-score tables from local
   verified data. This is a separate write step; it does not call a provider or
   repair raw, date-mapping, delta, history, manifest or legacy-derived data.

   ```powershell
   $zscoreArgs = @('--data-root', 'data', '--dbinfo-file', 'config/dbinfo.id', '--adjustment', 'adjusted',
       '--windows', '20,60', '--start-date', '<first-session>', '--end-date', '<last-session>',
       '--publication-delay-minutes', '<documented delay>', '--publication-evidence', '<publication provenance>')
   & $pythonExe -m stockguidance.cli.update_zscores @zscoreArgs @historyArgs --initialize-schema
   ```

   The updater reads exchange-session warm-up context but publishes only the
   requested range. Repeat the command without `--initialize-schema` for later
   rebuilds after reviewing the reported stale or unavailable results.

6. Run the read-only Python validator with the same window selection:

   ```powershell
   & $pythonExe -m stockguidance.cli.validate_database @updateArgs @historyArgs @publicationArgs `
       --start-date '<first-session>' --end-date '<last-session>' `
       --zscore-windows 20,60 --format json > output/validation-zscores.json
   ```

   Validation does not initialize or repair z-score tables. It rejects stale
   results before a rebuild, reports NULL/status outcomes explicitly, and keeps
   z-score findings out of raw/delta repair authorization.

Training and simulation use the same database read boundary. For the canonical
Polygon/Massive source, that boundary rejects requested sessions before
`2006-11-16` and runs an advisory guardian over z-score status rows for windows
`20` and `60`. Non-ready statuses are reported as `DataValidityWarning`; they
are never replaced with interpolated values or zeroes.

### Daily maintenance

Run the maintenance frontend daily or after missed runs. It discovers published
XNYS trading days in a two-calendar-year repair window, downloads missing,
corrupt or refresh-due Massive source responses, and uses the production Java
writers for raw rows, daily derived rows and z-score windows `20,60`. Independent
Python validation checks each bounded batch, using the Stage 15 vectorized,
multiprocess z-score validator. Existing matching raw and derived rows are reused;
z-score calculations recheck their dependencies and preserve matching output rows.

```powershell
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily
```

The command is read/write and requires the normal provider credential and an
initialized migrated production schema. Build Java with
`mvn -f java/pom.xml package -DskipTests` after code changes. Source semantics and
table names are inferred from the database configuration. No operator-supplied
migration manifest is required: live workload descriptions and resume evidence
are generated under `output/daily-maintenance/java`.
A nonzero result must be resolved before training or forecasting. JSON goes to
stdout; progress and phase estimates go to stderr. See
[Daily maintenance](daily_maintenance.md) for recovery, bounds and readiness.
The default publication policy is 15 minutes with an explicitly labeled
operational assumption; override it with `--publication-delay-minutes` and
`--publication-evidence` when provider-specific evidence is available.
Correction defaults are a 30-day recent window, refresh-on-every-run for
recent sessions (`0` hours), and a 30-day archive recheck interval. A session
means one exchange trading day, not one calendar day. The recent window is the
last 30 days since each trading session's close; older sessions use the archive
interval.

The Python package loads the canonical JSON through package resources when no
external file is selected, so an installed distribution does not depend on its
working directory. The Java build copies that same source JSON into Java
resources; rebuild Java after changing the canonical file. Separately installed
Python and Java distributions must use the same explicit external registry when
their packaged snapshots may differ. Python-launched Java validation receives
the effective selected registry for the subprocess lifetime. Java retains its
read-only raw/derived validation scope; Python owns the optional z-score audit.

Names-file edits address different physical tables; they do not rename or adopt
old tables. The generated rows are current rebuildable materializations, not a
revision archive. Each score uses a window of exchange sessions, not calendar
days. Price-delta inputs use the stored `close_ratio_to_prior` value minus one;
the updater does not silently recompute a missing or inconsistent stored delta.
MySQL `FLOAT` persistence has float32 precision. Only finite ready rows have a
non-NULL score; NULL scores and status codes preserve insufficient history,
missing, unverified, invalid and zero-variance outcomes. A historical cutoff
controls availability decisions but cannot recover overwritten source revisions
or original receipt evidence.

## Environment

Run from `C:\Projects\StockGuidance`. Use the executable selected by PyCharm for
this project. On 2026-10-16 the IDE reported Python 3.14.8 at:

```powershell
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $pythonExe -m stockguidance.cli.validate_database --help
& $pythonExe -m stockguidance.cli.update_market_data --help
& $pythonExe -m stockguidance.cli.train_model --help
& $pythonExe -m stockguidance.cli.simulate --help
```

Rediscover the interpreter if the SDK changes. The explicit `PYTHONPATH` supports
running this checkout without an editable installation. The repaired interpreter
passed these four help commands and module-presence checks for NumPy,
exchange-calendars, Torch and pytest. That is not a Torch training, CUDA, MySQL or
live-provider acceptance test. The IDE reported an unknown package manager, so
no package installation was attempted. Old Python-base workarounds in historical
refactoring handoffs should not be copied into the repaired environment.

The validation and download-comparison frontends also locate the checkout root
when launched by opening a Python file in the IDE, so their default `data` and
`dbinfo.id` paths do not depend on the IDE working directory.

## Three different date ranges

| Range | Meaning | How it should be used |
|---|---|---|
| Retained local history | Dates in local responses/manifests and database rows; these may start before current download access | Audit and preserve it, recording which observations are actually verified |
| Currently downloadable history | Account/request-specific provider coverage as of a particular check | Bound network requests and scheduled refreshes |
| Requested operation range | Dates selected for validation, updating, training or simulation | Default to appropriate discovered coverage; allow explicit date subsets |

The latest database row is an **observation**, not proof of completeness. The
update target is the latest completed exchange session whose daily data should
be published at the current clock, using the configured publication delay.
Weekends, holidays, early closes and provider delays matter. Scan for interior
gaps even if a later date is present.

Example: retained data starts in 2010, while today's provider entitlement starts
in 2021. Validation should still inspect the retained 2010-2020 archive. Updating
should preserve those years, use intact local evidence to repair imports where
possible, and fetch only within today's supported download range. An archive
gap that cannot be fetched must remain an explicit unresolved finding. It must
not trigger deletion, a fabricated replacement, or a claim of complete history.
Raw database rows alone do not prove source identity or original availability.

**Current validation behavior:** the validator uses the shared resolver to
inventory source-bound manifests and legacy database observations without
writing discovery metadata. With date and history arguments omitted, it audits
the observed retained scope through the latest expected published session. It
reports local observed bounds, verified retained sessions and holes, cached
provider capability/freshness, the effective audit scope, and unresolved
earliest/provider limits separately. The history arguments remain optional,
source-bound overrides; validation never persists them. The updater uses the
same resolver, inventories manifests and eligible legacy date mappings once,
and keeps retained observations separate from current provider entitlement.
Network fetches require fresh verified provider capability evidence. Unknown or
stale capability leaves local audit/reconciliation visible and reports the
unresolved provider limit without fetching.

For a local consistency audit, `--history-evidence` is provenance for an
explicit earliest-history claim, while `--publication-evidence` is provenance
for the expected post-close provider publication delay. Both are optional for
the read-only local audit. The audit uses the earliest observed retained date as
its local floor and reports provider-history and publication uncertainty
separately. For exact comparison with the older downloaded archive, use the
slower `compare_database_downloads` command documented in
`database_validation.md`.

Stage 1 discovery is read-only and takes an injected clock, authoritative session
calendar, publication policy, optional local inventory, and an optional typed
provider capability adapter. `ManifestStore.inventory()` scans source-bound
response manifests by their validated payload dates. `MySQLSessionRepository.inventory()`
performs one bounded query for legacy date mappings; those rows remain explicitly
unverified because they have no source-bound response or receipt evidence. A
provider without a capability adapter returns `unknown`; stale capability evidence
is retained but never supplies fetch bounds. An explicit history override is
source-bound and conflicts with a different persisted boundary instead of
rewriting it. Version 2 discovery metadata is stored in a separate
`*.discovery.json` sidecar, preserving existing version 1 boundary records.

Provider access may roll forward or change with an account. A first successful
download, an empty response, a filename or `MIN(date)` cannot establish an
authoritative provider limit. Automatic discovery needs evidence; an unresolved
limit must be reported honestly. Offline validation must not contact the provider.

## Validate and update with the current commands

Use one data root, database configuration, source identity and adjustment policy
throughout. The examples use `adjusted`; select what matches your existing
source. `dbinfo.id` is an explicit JSON configuration; do not paste its contents
into prompts or reports. Set `POLYGON_API_KEY` through your existing configuration
if needed. No credential values belong in these examples.

Set policy values from your provider/account evidence. No live delay or
correction interval is inferred here:

```powershell
$publicationDelay = Read-Host 'Verified publication delay in minutes'
$publicationEvidence = Read-Host 'Publication policy provenance'
$recentWindowDays = Read-Host 'Recent correction window in days'
$recentRefreshHours = Read-Host 'Recent refresh interval in hours'
$archiveRefreshDays = Read-Host 'Archive refresh interval in days'
$correctionEvidence = Read-Host 'Correction policy provenance'

$commonArgs = @('--data-root', 'data', '--dbinfo-file', 'dbinfo.id', '--adjustment', 'adjusted')
$publicationArgs = @('--publication-delay-minutes', $publicationDelay, '--publication-evidence', $publicationEvidence)
$correctionArgs = @('--recent-window-days', $recentWindowDays, '--recent-refresh-hours', $recentRefreshHours,
    '--archive-refresh-days', $archiveRefreshDays, '--correction-evidence', $correctionEvidence)
$historyArgs = @()
New-Item -ItemType Directory -Force -Path 'output' | Out-Null
```

For a current checkout, `$historyArgs` is an optional source-bound override. It
is not required when retained local evidence or fresh provider capability
establishes an auditable scope. A conflicting override remains an error; it
never expands current provider fetch access by itself.

```powershell
$historyArgs = @('--history-start', '<evidenced-first-supported-session>',
    '--history-evidence', '<non-secret provenance>')
```

Replace the bracketed strings before use. If history is already persisted or
local retained evidence is discoverable, leave `$historyArgs` empty. Validation
uses an explicit boundary only in memory. The updater persists a new explicit
boundary only when no matching source-bound boundary already exists.

```powershell
& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs --format json > output/validation-before.json
$validationExit = $LASTEXITCODE

& $pythonExe -m stockguidance.cli.update_market_data @commonArgs @historyArgs @publicationArgs @correctionArgs
$updateExit = $LASTEXITCODE

& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs --format json > output/validation-after.json
$validationExit = $LASTEXITCODE
```

These are separate steps: inspect the report and exit code between commands.
An existing database with integrity/schema findings needs review before writes.
For a new empty database only, add `--initialize-schema` to its first update.
Add `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` to narrow the operation;
bounds are inclusive. Omitted dates use the discovered retained/fetchable scope;
the default end is the latest expected published exchange session. If current
provider capability is unknown or stale, the update still audits and reconciles
intact local evidence, but it does not guess a fetch range. Missing or due
refreshes outside access remain explicit unresolved/stale results. Corrupt
source content outside access is preserved and cannot be marked complete.

Validation exits: `0` verified required coverage, `2` missing/invalid/incomplete/
unverified coverage or no available session, `1` setup/inspection failure.
Argument errors also use `2`. Check the findings and feature scope as well as
the exit code: a calendar-only range can pass without any training samples.

To audit optional rolling z-score materializations without any writes or
provider access, add `--zscore-windows 20,60` to each validation command. This
checks both price and stored close-ratio delta families, including warm-up
dependencies, receipt cutoffs, current dependency digests, statuses, metadata,
and independently recomputed float32 scores. Ordinary validation omits this
audit and does not require z-score tables. The Python validator owns this
optional audit; the Java validator retains its existing raw/derived comparison
scope. Z-score findings require a separate explicit `update_zscores` run and
are never interpreted as raw/delta repair authorization.

To check the exact model input layout, add both arguments using **real IDs in
the intended order**:

```powershell
& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs `
  --start-date '<first-entry-date>' --end-date '<last-entry-date>' `
  --ticker-ids '<ordered,real,ticker,ids>' --lookback-sessions 20 --format json > output/model-readiness.json
```

Feature checks also enforce historical receipt availability. Ordinary raw/derived
coverage success does not guarantee model readiness. `entry_open` requires its
own feature specification and original opening receipts; start with
`previous_close` unless those receipts exist.

For an automatic repair plan that has no manual-review items, substitute this
for the ordinary update command:

```powershell
& $pythonExe -m stockguidance.cli.update_market_data @commonArgs @historyArgs @publicationArgs @correctionArgs `
  --repair-plan output/validation-before.json
```

Use a recent report from the same source and scope. The updater rejects plans
requiring schema/integrity/policy review; do not remove those findings merely to
make the plan run. Explicit date bounds must match the plan. Always validate
again afterward. See [database_validation.md](database_validation.md) for the
finding taxonomy and [market_data_updating.md](market_data_updating.md) for recovery.

### Populate rolling z-scores locally

Z-score materializations are an explicit, independent write step. They use only
verified local raw closes, source-bound manifests and the stored
`close_ratio_to_prior` delta column; they never call a provider or repair raw,
date-mapping, delta, history, manifest or legacy-derived data. Review the
selected registry and optional tables first:

```powershell
& $pythonExe -m stockguidance.cli.preview_schema --windows 20,60
& $pythonExe -m stockguidance.cli.update_zscores `
  --data-root 'data' --dbinfo-file 'dbinfo.id' --adjustment adjusted `
  --windows 20,60 --start-date '<first-session>' --end-date '<last-session>' `
  --publication-delay-minutes '<documented delay>' --publication-evidence '<publication provenance>' `
  --history-start '<first-supported-session>' --history-evidence '<boundary provenance>' `
  --initialize-schema
```

Use `--initialize-schema` only after reviewing the preview and only when the
existing eight updater tables and source binding are already compatible. Omit it
for later rebuilds. Windows count exchange sessions and include warm-up context
without publishing warm-up rows. The command reports the current cutoff,
effective scope, ready/status counts, stale rebuilds and idempotent skips. Exit
`0` means the requested eligible computation completed, including expected
boundary or constant-window unavailability; exit `2` means input coverage is
incomplete or not yet published; exit `1` means setup or storage failed.

The names-file path follows database configuration rules: an explicit relative
path is relative to that configuration file, while omission uses the packaged
authoritative resource. Editing names does not rename existing tables, and
`CREATE TABLE IF NOT EXISTS` does not convert incompatible schemas. Z-score
tables are current rebuildable materializations, not revision archives; stale
dependencies are rejected until this explicit updater is rerun. Stage 7 adds
the optional read-only validation audit; ordinary validation does not require
z-score tables.

## Train on a date subset

Keep the full archive; choose training entry dates in the job JSON. Do not delete
rows or renumber database IDs. Start with the complete configuration example in
[model_training.md](model_training.md), save it as `training-job.json`, and edit:

For a parser-valid disposable starting point, use
[`examples/stage5/previous-close-training-subset.json`](../examples/stage5/previous-close-training-subset.json).
The fixed later-period and chronological-retraining companions are
[`examples/stage5/fixed-later-simulation.json`](../examples/stage5/fixed-later-simulation.json)
and
[`examples/stage5/chronological-retraining-simulation.json`](../examples/stage5/chronological-retraining-simulation.json).
All three are fixture-only configurations with no credentials and strict
previous-close availability by default.

| Field | Choice |
|---|---|
| `source`, `data_root`, `database_config` | Your verified source and storage |
| `features.ticker_ids` | Explicit ordered real IDs, fixed without future survival information |
| `features.lookback_sessions` | Number of preceding exchange sessions per sample |
| `features.signal_mode` | `previous_close` for the initial workflow |
| `start_date`, `end_date` | Inclusive **entry-session** training subset |
| `training_cutoff` | Aware timestamp when fitting is allowed to know its mature labels |
| `availability_policy` | `strict` (default) or explicitly opted-in `retrospective_daily_previous_close` research mode |
| `data_snapshot` | Required aware research snapshot; keep `null` for strict mode |
| Publication fields | Your evidenced delay and provenance |
| `evaluation` | A strictly later holdout, or `null` initially |

The following dates are an illustrative split, not evidence that this checkout
has usable observations for them:

```json
{
  "start_date": "2022-01-03",
  "end_date": "2023-12-28",
  "training_cutoff": "2023-12-30T12:00:00+00:00",
  "evaluation": null
}
```

This is a **fragment to merge into the full example**, not a standalone job.
The last training entry needs its next-session close and publication before the
cutoff. With a 20-session lookback, retain 20 sessions before the first entry,
and at least one exit session after the last entry. Each selected ticker must
have the required observations; missing values are not zero-filled. The first
usable entry can be later than the local archive minimum.

```powershell
& $pythonExe -m stockguidance.cli.train_model --config training-job.json `
  --artifact output/subset.sgmodel --checkpoint output/subset-training.pt > output/training-result.json
$trainingExit = $LASTEXITCODE
```

The small offline acceptance fixture is `tests/stage5/test_examples_acceptance.py`.
It trains a CPU model through the in-memory repository, reads the reported
`artifact.path` and canonical `artifact.artifact_id`, exports and reloads the
model, then simulates a strictly later range. It also checks fees, independent
staggered balances, anchored skips, end positions, exact ticker order and the
absence of future feature/label reads. It never initializes MySQL or calls a
provider.

The cross-stage Stage 10 acceptance fixture is
`tests/stage10/test_cross_stage_acceptance.py`. It keeps retained 2010 evidence
when provider access begins in 2021, repairs an in-access gap, blocks an older
gap, trains a subset, and runs a later fixed simulation without future reads.

Inspect `training_samples`, `excluded_training`, the artifact reference and
metrics. A small CPU trial should establish readiness before a long fit. The
checkpoint supports a compatible continuation; use a new fit for an independent
experiment or changed split. Training MAE is an in-sample statistic.

### Historical downloads and original decision times

**A successful download today does not make old dates strict-trainable.**
`datasets/building.py` preserves each sample's original feature-decision cutoff,
and the verified repository checks actual receipt evidence. Prices fetched in
2026 for a 2022 sample cannot prove that those exact observations were known in
2022. Moving `training_cutoff` to today only matures labels; it does not move the
feature decision to today. Fabricating earlier receipt times would hide this issue.

For strict historical evaluation, use original contemporaneous evidence and
preserved revisions. Where it is missing, samples remain excluded. For research
on a verified backfilled archive, set the training and simulation JSON field
`availability_policy` to `retrospective_daily_previous_close` and provide the
aware `data_snapshot` timestamp. This assumes each daily previous close and
mature label was available at calendar close plus the evidenced publication
delay; it never edits `available_at` or fabricates opening evidence. The policy
is rejected for `entry_open`, future/unpublished input, immature labels and
production recommendations. Reports and artifacts retain the policy, snapshot
and revisions and must state that corrected/adjusted history and the chosen
universe may contain hindsight; this is not an original point-in-time replay.

## Simulate the resulting model

For a fixed model, use a **later** period: for example, train on the illustrative
2022-2023 subset, then simulate entries from 2024-01-02 through 2024-06-28. The
model's information cutoff must precede the first simulation open and its final
training exit must precede the first simulated entry date. Save a complete
configuration from [simulation.md](simulation.md) as `simulation.json` and set:

- The same source, data paths, ordered tickers, lookback, feature order and signal
  mode as the artifact.
- `start_date` and `end_date` to the later entry range, with an explicit allocation
  `anchor_date` and either `alternate_sessions` or `staggered_halves` policy.
- `initial_capital` and `fee_per_fill` as decimal strings, plus `top_k` and the
  evidenced publication policy.
- `execution_audit_cutoff` after the receipt of the prices used for fills/marks;
  it never authorizes future signal inputs. Set `retraining` to `null`.

Use the **reported artifact identity**, not a file hash from `Get-FileHash`.
The ID hashes the canonical manifest payload and weights identity, not the
entire `.sgmodel` archive bytes.

```powershell
$trainingResult = Get-Content -Raw output/training-result.json | ConvertFrom-Json
if ($trainingResult.successful -ne $true) { throw 'Training did not produce a successful artifact' }
& $pythonExe -m stockguidance.cli.simulate --config simulation.json --model-policy fixed `
  --artifact $trainingResult.artifact.path --artifact-id $trainingResult.artifact.artifact_id > output/simulation-result.json
$simulationExit = $LASTEXITCODE
```

Simulation exits `0` for a successful complete run, `2` for invalid configuration
or an incomplete completed run, and `1` for a required-stage failure. Inspect
skips, missing fills, missing/stale marks, fees, open positions and benchmark
completeness alongside returns. Expected end-period open positions are retained.
Extend the simulated session range if you want later exits, accounting for any
new entries scheduled in that range; there is no automatic forced liquidation.

If you mean "train on a subset and simulate **those same dates**," a model fitted
using those dates would have hindsight. The fixed-model command rejects that
chronology. Use a later test range, or use `--model-policy chronological_retrain`
with the documented `retraining` configuration and `--artifacts-directory`.
That workflow fits only mature history available at each scheduled decision.
It starts with pre-period history; the global training range does not authorize
future outcomes. Repeatedly tuning against the same test range makes it a
development holdout; reserve another later period for final evaluation.

Both allocation policies buy at T's close and sell at the next trading close.
Sale proceeds rest until the next exchange session. Staggered halves have
independent balances; missed decisions never shift the anchored schedule.

## What belongs in Java?

Stage 8 now provides an opt-in Java bounded validator for the measured
raw/derived reconciliation hotspot. Database latency, round trips, parsing,
materialization and Python loops still need separate measurement.
NumPy and Torch already execute substantial work in native code. Compare a
correct batched/vectorized Python implementation with Java before choosing.

| Area | Recommendation |
|---|---|
| SQL range scans and raw-row integrity | First improve query bounds, batching and query plans; consider a streaming JDBC reader if measured CPU/allocation remains significant |
| Hashing, response parsing, row reconciliation | Candidate for a Java worker after Python/Java canonicalization and report parity tests |
| Bulk import and deterministic legacy-derived rebuild | Later candidate, requiring identical transactions, receipts, dependency invalidation and recovery |
| Repeated simulation over frozen scores | Possible later candidate if the event loop dominates; Python still owns inference/training and the semantic reference |
| Torch models, fitting and feature specifications | Keep in Python initially; do not rebuild the ML stack to optimize database work |
| Provider/history policy and exchange chronology | Keep one authoritative contract; export explicit session/policy data rather than maintaining divergent rules |

The Stage 8 validator is the only current Java implementation. The incompatible,
Maven-excluded historical prototypes were retired; recover them from Git history
only for source archaeology, not as a basis for a production port.
`stockguidance.validation.ValidatorMain` remains an opt-in, explicit-path,
read-only CLI and does not replace the Python validator. Any future Java work
starts from the Python contracts and parity tests, then requires an end-to-end
benchmark including JVM startup and transport. Python remains the default until
that work demonstrates a benefit.

## Current completion checklist

For the offline Stage 15 migration audit, use the commands, benchmark findings,
bounded shared-memory worker pipeline, and batch resume instructions in
[stage15_validation.md](stage15_validation.md). Its JSON report distinguishes
partial work from final acceptance and retains excluded archives and dependency
gaps. The validator uses Python independently of the selected Java production
z-score writer.

A normal run discovers retained history and current published coverage without
history arguments, preserves older local data outside current provider access,
reports unrecoverable gaps explicitly, and supports explicit subsets. Record the
resolved bounds, provenance, data revision, training exclusions, canonical
artifact ID and simulation configuration with each experiment. The offline
cross-stage acceptance fixture covers these handoffs without live downloads or
production repairs.
