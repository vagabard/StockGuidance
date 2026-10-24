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
internally into context/setup, source archival, Java materialization, and Python
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

`source` downloads and archives the requested sessions. `java` requires those
archive files, materializes the database and saves one Java report beside each
workload under `output/daily-maintenance/java`. `validation` requires the
workloads and reports from `java` and runs only the independent Python checks.
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

Existing daily-maintenance archive files are treated as usable inputs without
reloading and decoding their provider-response manifests. Existing database
import receipts supply the source identity and availability for those sessions.
Missing files are downloaded and archived. Java transactionally reconciles raw
rows, including removal of keys withdrawn by a corrected response, and repairs
derived values from chronological neighbors. Source changes invalidate
dependent z-score completion receipts. The Java pipeline recomputes requested
z-score dependencies and preserves matching output rows; rechecking dependencies
also repairs deleted or corrupted z-score materializations.

Operational raw record identities use the same version 2 canonical decimal
contract as migration. OHLC values retain eight decimal places and VWAP retains
22; HTTP JSON decimals are parsed without a binary floating-point round trip.
Older version 1 source manifests are bypassed when their daily-maintenance
archive already exists. If that archive is missing, maintenance recreates it
from a current provider response.

Each Java batch is followed by independent Python numerical, status, and
metadata checks for the materialized z-scores. These checks use a fresh
read-only snapshot on every invocation. A correctly stored
non-ready z-score is valid materialization evidence, but remains unavailable for
training; the existing range validity guardian must still warn about selected
non-ready observations. Successful maintenance does not make every ticker valid
on every date.

## Recovery and readiness

Downloaded responses, generated workload descriptions and per-session committed
receipts survive interruption. Java records each raw date as in progress before
writing it and removes the marker after commit. On restart, maintenance deletes
raw and derived rows and the import receipt for marked dates, then imports them
again from the retained archive. Workload identities prevent stale checkpoints
from being reused after source changes. Validation snapshots are fresh so an old
passing report cannot hide subsequent database damage. An advisory database
lock prevents concurrent maintenance coordinators.

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
