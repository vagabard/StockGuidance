# Retained Massive archive: initial migration results

Measured on 2026-10-19 with Python 3.14.8 and OpenJDK 26.0.2.1, using the
supported Java 21-targeted build. This completes the initial read study and
sample audit, not the production migration. Continue with the numbered
[migration prompts](legacy_migration_prompts.md).

## Java versus Python

The fixed workload contained 25 files and 217,465 records: 24 random sessions
stratified across the archive, plus its largest file. Each configuration ran
one first invocation and three warm repetitions. Java and Python ran sequentially,
with one or three application workers; Java used threads and Python processes.
Native-library threads were limited to one; Java used a 512 MiB heap and serial
GC. JVM/OS housekeeping threads are outside the application-worker count.

Warm median task wall time, seconds:

| Workload | Java: 1 thread | Java: 3 threads | Python: 1 process | Python: 3 processes |
|---|---:|---:|---:|---:|
| Read, decode and hash files | 1.88 | 1.35 | 11.20 | 4.17 |
| Read, decode and hash SQL rows | 33.57 | 13.30 | 44.01 | 16.24 |
| Read files and SQL, reconcile digests | 38.41 | 13.76 | 57.83 | 20.03 |

All canonical results matched across languages, worker counts and repetitions;
the final check also matched shared file/database identities across workloads.
The SQL plan used `fk_stock_date` to select a single date ID, then ticker primary
key lookups and a bounded label sort. Python used unbuffered 1,000-row batches;
JDBC used cursor fetching. There were no production writes.

Three-worker reconciliation was about 2.79x faster than one worker in Java and
2.89x in Python. Java's best warm read/reconcile result was about 1.46x Python's
best result. Sampled peak process-tree RSS for that configuration was about
602.5 MiB in Java versus 263.5 MiB in Python, including runtime overhead.

The provisional choice is **Java with three reader workers**. This does not
select a Java writer or establish derived-computation parity. First-run timings
are not guaranteed OS-cache cold. Full process totals include startup and all
four repetitions and are retained in the JSON; they are not single-task cold
latencies. CPU collection differs between worker and JVM process accounting,
so wall time drives this provisional decision. Exact SQL query-count
instrumentation and the Stage 2b derived study are now complete. Stage 2c remains
skipped because no Java writer was proposed. Each reader used a separate
read-only transaction; identical content was observed, not enforced through a
global shared snapshot or production writer lock.

## Stage 2b derived-computation parity

The Stage 2b study ran on 2026-10-19 against the exact 25 paths selected by
Stage 2a, with each central session joined to its immediate retained neighbors.
It used four repeats (first plus three warm) and the required alternating order:
Java then Python at one worker, Python then Java at three workers. Each worker
used a separate read-only `REPEATABLE READ` transaction; the database was
required to remain idle and there was no shared snapshot. Java used `-Xmx512m`,
serial GC, `ActiveProcessorCount=3`, and one native-library thread. No writes,
receipts, schema changes or writer timing were performed.

The workload queried date mappings, raw rows and persisted derived rows through
bounded one-target `date_id IN (...)` scopes. All 25 central sessions had raw
context and were machine-readable as complete. The four runs produced identical
canonical digests (`parity: true`); this is language/worker/repeat parity, not
an assertion that the existing persisted derived values are correct.

| Backend | Workers | First task (s) | Warm median (s) | Process total (s) | Peak tree RSS (MiB) | Warm queries | Raw rows | Derived rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Java | 1 | 107.191 | 99.784 | 406.792 | 615.2 | 77 | 651,812 | 650,818 |
| Python | 1 | 173.309 | 173.765 | 695.011 | 166.6 | 77 | 651,812 | 650,818 |
| Python | 3 | 107.957 | 108.100 | 422.248 | 479.5 | 81 | 651,812 | 650,818 |
| Java | 3 | 80.197 | 78.569 | 314.752 | 606.8 | 81 | 651,812 | 650,818 |

Derived persistence parity did not pass the existing `1e-10` relative / `1e-12`
absolute tolerance: zero of 25 sessions matched all columns. The study found
217,055 stored rows versus 217,467 expected rows, with 412 missing keys and no
extra keys. The existing values show schema/storage rounding effects, including
four-decimal persisted ratios; the study reports these findings rather than
normalizing them away. Aggregate differing-column counts were:

