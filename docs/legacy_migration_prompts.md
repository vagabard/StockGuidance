# Massive legacy archive migration prompts

Use one numbered stage per conversation. Start with:

```text
Read docs/legacy_migration_prompts.md and execute Stage N only. Check its
prerequisites, implement and test its deliverables, run its authorized bounded
operations, then update its handoff. Preserve unrelated edits. Do not commit.
```

Select Luna medium for stages 4, 7, 8 and 10; high for stages 1, 2, 3, 5, 6
and 9. A stage is complete only when its acceptance criteria and handoff exist.
Do not interpret completed scaffolding as a completed production migration.

## Shared instructions

- Work in `C:\Projects\StockGuidance`. Read applicable AGENTS.md instructions,
  `docs/operations_guide.md`, `docs/database_validation.md`, this file and the
  previous stage's handoff. Inspect git status; preserve unrelated files.
- Consult python-tools and rediscover PyCharm's interpreter before every Python
  invocation. Set PYTHONPATH to the checkout's `src`. No package installation
  or interpreter changes without checking the existing environment.
- Run only one language benchmark at a time. Use one or three Java application
  threads, or one or three Python worker processes. No nested pools; cap native
  libraries at one thread. Count runtime/monitoring overhead separately. Use at
  most three workers in migration pipelines and one production database writer.
- Credentials stay in existing configuration files. Never print configurations,
  import the legacy fallback-key module for inspection, include secrets in
  subprocess arguments, or put them in reports. Read-only study commands need
  only the path to `dbinfo.id`.
- The user attests these files are direct Massive grouped daily US-stock
  responses, split-adjusted and excluding OTC. Record that as operator
  provenance, not independent provider authentication. Nine passing random
  files gate source acceptance; validate every subsequent file before adoption.
- The user authorizes repair of database differences from validated archive
  files, preserving existing ticker/date IDs and unrelated data. Review backup
  restoration and pilot results before full writes. Do not drop production
  tables or delete original downloads. Do not change source bindings silently.
- Retained archive coverage is the operation scope. Database-only observations,
  missing sessions and unassigned orphans remain explicit. No automatic network
  requests, invented bars, empty-file no-trading claims or provider entitlement
  claims. Do not infer original receipt times from filesystem timestamps.
- Strict historical training rules remain unchanged. Adoption proves a local
  snapshot exists at verification time, not that it was known on the bar date.
- Write stage artifacts below `output/legacy-migration/`. Keep a small tracked
  human-readable handoff here; do not track backups or full data reports.
- Stop on failed source acceptance, conflicting identities, invalid mappings,
  changed input hashes, unavailable backups or failed recovery tests. Finish
  independent implementation/tests and record exactly which production work is
  blocked. Never mark unresolved sessions complete.

## Current starting state and discovered precision issue

The earlier initialization created the eight updater/support tables, bound the
standard adjusted Polygon/Massive source, and converted `transaction_dates` to
InnoDB. Raw/delta rows were retained; import/verification tables were empty.
Reinspect this state before relying on it. The existing generated date IDs must
remain intact; initialization alone does not migrate column definitions.

Two backups exist under `output/database-backups/`; the full SQL dump is about
10.6 GB. Its completion trailer was checked, but restoration has not been
tested. It was taken while `transaction_dates` was MyISAM, so do not assume a
consistent backup if writers were active. Validate or replace it before repair.

Archive inventory currently spans 2005-12-16 through 2026-05-07. Full-history
Python validation previously buffered tens of millions of rows; even a narrow
request triggered a global scan through its orphan-query branch. Never repeat
that query shape inside every chunk.

**Precision prerequisite discovered during implementation:** sampled archive
files contain fractional adjusted volumes, for example 14982.4, 61812.9999 and
148789.380777. These are valid provider representations; the current DailyBar
contract requires integral volume and production `raw_stock_data.volume` is
BIGINT UNSIGNED. Rounding them would make archive adoption lossy. Massive
documents this behavior at:
https://massive.com/knowledge-base/article/why-does-volume-return-as-a-decimal-value-from-the-aggregates-endpoint

