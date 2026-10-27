# Stage 12 SQL migration: operator runbook and flow

Updated 2026-10-27. The command is
`stockguidance.cli.migrate_database`; its bounded MySQL engine is
`database/migration_sql.py`. The earlier Python whole-table copy has been
removed. The initialization appendix below is generated from the schema
initializer. Queries here show default registry names; actual identifiers are
validated and resolved from configuration. `%s` means a bound driver parameter.

## Run and resume

Source and target must be different databases on the same MySQL server. The
target execution account must have SELECT permission on the legacy tables and
DDL/DML permission on the target. Both databases must use the same table registry.
The migration requires InnoDB and verifies server UUIDs. Keep all source writers
stopped throughout the migration, including between pauses. Do not use the
unfinished target for ordinary maintenance, training, or prediction.

Use the existing verified backup manifest and export directory. Do not recreate
or edit the old journal to bypass checks. The empty target left by the failed
engine is supported: its matching `target_initialized` journal permits adoption,
but the engine refuses to adopt uncheckpointed populated tables.

From `C:\Projects\StockGuidance`:

```powershell
$python = '.venv/Scripts/python.exe'
$common = @(
  '--legacy-dbinfo-file', '.\replacement-dbinfo.id',
  '--target-dbinfo-file', '.\target-dbinfo.id',
  '--windows', '20,60',
  '--batch-size', '10000',
  '--backup-manifest', '.\output/database-simplification/stage12/backup-manifest.json',
  '--export-dir', '.\output/database-simplification/stage12/export'
)
& $python -m stockguidance.cli.migrate_database plan @common
& $python -m stockguidance.cli.migrate_database apply @common --apply-confirmation 'APPLY stock_data'
```

Run that same `apply` command to resume after fixing an error. Batch size can
change on resume because cutoffs are real keys, not offsets. Ctrl+C stops the
current attempt; its uncommitted batch rolls back. No automatic reconnection or
blind retry obscures the original error.

Console progress is flushed to stderr after each committed batch, with phase,
table, batch size, committed count, last key, elapsed time, and rate. A heartbeat
every ten seconds covers long statements and backup verification. Final JSON is
on stdout. Exit 0 means the requested phase succeeded; exit 2 reports an error.
The last printed key may precede an uncertain commit: the next attempt reads
the database checkpoint to resolve that outcome.

```powershell
# Read-only: reports database checkpoints, without scanning market values.
& $python -m stockguidance.cli.migrate_database status @common

# Independently compare source/target in bounded SQL ranges.
# Writes validation checkpoints/support metadata, but does not repair market data.
& $python -m stockguidance.cli.migrate_database validate @common
```

A fresh `validate` starts a new validation generation. If interrupted, the next
`validate` continues that generation. Fix the reported failed range before
resuming; do not change already-verified ranges behind the script.

## 1. Preflight, lock, backup, and export

The execution connection acquires the existing server-wide maintenance lock:

```sql
SELECT @@server_uuid;
SELECT GET_LOCK(%s, 5);
SELECT IS_USED_LOCK(%s) = CONNECTION_ID();
-- Bound lock name: stockguidance_daily_maintenance.
```

The same connection executes the batches and checks ownership before committing.
Source/target UUID, source semantics, registry, engines, primary-key order,
permissions, and legacy unfinished markers are checked. Planning reports
`information_schema.TABLES.TABLE_ROWS` estimates, labeled as estimates, rather
than scanning the 800-million-row z-score population for exact counts.
The source singleton is read explicitly and compared against target configuration.
Import/download marker counts are exact. Source reads never modify legacy data.

Backup files are hashed using streaming file reads. The manifest's
`restore_verified` is the operator's attestation: this command does not itself
restore a database or certify the independently restored server instance.