| Column | Differences | Column | Differences |
|---|---:|---|---:|
| `open_ratio_to_prior` | 202,407 | `high_ratio_to_prior` | 202,065 |
| `low_ratio_to_prior` | 201,899 | `close_ratio_to_prior` | 203,341 |
| `volume_ratio_to_prior` | 210,763 | `vwap_ratio_to_prior` | 211,170 |
| `transactions_ratio_to_prior` | 194,088 | `change_in_price_over_day` | 194,533 |
| `next_day_change_in_price` | 196,615 | `zero_centered_next_day_price_change` | 199,860 |
| `next_day_price_change_to_extremes_0_1` | 185,100 | `next_day_raise1_lower0` | 5,132 |
| `next_day_top100_raise1_lower0` | 7,035 | `zero_center_extremes_neg1_pos1` | 192,797 |
| `zero_center_top100_1_other_neg1` | 7,035 | `zero_center_raise1_lower_neg1` | 5,132 |

The report and per-run JSON files are under the intentionally ignored
`output/legacy-migration/benchmark/derived/` directory. The completed report
SHA-256 is `adb098d4afebc186484553500d4b241301296e8be821293207b9d166fc98bf7e`.
The bounded query count is 77 per repeat for one worker and 81 per repeat for
three workers. Stage 2c was not executed because no Java writer was proposed;
the provisional Stage 3 reader choice remains unchanged.

Reproduction command, using the configured Python executable and existing
Stage 2a report:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $pythonExe -m stockguidance.cli.archive_study `
  --derived-only `
  --study-report output/legacy-migration/benchmark/study.json `
  --output output/legacy-migration/benchmark/derived `
  --dbinfo-file dbinfo.id `
  --java-classpath 'StockGuidance/java/target/classes;StockGuidance/java/target/dependency/*;StockGuidance/java/lib/mysql-connector-j-9.6.0.jar'
```

Artifacts, intentionally ignored by Git:

- `output/legacy-migration/benchmark/study.json`: complete read measurements.
- `output/legacy-migration/benchmark/summary.md`: readable full measurement table.
- `output/legacy-migration/decision.json`: provisional reader choice and gates.

## Source acceptance and stock-table differences

Nine randomly selected files, three per chronological third with seed 20261019,
passed complete record/timestamp/calendar checks: **77,783 records**. Java
independently matched every file hash, normalized record digest and count.
Under the user's stated policy, their Massive provenance is accepted and recorded
as operator-attested retained-archive evidence. Original download receipt times
remain unknown; other files have not been adopted merely because this sample passed.

Read-only comparison of these nine sessions found:

- 3,597 archive keys missing from the resolved database rows and 3,490 extra keys.
- 6,682 shared-key rows with differing values; all included a volume difference.
- 24 shared-key rows with open-price differences; other OHLC/VWAP/count fields
  also differ in some rows.
- The source sample contains 6,710 fractional volumes and one record whose
  prices exceed the legacy DECIMAL(19,4) range.

Live column inspection confirmed the ticker unique key is case-insensitive
(`utf8mb4_0900_ai_ci`). Exact source keys such as AAP and AAp cannot coexist
under that constraint. Uppercase legacy aliases explain many key differences;
conflated raw values and range/rounding damage require explicit investigation
and repair from the accepted archive. A case-folded comparison would conceal
these findings.

The sample comparison assesses the nine selected sessions only; it does not
claim a full-archive or global-orphan audit. No source manifests, import receipts,
verification-table rows, raw/delta rows or production schemas were changed.

Evidence is in `source-acceptance.json`, `source-java-comparison.json`,
`sample-database-comparison.json` and `ticker-identity-diagnostic.json` under
`output/legacy-migration/`.

## Next prompts and checks

Stage 2b is complete; proceed to the final implementation choice, then Stage
5a0's ticker identity inventory and Stage 5a's numeric precision
inventory/contracts.
Stage 2c is required only if proposing a Java writer. Source sampling in Stage 4
is already complete; do not repeat it or substitute different files to hide
findings. Population, receipts, collision repairs and 20/60 z-scores remain
Stages 5–10, behind the tested-backup and bounded-pilot gates.

Validation: 25 focused Python tests passed, including cross-language fixtures,
decimal volumes, large adjusted prices, worker limits, cross-workload drift,
rollback and read-only sample comparison. All 31 Java tests and offline packaging
passed. Full legacy Python/ML suites were not run. The JDBC encoding setting was
corrected from a MySQL charset name to Java's UTF-8 encoding; the live read study
then exercised the connection successfully. No credentials are stored in these
reports or subprocess arguments.

Stage 2b validation added derived mismatch/neighbor tests; the targeted Python
archive suite passed 23 tests and the Java suite passed 32 tests before the live
study. The Stage 2b command was the `archive_study --derived-only` invocation
using `output/legacy-migration/benchmark/study.json` as its Stage 2a input.