The 2017-03-13 file also contains RNVA open 1702500000000000.0, above the
current DECIMAL(19,4) price limit. Stage 5 must inventory price ranges as well
as fractional volumes and check whether legacy values were clipped/rounded.
The benchmark has a separate decimal-preserving ArchiveBar; it does not change
production DailyBar or claim that production can already import these records.
Stage 5 must resolve both issues before Stage 7 writes.

**Ticker identity prerequisite:** live inspection confirmed `ticker_symbol.label`
uses `utf8mb4_0900_ai_ci` with a unique index. Source keys such as `AAP` and
`AAp` are distinct; that index cannot represent both. The database stores `AAP`
and `ABKPZ`, not their exact-case source counterparts `AAp` and `ABKpZ` in the
inspected examples. Nine-session auditing found 3,597 missing exact-case keys,
3,490 extra keys and 6,682 differing shared rows. Do not uppercase/case-fold
source keys to make comparison pass. Investigate conflated rows before repair.

## Stage 1 — Bounded reader and parity contract (high)

Inspect `performance/archive_read.py`, `cli/archive_study.py`, and Java
`validation/ArchiveStudyMain.java`. Support explicit file lists and modes
`files`, `database`, `reconcile`. Each result identifies session, full original
file SHA-256, normalized record digest/count, and database digest/count where
applicable. The benchmark digest uses price strings at four decimal places and
volume as a canonical decimal string without trailing zeros. It is versioned
study evidence, not a production import receipt.

Read a file at a time. Reject duplicate JSON keys/tickers, malformed records,
empty files, inconsistent OHLC, invalid/null required numbers, wrong dates and
unsupported ranges. Accept exact fractional nonnegative volume; keep transaction
counts and milliseconds integral. Use Decimal/BigDecimal, never binary floating
point, for parsed quantities and prices. Do not strip or rewrite source files.

Python must use an unbuffered mysql.connector cursor or PyMySQL SSCursor.
Java must use cursor-fetch-enabled Connector/J, forward-only/read-only result
sets and a positive fetch size. Query date mappings first, then raw rows by
explicit date ID using the existing date_id index. Close results/connections
and roll back all transactions, including failures. No full raw-table scan.

Acceptance: focused reader tests, Java tests/package, identical cross-language
digests on valid real files, and rejection fixtures for invalid data. Add mock
database tests covering query bounds, nulls, duplicate mappings and rollback.

## Stage 2 — Complete measured comparison (high)

For bounded conversations, execute Stage 2a (raw/file study and EXPLAIN), then
Stage 2b (derived computation parity/study), then Stage 2c (disposable write
candidate study, only if proposing a Java writer). Do not bundle the Java
writer port with reader measurements. Stage 2a's `complete` report flag means
its selected read workloads finished, not that Stages 2b/2c are complete.