Retired metadata is exported before copying. Each table is read in primary-key
order with bounded queries, serialized to JSONL, atomically published, and
checksummed. A valid existing export manifest is reused. An interrupted individual
export file is rewritten on the next attempt. Exported metadata includes imports,
archive/source catalogs, old in-progress markers, quality/audit/output metadata,
verification metadata, and old migration receipts/completion. The full backup
remains the recovery source; these JSONL exports are retirement evidence.

## 2. Owned schema initialization and durable checkpoints

Initialize the target using the appendix DDL, seed the source singleton and
state dictionary, and preserve version/registry metadata. Initial DDL is guarded
by an engine-v2 ownership journal containing server UUID and registry digest.
An interrupted owned initialization can rerun CREATE TABLE/seed statements and
skip existing indexes. Unrelated partial schemas remain blocked. This does not
make MySQL DDL transactional.

The migration adds these operator control tables:

- `stage12_migration_run`: engine/run identity, server/configuration/registry
  fingerprints, fixed migration timestamp, and overall phase.
- `stage12_migration_steps`: step name, JSON last key, committed row count,
  completion flag, and optional report.
- `stage12_migration_sessions`: small calendar/receipt support table with
  prior/following session IDs and decoded receipt evidence.

All use InnoDB. The calendar/receipt support table is removed after successful
validation and rebuilt if later verification needs it. Run and step checkpoints
remain for resume and audit. Ordinary application code does not consume them.
No per-price migration dependency graph is created.

Ticker/date tables already have AUTO_INCREMENT. Explicit IDs are inserted
unchanged; the next generated values must exceed the imported maxima. A
date-leading index is added to the target delta table when absent:

```sql
CREATE INDEX stage12_delta_date_ticker
ON stock_data.daily_delta_stock_data (date_id, ticker_id);
```

## 3. Batch transaction used for copying and validation

Raw/delta keys are `(ticker_id,date_id)`; z-score keys are
`(date_id,ticker_id)`. Dictionary keys are `id`. No key is advanced by adding
10,000. An expanded lexicographic predicate handles gaps correctly.

Each iteration locks its checkpoint:

```sql
SELECT last_key, committed_rows, complete
FROM stock_data.stage12_migration_steps WHERE step = %s FOR UPDATE;
```

The server selects at most 10,000 next keys and returns only the last key and
batch count. For raw rows, the lower-bound predicate is:

```sql
WHERE s.ticker_id > %s
   OR (s.ticker_id = %s AND s.date_id > %s)
ORDER BY s.ticker_id, s.date_id
LIMIT %s
```

The actual terminal-key query wraps that bounded selection with
`COUNT(*) OVER ()` and descending key order `LIMIT 1`. Python never receives
the 10,000 numerical rows.

Copying uses both the saved lower bound and selected upper bound. For raw data:

```sql
INSERT INTO stock_data.raw_stock_data
  (ticker_id,date_id,open,high,low,close,volume,vwap,transactions,state_id,observed_at)
SELECT s.ticker_id,s.date_id,s.open,s.high,s.low,s.close,s.volume,s.vwap,
       s.transactions,COALESCE(q.state_id,1),q.observed_at
FROM stockforecasting.raw_stock_data s
LEFT JOIN stock_data.stockguidance_repair_metadata q
  ON q.date_id=s.date_id AND q.ticker_id=s.ticker_id
WHERE (s.ticker_id > %s OR (s.ticker_id = %s AND s.date_id > %s))
  AND NOT (s.ticker_id > %s OR (s.ticker_id = %s AND s.date_id > %s))
ORDER BY s.ticker_id,s.date_id;
```

The initial batch omits the lower-bound condition. Required batch counts and
raw/z-score state validity are checked before commit, so invalid unannotated
raw observations or inconsistent score/status rows fail at their own batch.
SQL warnings fail the attempt, and progress advances in the same
transaction:

```sql
UPDATE stock_data.stage12_migration_steps
SET last_key=%s, committed_rows=%s, complete=%s, report=%s
WHERE step=%s;
COMMIT;
```

