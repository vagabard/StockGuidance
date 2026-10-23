# Stage 15 independent Python validation

The validator uses its own Python semantic reference. The selected Java
production writer remains the Stage 14 writer; Stage 15 opens one read-only
repeatable-read database transaction and validates the retained snapshot.

## Run, bound, and resume

Use the configured Python interpreter and this checkout's `src`:

```powershell
$pythonExe = (Get-ChildItem 'C:/Users/formm/AppData/Local/Python/pythoncore-3.14-64/python.exe').FullName
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
  --run-stage15 --data-root data --dbinfo-file dbinfo.id `
  --stage15-workers 5 --stage15-zscore-batch-size 256 `
  --stage15-fetch-size 1000 --stage15-sessions-per-batch 128 `
  --stage15-cache-slots 2 --stage15-progress-seconds 10
```

Add `--stage15-max-batches 157` for a bounded invocation. The bound limits new
z-score ticker batches per invocation, and new raw/derived session tasks when
those phases need work. An intentional bounded stop returns `status=partial`
and exit zero; it does not establish final acceptance. A completed audit with
failed value/status acceptance returns exit two; setup or execution errors return
exit one. Omit the bound on the next
invocation to finish. A completed z-score batch is a session block crossed with
up to 256 tickers. A completed raw/derived task covers one session. Existing
valid checkpoints do not consume the new-work limit.
When a block needs fewer than half of the mapped tickers, database reads are
restricted to that pending subset, including resumes at nonzero ticker offsets.

The final report is `output/legacy-migration/final-audit.json`. Checkpoints are
under `stage15-checkpoints/`, `stage15-raw-checkpoints/`, and
`stage15-derived-checkpoints/` beside it. They are atomically published and
checksummed. Their identities bind prerequisite hashes, receipt/mapping revision,
date/ticker maps, evidence, engine version, and, for z-scores, batch geometry.
Changing worker count or fetch size retains z-score checkpoints; changing vector
size or sessions per batch creates a new checkpoint identity. An interrupted
in-flight batch is repeated; completed batches survive. Preserve these directories
and the prerequisite artifacts. `--stage15-no-resume` ignores the top-level
report; use a different output directory for an entirely fresh batch audit.

Resume requires unchanged source files and migration receipts/mappings. The
revision gate relies on the migration's controlled-writer/receipt contract; it
does not detect arbitrary out-of-band edits that bypass that contract. Final
support-table counts, mapping checks, and orphan checks remain required before
the aggregate report can claim completion. Source hashing and final integrity
queries can take time before/after the worker progress display.

## Pipeline and independence

One loader fetches up to 1,000 records at a time from six bounded queries per
session block: raw close, stored delta ratio, and the four z-score tables.
These are streaming fetch sizes, not SQL `LIMIT/OFFSET` pagination. The date
predicate bounds each query. Each block includes the authoritative dependency
halo, including missing/unverified sessions, and all mapped tickers.

The loader writes NumPy arrays in a bounded shared-memory cache. With the current
40,066-ticker mapping and defaults, the full run allocated about 552 MiB. It
publishes work through a process-safe queue only after the entire cache slot is
filled. Workers wait on that queue with timeouts, so they can start before their
inputs are available. Five spawned processes read published slots concurrently;
they calculate across ticker vectors and independently check both families and
windows, exact statuses, null scores, and float32 score tolerances. Small-variance
cases retain a scalar `math.fsum` fallback. The Python validator does not call the
production vectorized calculator or the Java writer's formulas.

Workers put results on a bounded queue. The main process is the sole consumer
and checkpoint writer. It returns a cache slot to the loader only after every
reader of that slot has finished. Queue publication and slot acknowledgements
provide synchronization; one writer alone would not prevent readers observing
partially populated arrays. Bounded slots avoid copying the entire historical
database into each worker or retaining all materializations in RAM.

Raw validation preserves exact Decimal values and archive hashes with the
migration's `read_exact_archive` contract, without rounding database values.
Its session
tasks run in separate processes. Derived validation reads bounded stored session
data, recomputes the independent Decimal semantic reference from retained
archives using `_exact_derived_rows`, and vectorizes comparison of all sixteen
derived columns. Its top-100
ranking still spans the whole session. Both phases retain complete session
findings and support session checkpoints. Exact-format raw checkpoints can be
reused; the earlier scalar audit's rounded raw results are deliberately
invalidated. An empty derived result list is never accepted as coverage.

The progress display reports completed/total batches, rate, loaded source rows,
elapsed time, and estimated remaining z-score time. It waits for one complete
session block before projecting ETA; estimates change with loading throughput.
If a source query or worker takes substantially longer than the measured block
rate, the display switches to `re-estimating` instead of repeating an optimistic
near-zero estimate.
Raw/derived progress reports sessions and their own ETA. The z-score ETA excludes
subsequent derived and global integrity phases.

Global integrity compares exact full-table counts with the independently
validated complete key sets. Equality plus successful key/value validation proves
there are no extra date/ticker keys, so clean tables do not need a redundant
full-table orphan join. The raw/derived subset relation also proves the number of
raw keys without derived neighbors. Count drift, missing validation, or failed
comparisons trigger SQL orphan scans instead. `database.integrity.scope_proofs`
records which method established each result; failures are never assumed away.
Previously completed SQL integrity checks can also be resumed when the report's
database revision and every exact table count are unchanged; reused checks are
listed explicitly. New checks and any changed count/revision require validation.

Final metadata validation independently counts statuses and checks series/source
definitions, row counts, ready counts, and unavailable-score nulls. Publication,
dependency, and availability digests must also match the frozen Stage 14 report
bound by the prerequisites. Those digest comparisons reuse publication evidence;
the Python workers independently recompute values and statuses from source rows.

## Recorded comparison — 2026-10-23

Run the reproducible bounded benchmark:

```powershell
& $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
  --run-stage15 --stage15-benchmark --data-root data --dbinfo-file dbinfo.id