Prerequisite: Stage 1 correctness. The `archive_study` CLI provides the raw/file
portion. Run from the checkout with the configured interpreter:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
# Set $pythonExe using get_python_environment, not a guessed executable.
& $pythonExe -m stockguidance.cli.archive_study --output output/legacy-migration/benchmark
```

Use seed 20261019 and 24 sessions (eight per chronological third), plus the
largest file if absent. Preserve exact selected paths in requests. Complete the
remaining study work before calling this stage complete:

1. Confirm query plans use bounded date-ID access; save sanitized EXPLAIN plans.
2. Ensure an idle database or a shared consistent snapshot. Separate workers'
   repeatable-read transactions alone are not a shared snapshot. Report drift.
3. Add derived-computation measurements using neighboring exchange sessions,
   Python `legacy_rows` and the supported Java formulas. Compare every column
   with existing persistence tolerances; do not benchmark incompatible targets.
4. Measure first invocation plus three warm repeats, alternating language order
   across paired trials. Keep executor lifetime and setup rules symmetric.
5. Report process startup/total wall time, task wall time, summed process CPU,
   aggregate process-tree sampled RSS, row throughput and actual query counts.
   Label unavailable metrics and first-run cache state honestly. Include Python
   spawn/serialization and JVM startup. Set Java heap to 512 MiB and native
   library threads to one; disclose these settings.
6. Save partial reports and bounded timeouts. Match all canonical results before
   ranking. Do not time production writes. Write comparisons need disposable
   storage and the same pilot input and commit boundaries.

Acceptance: single/three-worker parity for both languages, bounded RSS and
reproducible machine-readable results plus a concise comparison table. Separate
raw/file completion from derived/write coverage in the handoff.

Current handoff: Stage 2a raw/file and EXPLAIN work is complete, and Stage 2b
derived-computation parity/study is complete in
`output/legacy-migration/benchmark/derived/study.json`. Stage 2c remains skipped
because no Java writer is proposed; do not perform production writes as part of
this handoff.

## Stage 3 — Select implementations and freeze the rollout (high)

Prerequisite: Stage 2 reports. Choose the lowest warm median end-to-end time
among correct implementations; a difference below 10% favors existing Python.
Show single-worker and three-worker throughput and speedup separately. Estimate
archive runtime/disk needs from measured file sizes and row counts; label the
estimate rather than promising a duration.

Include dense z-score capacity explicitly: the earlier inventory had 36,866
ticker IDs and 5,130 sessions. Four dense materializations would contain up to
756,490,320 rows before metadata, indexes and rebuild overhead. Recheck counts
and available disk; do not confuse raw-row counts with the existing updater's
all-ticker/status-row expansion. Preserve required NULL/status semantics and
record the actual selected storage/population behavior before rollout.

The user permits a Java write implementation if justified. Implement it only
after a representative disposable write benchmark demonstrates at least a 10%
end-to-end advantage over Python and identical transaction/recovery semantics.
Without that evidence, retain Python transactions and use the faster proven
reader/processor. Do not infer a write speedup from a read-only result.

Write `output/legacy-migration/decision.json`: input report hashes, selected
reader/processor/writer, batch limits, workers, memory bounds and reasons.
Update stages 5–10 below with concrete selected entry points. Default to one
session per write transaction and a chronological 20-session processing chunk;
use measured smaller limits if needed to stay under 512 MiB per worker. Enforce
bounded queues (at most three prepared sessions) and no competing writer.

Acceptance: parity-backed selection, explicit unmeasured areas, and updated
later-stage commands/configuration. No production mutations in this stage.

## Stage 4 — Random archive source acceptance (medium)

Prerequisite: Stage 2a read benchmark report and its provisional reader decision
exist. Final derived/write selection is not needed for this read-only check.
Select nine distinct files with seed 20261019, three per chronological third,
from the sorted complete inventory. Do not substitute files after failures.
Fully validate all records in each selected file using both readers when their
parity is established. Check exchange calendar membership as well as each row's
timestamp in America/New_York. Filename dates alone are insufficient.

Persist the user's source attestation, explicit source identity, seed, complete
inventory identity, paths, original hashes, validated record counts/digests,
verification time and typed findings. Preserve exact fractional volumes.
Passing samples establish accepted provenance under the user's policy; they
do not establish market-wide completeness or validate unexamined files.

Acceptance: `source-acceptance.json` reports nine passing files or a failed gate
with actionable findings. No database writes, network calls or fake HTTP data.

The implemented Python command is:

```powershell
& $pythonExe -m stockguidance.cli.verify_archive_sample --attestation 'Operator confirms direct Massive split-adjusted US-stock grouped downloads excluding OTC'
& $pythonExe -m stockguidance.cli.compare_archive_sample
```

The first command checks all records in nine selected files and records original
hashes and schema-range diagnostics. The second rechecks those hashes and compares
the nine files against raw stock tables in one read-only transaction, saving
per-field difference counts and bounded examples. It returns 2 for differences;
that is a completed audit, not a setup failure. It creates no import receipts.
Complete the cross-language sample comparison and retain the benchmark
prerequisite before marking the stage complete.

## Stage 5 — Precision, archive evidence and complete audit (high)

Prerequisite: source gate passed. Split this stage into separate prompts 5a0–5d
if needed; each is independently reviewed and tested before the next.

**5a0 — Case-sensitive ticker identity.** Inventory exact archive keys across
all retained dates and compare them with existing labels/IDs. Produce a
collision/alias map before writes. Preserve IDs for exact canonical matches.
Rename an uppercase legacy alias while preserving its ID only when it has no
exact archive-key match and maps to exactly one canonical archive key globally.
For ambiguous/colliding keys, keep existing exact-key IDs and create distinct
canonical IDs after the collation migration; do not merge securities or silently
reuse the common-share ID for a preferred-share key. Record unresolved aliases.
Generate tested DDL using case-sensitive `utf8mb4_bin` collation for ticker labels
and retain the unique constraint. Update new-schema DDL and all lookup/upsert
paths to preserve exact case. Test AAP/AAp and preferred-series examples through
Python/Java, preserving IDs and rebuilding damaged per-session raw/derived data
from validated files. Apply production DDL only in Stage 7 after restore checks.

**5a — Decimal quantity contract.** After 5a0's read-only identity inventory,
inventory every valid archive quantity's
integer/fractional digits and source precision without changing data. Adopt
exact Decimal/BigDecimal volume end to end and support observed adjusted-price
ranges. Select DECIMAL precision/scale for OHLC, VWAP and volume that
exactly fits the inventory with explicit headroom within MySQL limits; record
the measured maximum and selected DDL in the handoff. Reject overflow rather
than rounding. Keep transactions integral. Update Python/Java decoding,
serialization, hashing, validation, derived volume ratios, schema preview and
parity fixtures. Preserve old integral digest/manifest readability through an
explicit versioned compatibility rule; do not silently invalidate old receipts.
Generate migration DDL but execute it only at Stage 7 after backup checks.

**5b — Retained archive evidence.** Add a versioned typed evidence contract and
store containing source identity, attestation ID, original path/hash, canonical
records ID/count, validation time and unknown original network receipt time.
No fabricated HTTP 200, status OK, resultsCount or pagination evidence. Adapt
shared consumers to distinguish adopted archive snapshots from captured network
responses. They must reject source conflicts, changed files and corrupt metadata.

**5c — Streaming complete audit.** Implement an offline migration frontend with
audit as default, explicit apply mode, bounded dates, decision/evidence paths
and checkpoints. Audit all files and database rows, compare exact normalized
values/keys, and report missing/extra/different fields per session. Enumerate
database-only dates independently. Scan global orphan integrity once, not in
each session query. Invalid files stay quarantined without modifying originals.
Save resumable per-session reports bound to file hashes, schema/source identity
and database revisions; recheck inputs before reuse.

**5d — Local snapshot availability.** Add an explicit offline snapshot path for
population/z-scores that requires verified archive observations at a fixed aware
cutoff. Unknown live publication/correction policy must not block historical
local snapshot operations or be replaced with invented values. Preserve existing
live-provider and strict historical training behavior and provenance distinctions.

Acceptance: decimal-volume regression fixtures; old-evidence compatibility;
no source adoption through sampling alone; no credentials in reports; complete
bounded audit with explicit gaps, orphans and unadoptable files. No production
schema/data writes yet.

## Stage 6 — Population and recovery implementation (high)

Prerequisite: Stage 5 contracts/audit and Stage 3 implementation decision. Reuse
existing raw/derived formulas and transactions. Do not reimport matching raw
rows just to establish evidence: atomically verify their current content and
insert the appropriate adoption receipt. For differences, replace only the
authorized validated session's raw set. Preserve mapped ticker/date IDs.

Commit session data and receipts together. Retain provenance kind and adoption
time; original receipt remains unknown. Set completion metadata only after
commit. Include file/source/record identities in restart checks. Recheck raw
dependencies in the write transaction; invalidate affected prior/current/next
derived results and every dependent z-score window. Rebuild chronologically
with required neighboring sessions. Database-only history remains untouched.

Implement a production writer only after disposable tests prove rollback,
before/after-commit interruption recovery, stale checkpoints, changed inputs,
derived dependency races and idempotent reruns. A Java candidate must additionally
match Python decimals, numerical tolerances, receipts and commit boundaries and
meet Stage 3's measured write threshold.

Acceptance: disposable migration/recovery tests and concrete audit/apply/pilot
commands recorded here. No production mutations in acceptance tests.

## Stage 7 — Backup, precision migration and pilot (medium)

Prerequisite: all preceding acceptance criteria. Inspect current source binding,
engines, column definitions, date/ticker IDs, disk space and competing writers.
Prove backup usability by restoring to an isolated disposable database and
checking schema and representative row counts/hashes. Refresh the backup if
needed for a consistent current restore point. Never restore over production.

Preview the selected registry/DDL. Apply the tested ticker-collation and numeric-column migrations,
preserving IDs and indexes; account for table-copy disk and duration on the
44-million-row table. Initialize only missing compatible support tables.
Choose the latest complete calendar month represented in the archive, requiring
every expected exchange session, plus neighboring dependencies. If no complete
month exists, use the latest 20 consecutive represented exchange sessions and
record the fallback. Run the explicit offline pilot, then independent audit.

Acceptance: restore report, before/after schema and pilot reports, unchanged
second-run receipts/rows and no unexplained raw or derived differences. Stop
production rollout on pilot failure; preserve evidence for diagnosis.

## Stage 8 — Populate remaining retained history (medium)

Prerequisite: pilot passed. Run chronological bounded chunks using the frozen
decision and the same source/evidence/cutoff. Write durable completion markers
after validated commits. Recheck each file hash before consumption and database
revision before applying cached repairs. Finish all valid archive sessions;
record invalid files and missing dependencies without synthetic replacements.

Validate each chunk and its boundary dependencies. Recover interrupted chunks
using the implemented restart protocol; do not reset progress or blindly trust
the newest date. Save coverage, original/current row counts, receipt counts,
repaired/matching sessions and unresolved issues to `population.json`.

Acceptance: every adopted session reconciles exactly, raw/derived receipts are
valid, original ticker/date mappings are retained, and rerun is idempotent.

## Stage 9 — Populate z-scores (high)

Prerequisite: verified raw/derived archive inputs and snapshot availability path.
Create missing z-score metadata and both families' 20/60 materializations using
the authoritative registry. Reuse calculation/status/float32 contracts.
Process bounded chunks with at least 60 preceding exchange sessions as needed
and only publish requested sessions. Price-delta inputs remain the stored
close_ratio_to_prior minus one, with dependency validation.

Use all retained ticker IDs. Preserve insufficient history, missing/unverified/
invalid input and zero variance as explicit NULL/status outcomes, never zeros.
Keep publication provenance local-snapshot-specific; do not claim original
historical availability. Save per-series/session counts, hashes and statuses.

Acceptance: independent recomputation on chunk boundaries and random sessions,
float32-aware comparisons, stale-input rejection, repeatable reruns and explicit
coverage of four materialization tables plus two metadata tables.

## Stage 10 — Independent final audit and operating handoff (medium)

Prerequisite: population and z-score reports. Run bounded independent archive/raw
comparison over the complete retained scope and recompute derived/z-score values
using the semantic reference. Recheck global mapping/orphan integrity once.
Aggregate reports without hiding failed or omitted findings. A gap can remain
unresolved, but must prevent a claim of fully verified continuous coverage.

Update operations/database-validation/performance documentation with actual
commands, benchmark findings, selected backend, decimal migration, evidence
semantics, table populations, known limitations and resume instructions. Correct
the old six-table base-schema wording to eight. Keep live refresh policy and
training/simulation setup separate from this offline archive migration.

Acceptance: final audit reports exact adopted raw agreement, valid derived and
z-score dependencies, counts for every support/materialization table, tested
recovery and a complete list of unresolved gaps and excluded data.

## Handoff log

### Initial implementation — 2026-10-19

- Added bounded decimal-preserving Python and Java study readers and one/three
  worker runner. Existing production paths remain unchanged.
- Added focused Python/Java tests. The initial Java test/package run passed;
  the initial 15 Python tests passed before decimal-volume support was added.
- Initial real benchmark exposed fractional source volume, requiring the Stage 5
  precision prerequisite above. This is a contract/schema issue, not evidence
  that these files are counterfeit or malformed.
- Stage completion/results are recorded below after validation. No production
  schema, data, source receipts or manifests were changed by this implementation.

### Initial read study and sample audit completed — 2026-10-19

- Stage 1: implemented and exercised. Stage 2a: all twelve file/SQL/reconcile
  configurations completed with matching canonical results, including a final
  cross-workload identity check. Stages 2b/2c remain pending as specified above.
- Stage 3: provisional Java three-worker reader selection recorded in
  `output/legacy-migration/decision.json`; processor/writer selection and
  production readiness remain unset. Do not interpret this as a Java write port.
- Stage 4: nine files/77,783 records passed in Python; Java independently matched
  all original hashes, record digests and counts. Operator-attested source gate
  passed. Original network receipt times remain unknown.
- Nine-session stock-table comparison completed with differences: 3,597 missing
  keys, 3,490 extra keys, 6,682 differing shared rows and 24 open-price differences.
  This audit did not create import receipts or mark database rows verified.
- Added Stage 5a0 after live inspection proved the ticker unique index is
  case-insensitive. Stage 5a also needs decimal volume and wider adjusted-price
  support. Stages 5–10 are not implemented or executed by this initial study.
- Validation: 25 focused Python tests passed; all 31 Java tests and package
  passed. JDBC UTF-8 correction was exercised by the live read study.
- See [initial results](legacy_migration_results.md) for measured times, memory,
  commands, evidence paths and limitations. Next prompt: execute Stage 2b only.

### Stage 2b derived-computation parity study completed — 2026-10-19

- Stage 2a was treated as complete and its exact 25 selected paths were reused;
  immediate retained neighbors were included without resampling or rerunning
  Stage 2a. Stage 2c was not executed.
- Python and Java both ran the 16-column legacy derived formulas against the
  existing persisted rows, using four repeats and the required order: Java then
  Python with one worker, Python then Java with three workers. Each worker used
  a separate read-only repeatable-read transaction, bounded one-target scopes,
  Java `-Xmx512m` serial GC and one native-library thread. No production writes
  or writer timing occurred.
- All 25 central sessions had context. Cross-language, cross-worker and
  cross-repeat canonical parity passed (`parity: true`); this does not mean the
  existing derived values passed formula validation.
- Existing persistence validation found 217,055 stored versus 217,467 expected
  rows, 412 missing keys, no extra keys and zero of 25 sessions matching all
  columns under the existing tolerance. Rounding/storage differences are
  retained as findings for the numeric precision work; they were not repaired.
- Results: `output/legacy-migration/benchmark/derived/study.json` and its
  per-run JSON files; report SHA-256:
  `adb098d4afebc186484553500d4b241301296e8be821293207b9d166fc98bf7e`.
- The provisional Stage 3 Java three-reader choice remains a reader-only choice.
  No Java writer is proposed, so Stage 2c remains skipped; continue with the
  separately gated Stage 5a0/5a precision and identity work.

### Stage 2c prerequisite review completed — 2026-10-19

- Stage 1 correctness, Stage 2a read/EXPLAIN completion and Stage 2b derived
  parity were rechecked from their reports. `output/legacy-migration/decision.json`
  still records `processor: null`, `writer: null` and `production_ready: false`;
  the Java selection is reader-only.
- The current Java source tree contains no writer or import candidate. Because
  Stage 2c is authorized only when a Java writer is proposed, its disposable
  write-study deliverable is not applicable and no candidate study was run.
- Focused validation passed: `pytest` archive/read/sample/artifact tests, 58
  passed; offline `mvn -o test`, 32 passed. No disposable or production
  database writes, schema changes, receipts or source changes occurred.
- Stage 2c is intentionally recorded as skipped pending a future Java writer
  proposal; do not treat the reader selection or these tests as write evidence.