There is no INSERT IGNORE or conflict-skipping upsert. Unexpected duplicates
fail. With InnoDB, a failure after 3,333 of 10,000 rows were processed leaves no
committed portion of that batch. Both data and checkpoint roll back; earlier
committed batches remain. Loss during COMMIT is resolved from the saved
checkpoint on restart, not by deleting rows or guessing from MAX(destination).

## 4. Copy order and transformations

1. Copy session/ticker dictionaries with IDs intact. New completion fields
   initially remain false/NULL.
2. Decode exceptional row-quality metadata in bounded Python batches and insert
   permanent structured repair records.
3. Copy raw prices directly in SQL, attaching state/time through the small indexed
   repair table. The join is simple enough that a separate mass UPDATE is unnecessary.
4. Copy the sixteen stored delta/label fields unchanged, attaching calculation
   status, output quality, provenance, and the fixed migration timestamp.
5. Copy z-score definitions/completion metadata and selected numerical tables.
6. Determine raw/delta session completion independently and validate everything.

Repair conversion pseudocode:

```text
for each legacy quality record:
    decode with the existing QualityRecord parser
    verify payload revision, ticker identity, session date, and endpoint IDs
    state = interpolated if effective values and interpolation flags exist; else bad
    preserve original/effective decimal strings, NULLs, fields, revision,
             endpoint values, prior/following IDs, and UTC availability times
    recorded_at = persisted migration timestamp
```

Raw state is `raw_stock_data.state_id`: 1 good, 2 bad, 3 known missing,
4 interpolated. Ordinary raw numerical fields are copied unchanged, not
substituted with decoded effective values. Effective values remain in repair
metadata and are used for independent formula checks. No new interpolation runs.
Legacy quality records identify bad/interpolated observations; absent keys do
not create missing-state rows. Unannotated invalid raw observations stop
completion processing with their session/count.

Delta metadata pseudocode:

```text
preserve all sixteen numerical columns
first trading session -> insufficient_history
missing/unusable current or prior observation -> missing_input
any NULL delta/label field -> not_yet_available
otherwise -> ready
attach output_quality['legacy-derived', date_id, ticker_id]
missing output metadata -> flags=0, gap=0, provenance={}
```

Session adjacency is built with LAG/LEAD over transaction_date. Z-score
metadata joins use their validated family/window series key. Z-score values,
statuses, and reproducibility fingerprints are copied unchanged. Definition
fingerprints are checked through the existing definition class. Small metadata
decoders normalize timezone-aware timestamps to UTC and legacy status-count
strings such as `0:2,1:3` to JSON objects. Missing quality is not independently
invented or recalculated; existing provenance is preserved.

The z-score batch uses the same bounded transaction pattern:

```sql
INSERT INTO stock_data.zscores_price_20
  (date_id,ticker_id,zscore,status,quality_flags,gap_length,provenance)
SELECT s.date_id,s.ticker_id,s.zscore,s.status,
       COALESCE(q.flags,0),COALESCE(q.gap_length,0),
       COALESCE(q.provenance,JSON_OBJECT())
FROM stockforecasting.zscores_price_20 s
LEFT JOIN stockforecasting.stockguidance_output_quality q
  ON q.series_key='<validated-series-key>'
 AND q.date_id=s.date_id AND q.ticker_id=s.ticker_id
WHERE <exclusive-lower-and-inclusive-upper-key-predicates>
ORDER BY s.date_id,s.ticker_id;
```

Repeat for price/price_delta windows 20 and 60. All existing rows, including
explicit non-ready rows, are preserved. The streamlined schema does not imply
pruning dense legacy z-score populations.

## 5. Completion and independent validation

Raw completion requires a receipt with a syntactically valid raw revision and
timezone-aware availability plus preserved source/target coverage and values,
with no unannotated invalid raw bars. Receipt hashes are retained, not rebuilt
from provider downloads; this migration establishes faithful database transfer,
not external provider completeness.

