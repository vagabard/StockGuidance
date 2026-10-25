# Daily maintenance

Run from the repository root after installing the Python package with database
dependencies and building Java 26:

```powershell
uv --native-tls pip install --python .venv/Scripts/python.exe -e '.[database]'
mvn -f java/pom.xml package -DskipTests
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily
```

The default command uses `dbinfo.id`, `data`, the initialized database source
configuration and its table-name registry. Set `MASSIVE_API_KEY` or
`POLYGON_API_KEY`; the existing configured credential fallback remains available.
The grouped-daily Massive/Polygon adapter is shared with the other update CLIs.
Java classes and dependencies must be available under `java/target`.

The implementation keeps one `maintain_daily` command but separates the workflow
internally into context/setup, source downloading, Java materialization, and Python
validation modules. The `--stage` option selects the corresponding module for a
focused rerun; the default command preserves the source-to-Java-to-validation
handoff and validates each Java batch before continuing.

## Scope and defaults

The repair window starts two calendar years before the current UTC date. Each
run enumerates exchange trading days, excludes holidays and unpublished sessions,
and includes the history and neighboring sessions needed for derived rows and
60-session z-scores. There is no dependency on yesterday's run succeeding.
Dates before the operational validity threshold, 2006-11-16, cannot be requested.
Required warm-up observations must still be available; missing data is never
interpolated or zero-filled.

Defaults are 15 minutes after the exchange close for publication, a 30-day
recent correction window, refresh on every invocation for recent sessions,
and a 30-day refresh interval for older sessions. Publication and correction
evidence flags remain overridable operational assumptions. Market adjustment
semantics come from the database; a conflicting explicit override is rejected.

Java uses a 4 GiB maximum heap, three calculation workers and one publisher,
with batches of 20 target sessions. The independent z-score validator uses five
processes, vector batches of 256 tickers and database fetches of 1,000 records.
The source phase holds one downloaded session in memory. Shared Java and NumPy
database copies are bounded to the current batch and its dependencies.

For debugging or a controlled rerun, execute one phase at a time:

```powershell
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily --stage source
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily --stage java
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily --stage validation
```

`source` downloads daily NDJSON files and recovers interrupted raw imports.
`java` discovers completed database sessions, imports pending NDJSON files, and
calculates daily deltas and z-scores from database values. Completed historical
sessions do not require their NDJSON files to be present. `validation` discovers
completed series directly from database metadata and runs independent Python
checks; it does not require workload files or previous reports.
Use the same date, policy, checkpoint and batch arguments for all three
commands. The default, with no `--stage`, runs all three phases. Numeric aliases
`1`, `2` and `3` are also accepted for source, Java and validation respectively.

For a bounded repair, override dates:

```powershell
& .venv/Scripts/python.exe -m stockguidance.cli.maintain_daily `
  --start-date 2025-11-07 --end-date 2025-11-10
```

`--java-heap`, `--workers`, `--batch-sessions`, `--timeout-seconds`, paths and
refresh/publication policies can be overridden. Schema creation is a separate
setup operation. Maintenance fails before downloading if required tables or
Java classes are unavailable.

## Writes and validation

Database import receipts are authoritative for completed raw and daily-delta
sessions. Java queries receipts in batches, checks filenames and file metadata,
and parses only new, explicitly refreshed, or changed NDJSON inputs. File size
and modification time are recorded in `stockguidance_daily_source_files` in the
raw import transaction. Existing receipts without file metadata are trusted and
receive a metadata baseline without decoding the file. Missing dates are checked
against exchange sessions, rather than inferred solely from the latest date.
The source stage records a pending-download flag in that table before publishing
a refreshed file, so a separate Java invocation still imports the refresh even
when the old receipt has no file metadata. Import completion clears the flag.

Java transactionally reconciles pending raw rows, including removal of tickers
withdrawn by corrections. Daily deltas use exact decimal raw values read from
the database in bounded batches. Changed raw data invalidates neighboring delta
receipts and affected z-score completion receipts. Unchanged completed deltas
and z-score sessions are reused without historical content comparisons. A
changed ticker count also prevents reuse of old z-score completion metadata.
Missing inputs fail with the session dates and a request to run the source stage.

Normal Java runs trust completed history. They do not detect arbitrary historical
database damage or file edits preserving both size and modification time.
Independent validation remains available; a force-verification option is deferred.

Operational raw record identities use the same version 2 canonical decimal
contract as migration. OHLC values retain eight decimal places and VWAP retains
22; HTTP JSON decimals are parsed without a binary floating-point round trip.
The daily-maintenance source stage retains NDJSON downloads without creating
duplicate response JSON or per-session manifest copies. Shared manifest storage
used by other workflows remains supported. Existing downloaded NDJSON files are
preserved. Historical files are not recreated merely because they are absent if
their database session is complete and no source refresh is due.

Each Java batch is followed by independent Python numerical, status, and
metadata checks for the materialized z-scores. These checks use a fresh
read-only snapshot on every invocation. A correctly stored
non-ready z-score is valid materialization evidence, but remains unavailable for
training; the existing range validity guardian must still warn about selected
non-ready observations. Successful maintenance does not make every ticker valid
on every date.

## Recovery and readiness

Retained NDJSON downloads and database completion receipts survive interruption.
Java records a raw date as in progress before writing it, then removes that marker
in the same transaction that commits the raw rows, receipt and file metadata.
The source stage removes incomplete raw/delta data for remaining markers; Java
then imports the pending download. A Java-only run encountering an unfinished
import requests source-stage recovery and does not delete data itself.

Workload descriptions, pipeline intermediates, validation details and checkpoints
are temporary files inside owned `run-*` subdirectories of
`--java-checkpoint-directory`. Successful runs remove their temporary files and
failed directories for the same stage, database configuration, tables and date
range. Failed runs retain diagnostics until a successful retry. Checkpoints never
override database completion. NDJSON downloads are never included in this cleanup.
Validation snapshots are fresh. An advisory database lock prevents concurrent
maintenance coordinators.

Version 2 operational workloads use database inputs. Version 1 archive workloads
remain supported by Java for compatibility, as do migration and benchmark inputs.
New operational runs generate their workloads afresh. Legacy workload/report files
and duplicate response/manifest files are not automatically deleted; other
workflows may still use them. They are no longer required by daily maintenance.

Before using this version on an existing database, run the existing explicit
updater schema initialization (`initialize_update_schema`, also exposed by the
updater's `--initialize-schema` option). It creates the missing
`stockguidance_daily_source_files` and `stockguidance_daily_import_in_progress`
tables without altering existing tables. Operational runs check these tables and
do not create or alter schema automatically.

Progress and phase estimates appear on stderr; stdout contains the final JSON
report. Exit 0 means the requested range passed both validators. Nonzero means
the range is not certified for downstream use; inspect the reported findings and
the evidence under `output/daily-maintenance` before rerunning.

The report identifies the latest input session for forecasting and the last
session whose next-day training outcome is available. The latest published day's
forward outcome remains null until the next session publishes. Maintenance
prepares data; model training and forecast generation remain explicit commands
with their own model/universe configuration. It does not certify older history
outside its requested range or populate optional entry-open observations.

The automated MySQL integration test uses synthetic provider responses, a
temporary dedicated schema and the real Java writers and Python validators:

```powershell
$env:STOCKGUIDANCE_MYSQL_INTEGRATION = '1'
& .venv/Scripts/python.exe -m pytest tests/workflows/test_daily_maintenance.py -q
```

It checks initial population, repeat runs and a source correction. The temporary
schema is removed in the test's cleanup; the production schema is not modified.