```

`output/legacy-migration/stage15-benchmark.json` compares the original scalar
semantic calculation, the independent NumPy calculation, and five spawned
processes on the same real database cache. This single invocation covered 128
sessions by 256 active tickers, both families, and windows 20/60 (131,072 checks).

| Measurement | Seconds |
| --- | ---: |
| Shared source loading, reported separately | 0.591 |
| Original scalar reference | 4.033 |
| Independent NumPy calculation, one process | 0.140 |
| Five processes, including spawn and result transport | 0.492 |

Counts and findings agreed across all three implementations, with zero
mismatches. Calculation speedups were 28.75x for NumPy and 8.20x for the five
process run relative to scalar. On this small batch, single-process NumPy was
faster than multiprocessing because of startup overhead. This is a calculation
comparison with a shared source read, not an end-to-end database speedup claim.
It excludes worker shutdown. The first full-width production-read block loaded
24,342,767 records and saved 157 checkpoints in 47 seconds; the full-scope run
resumed those checkpoints successfully. Final full-scope acceptance is recorded
only by `final-audit.json`, independently of benchmark success.

## Production decision and remaining evidence

The frozen Stage 10 decision measured all three candidates (startup-inclusive
warm median seconds):

| Workload | Python | Java | SQL | Selected |
| --- | ---: | ---: | ---: | --- |
| Raw adoption/repair | 11.311 | 10.364 | 21.653 | Python p3 |
| Derived computation/persistence | 46.679 | 41.139 | 57.522 | Java p1 |
| Z-score computation/persistence | 402.762 | 287.868 | 1006.039 | Java p1 |

Java raw improvement did not meet the required 10% threshold. SQL did not qualify
for any workload; no SQL candidate routine/loader was selected for production.
The production raw migration uses price `DECIMAL(28,8)`, volume `DECIMAL(20,8)`,
VWAP `DECIMAL(42,22)`, binary ticker collation, and InnoDB. These migration gates,
Stage 11 recovery tests, and the Stage 12 verified restore/idempotent pilot remain
prerequisites; the final audit reports actual schema and support/materialization
counts rather than manufacturing recovery writes.

The Stage 13/14 retained scope has 4,898 valid archive sessions and 232 excluded
invalid files. Five targets lack the complete derived neighborhood; four z-score
tables each contain 196,243,268 rows across 40,066 tickers. The final audit retains
all excluded files and unavailable dependencies. It cannot claim continuous
verified coverage while gaps remain. Archive adoption proves the local snapshot
at the recorded cutoff and does not establish original historical availability.
Live refresh policy and model training/simulation remain separate workflows.

The two early unavailable targets, `2006-07-03` and `2006-11-16`, remain
unavailable retained-data gaps. The three recent targets are controlled refresh
candidates for the daily maintenance frontend: refresh `2025-11-06` and
`2025-11-07`, `2025-11-10` and `2025-11-11`, and `2026-05-06` and `2026-05-07`.
After source reconciliation, rebuild derived rows and dependent z-scores, then
rerun the bounded Stage 15 audit; do not edit the frozen audit report manually.

## Completed full-scope audit — 2026-10-23

`final-audit.json` is `status=complete` and `acceptance.audit_passed=true`.
Exact adopted raw agreement covers 4,898 sessions and 42,482,212 rows. All 4,893
eligible derived sessions passed across 42,431,753 rows; five incomplete
neighborhoods are explicitly unavailable. All 6,123 z-score batches completed,
covering 196,243,268 rows in each of four materialization tables, with zero value,
status, null-policy, series-definition, or metadata findings. Metadata validation
version 2 checks all 19,592 session records against independent counts and the
frozen publication digests.

The complete physical table populations are:

| Table | Rows |
| --- | ---: |
| `stockguidance_source` | 1 |
| `transaction_dates` | 5,130 |
| `ticker_symbol` | 40,066 |
| `raw_stock_data` | 44,248,740 |
| `daily_delta_stock_data` | 44,188,967 |
| `stockguidance_imports` | 4,898 |
| `stockguidance_zscore_series` | 4 |
| `stockguidance_zscore_sessions` | 19,592 |
| `stockguidance_data_verifications` | 0 |
| `stockguidance_data_verification_samples` | 0 |
| `stockguidance_migration_receipts` | 4,898 |
| `stockguidance_migration_completion` | 9,791 |
| `zscores_price_20` | 196,243,268 |
| `zscores_price_60` | 196,243,268 |
| `zscores_price_delta_20` | 196,243,268 |
| `zscores_price_delta_60` | 196,243,268 |

The physical raw/derived totals exceed the independently adopted/derived cohort;
legacy rows associated with excluded or unsupported retained data are preserved.
Do not interpret these physical counts as a claim that every legacy row has
valid archive evidence. All 5,130 mapped dates belong to the retained file scope.
Ticker/date orphan counts are zero for both source tables and all four z-score
tables, and no derived key lacks a raw key. There are 59,773 raw keys without a
derived key. The report preserves that global key gap alongside the 232 excluded
archive files (including their failure reasons) and five unavailable targets:
238 unresolved records in total. `production_ready=false` and
`fully_verified_continuous_coverage=false` remain intentional.

The final revision before/after is unchanged:
`d559c9cc39e79812c4a4110cc6b3db75cbef18895c807c2d4db03bcc6d985951`.
The final gate reuses four completed SQL integrity checks under unchanged revision
and exact table counts, performs the additional ticker checks, and proves the
four compact tables' complete key scope from independent row validation plus
exact counts. The report exposes these methods in `scope_proofs`.

Final report SHA-256:
`f0e65d72396d9a36a17fb0fe994f12a72405757050f7586251249ffe4c49e8a5`.
Benchmark report SHA-256:
`2debf74978e79915de9ca0ef430997a8eac989dcc4fb9e762c133ecc0136b012`.
Thirty focused pipeline, CLI, recovery-integration, and documentation tests pass;
focused type checking passes for all three Stage 15 modules. No commit was made.