Delta completion additionally requires valid derived receipt fields and an
independent SQL reference comparison for that trading session. Effective bars
come from good/interpolated observations; preceding and following exchange
sessions supply dependencies. A bounded per-session temporary expected table
remains entirely inside MySQL.

```text
seven ratios = current.field / previous.field (NULL for absent/zero denominator)
day change = current.close / current.open
outcome = following.close / following.open, or NULL if absent
centered outcome = outcome - 1
extreme = (0.5 if outcome>=1 else 0) + outcome/(2*(outcome+1))
raise flag = outcome>=1
top flag = first 100 by descending floating outcome, ticker label breaking ties
centered labels = extreme*2-1, top_flag*2-1, raise_flag*2-1
compare exact key sets and NULLs; numeric tolerance relative 1e-10 / absolute 1e-12
```

This reference is checked against fixtures generated with the existing Python
kernel, including tied top-100 outcomes. It does not overwrite numerical values.
Unproven completion remains false and exact deferred sessions are reported for
the maintained readiness/repair workflow; migration does not download repairs.

Validation traverses bounded ranges in both directions, catching missing and
extra keys. Numerical copies use exact SQL NULL-safe comparisons:

```sql
SELECT COUNT(*)
FROM stockforecasting.raw_stock_data s
LEFT JOIN stock_data.raw_stock_data t
  ON t.ticker_id=s.ticker_id AND t.date_id=s.date_id
WHERE <bounded-source-key-range>
  AND (t.ticker_id IS NULL OR NOT (s.open <=> t.open) OR <other-field-differences>);
```

The actual comparison covers every copied field, including states, timing,
delta status, and attached quality. Provenance JSON compares structurally.
Decoded repair/definition/session metadata is compared in bounded Python
metadata batches. Session completion flags/times are independently recomputed
and compared without repairing them. Z-score completion counts/status
distributions are checked against the actual target table using indexed
date-specific aggregates. No whole numerical table is fetched into Python.

At most twenty mismatch keys are printed per failed SQL comparison. Each
validated range checkpoints transactionally; an interrupted validation resumes.
Final JSON reports exact copied/validated counts and deferred completion.
Successful apply ends with `phase: validated`.

## 6. Cutover, recovery, and rehearsal

Review deferred completion, run the normal independent target validator, and
check representative existing training/prediction jobs before cutover. Unknown
coverage must be resolved through maintained target readiness; it is never
silently declared complete by migration.

Keep target values unchanged until migration cutover is recorded: the cutover
comparison deliberately requires a faithful copy of legacy values. Use known
complete ranges and read-only computations for representative checks. Run
deferred repairs through normal target maintenance after the explicit cutover,
then verify readiness for the requested training/prediction ranges. Do not let
an automatic training preparation step modify the target during copy acceptance.

```powershell
& $python -m stockguidance.cli.migrate_database cutover @common --cutover-confirmation 'CUTOVER stock_data'
```

Cutover revalidates under the writer lock, durably records confirmation, and
writes the version/registry cutover marker. The operator switches active dbinfo;
the tool does not rewrite credentials/configuration. Subsequent apply is blocked.
Retirement remains a separately confirmed destructive operation and is not
needed to finish the additive copy. DDL cannot be undone by ordinary rollback.
Keep the legacy database/configuration for rollback until retirement; afterward
restore the verified backup into a separate database and repoint configuration.

Rehearse with NEW disposable names:

```powershell
& $python tools/database_simplification_stage12.py `
  --dbinfo-file .\replacement-dbinfo.id `
  --prefix stockguidance_disposable_stage12_my_new_trial `
  --output-dir .\output/database-simplification/stage12-my-new-trial
```

The rehearsal never selects the configured production database. It creates and
retains two explicitly prefixed disposable databases. Output contains private
fixture dbinfo files; keep it ignored/private. The synthetic backup manifest in
this test does not establish real backup/restore usability. The production-scale
runtime/storage cost must be observed during operator execution; small warm
fixture throughput is not a production estimate.

## Appendix: actual target initialization DDL

The following is generated by `simplified_schema_statements(load_table_names(),
(20, 60))` from the inspected checkout. It is recorded for review, not an
instruction to execute DDL manually against the interrupted target.

```sql
CREATE TABLE IF NOT EXISTS `stockguidance_schema_version` (id TINYINT UNSIGNED NOT NULL PRIMARY KEY, schema_name VARCHAR(64) NOT NULL, schema_version INT UNSIGNED NOT NULL, registry_version INT UNSIGNED NOT NULL, registry_digest CHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL, CHECK (id = 1)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_source` (id INT UNSIGNED NOT NULL PRIMARY KEY, configuration_key CHAR(64) NOT NULL, source_key CHAR(64) NOT NULL, source_family VARCHAR(64) NOT NULL, endpoint VARCHAR(512) NOT NULL, market VARCHAR(64) NOT NULL, universe VARCHAR(128) NOT NULL, interval_code VARCHAR(8) NOT NULL, calendar_id VARCHAR(32) NOT NULL, raw_price_split_adjusted TINYINT(1) NOT NULL, raw_price_dividend_adjusted TINYINT(1) NOT NULL, raw_price_vendor_defined_adjustment VARCHAR(128) NULL, created_at DATETIME(6) NOT NULL, CHECK (id = 1)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_data_states` (id SMALLINT UNSIGNED NOT NULL PRIMARY KEY, code VARCHAR(16) NOT NULL UNIQUE, description VARCHAR(255) NOT NULL) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `transaction_dates` (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE, raw_complete TINYINT(1) NOT NULL DEFAULT 0, raw_available_at DATETIME(6) NULL, raw_completed_at DATETIME(6) NULL, raw_revision CHAR(64) NULL, delta_complete TINYINT(1) NOT NULL DEFAULT 0, delta_completed_at DATETIME(6) NULL, CHECK (raw_complete IN (0, 1)), CHECK (delta_complete IN (0, 1))) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `ticker_symbol` (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL UNIQUE, exchange VARCHAR(10) NULL) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `raw_stock_data` (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, open DECIMAL(28,8) NULL, high DECIMAL(28,8) NULL, low DECIMAL(28,8) NULL, close DECIMAL(28,8) NULL, volume DECIMAL(20,8) NULL, vwap DECIMAL(42,22) NULL, transactions BIGINT UNSIGNED NULL, state_id SMALLINT UNSIGNED NOT NULL, observed_at DATETIME(6) NULL, PRIMARY KEY(ticker_id, date_id), CONSTRAINT simplified_prices_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id), CONSTRAINT simplified_prices_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_prices_state_fk FOREIGN KEY (state_id) REFERENCES `stockguidance_data_states`(id)) ENGINE=InnoDB;

CREATE INDEX simplified_prices_date_ticker ON `raw_stock_data` (date_id, ticker_id);

CREATE TABLE IF NOT EXISTS `daily_delta_stock_data` (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, open_ratio_to_prior DOUBLE NULL, high_ratio_to_prior DOUBLE NULL, low_ratio_to_prior DOUBLE NULL, close_ratio_to_prior DOUBLE NULL, volume_ratio_to_prior DOUBLE NULL, vwap_ratio_to_prior DOUBLE NULL, transactions_ratio_to_prior DOUBLE NULL, change_in_price_over_day DOUBLE NULL, next_day_change_in_price DOUBLE NULL, zero_centered_next_day_price_change DOUBLE NULL, next_day_price_change_to_extremes_0_1 DOUBLE NULL, next_day_raise1_lower0 DOUBLE NULL, next_day_top100_raise1_lower0 DOUBLE NULL, zero_center_extremes_neg1_pos1 DOUBLE NULL, zero_center_top100_1_other_neg1 DOUBLE NULL, zero_center_raise1_lower_neg1 DOUBLE NULL, calculation_status VARCHAR(32) NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, completed_at DATETIME(6) NOT NULL, PRIMARY KEY(ticker_id, date_id), CONSTRAINT simplified_deltas_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id), CONSTRAINT simplified_deltas_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_repair_metadata` (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, revision CHAR(64) NOT NULL, state_id SMALLINT UNSIGNED NOT NULL, original_values JSON NOT NULL, effective_values JSON NULL, invalid_fields JSON NOT NULL, interpolation_method VARCHAR(64) NULL, prior_date_id INT UNSIGNED NULL, following_date_id INT UNSIGNED NULL, endpoint_values JSON NULL, observed_at DATETIME(6) NULL, available_at DATETIME(6) NULL, endpoints_available_at DATETIME(6) NULL, recorded_at DATETIME(6) NOT NULL, UNIQUE KEY simplified_repair_revision (date_id, ticker_id, revision), CONSTRAINT simplified_repairs_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_repairs_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id), CONSTRAINT simplified_repairs_state_fk FOREIGN KEY (state_id) REFERENCES `stockguidance_data_states`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_pending_updates` (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, operation_kind VARCHAR(32) NOT NULL, scope_version SMALLINT UNSIGNED NOT NULL, scope JSON NOT NULL, created_at DATETIME(6) NOT NULL, clearing_started_at DATETIME(6) NULL, affected_rows_cleared TINYINT(1) NOT NULL DEFAULT 0, failure_code VARCHAR(64) NULL, failure_detail TEXT NULL, failed_at DATETIME(6) NULL, CHECK (affected_rows_cleared IN (0, 1)), CHECK (operation_kind IN ('initial_import', 'forced_replacement', 'derived_repair', 'recovery')), CHECK (scope_version = 1)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_zscore_series` (series_key CHAR(64) NOT NULL PRIMARY KEY, schema_version INT NOT NULL, source_scope CHAR(64) NOT NULL, calendar_id VARCHAR(128) NOT NULL, family VARCHAR(32) NOT NULL, `window` INT NOT NULL, calculation_version VARCHAR(128) NOT NULL, delta_definition VARCHAR(128) NOT NULL, `precision` VARCHAR(32) NOT NULL, missing_value_policy VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL, UNIQUE KEY simplified_series_definition (source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `stockguidance_zscore_sessions` (series_key CHAR(64) NOT NULL, date_id INT UNSIGNED NOT NULL, dependency_digest CHAR(64) NOT NULL, availability_digest CHAR(64) NOT NULL, actual_input_available TINYINT(1) NOT NULL, completed_at DATETIME(6) NOT NULL, row_count INT NOT NULL, ready_count INT NOT NULL, status_counts JSON NOT NULL, content_digest CHAR(64) NOT NULL, PRIMARY KEY (series_key, date_id), CONSTRAINT simplified_zscore_sessions_series_fk FOREIGN KEY (series_key) REFERENCES `stockguidance_zscore_series`(series_key), CONSTRAINT simplified_zscore_sessions_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `zscores_price_20` (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, PRIMARY KEY (date_id, ticker_id), CONSTRAINT simplified_price_20_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_price_20_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `zscores_price_delta_20` (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, PRIMARY KEY (date_id, ticker_id), CONSTRAINT simplified_price_delta_20_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_price_delta_20_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `zscores_price_60` (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, PRIMARY KEY (date_id, ticker_id), CONSTRAINT simplified_price_60_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_price_60_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id)) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `zscores_price_delta_60` (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, PRIMARY KEY (date_id, ticker_id), CONSTRAINT simplified_price_delta_60_date_fk FOREIGN KEY (date_id) REFERENCES `transaction_dates`(id), CONSTRAINT simplified_price_delta_60_ticker_fk FOREIGN KEY (ticker_id) REFERENCES `ticker_symbol`(id)) ENGINE=InnoDB;
```
