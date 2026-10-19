# Massive legacy archive migration prompts

Use one numbered stage or lettered substage per conversation. Start with:

```text
Read docs/legacy_migration_prompts.md and execute Stage N only. Check its
prerequisites, implement and test its deliverables, run its authorized bounded
operations, then update its handoff. Preserve unrelated edits. Do not commit.
```

Select Luna medium for stages 3, 4, 9a–9c, 12, 13 and 15; high for stages 1,
2, 5a0–5d, 6, 7a–7b, 8a–8c, 10, 11a–11b and 14. Execute each named
substage separately, not its entire parent stage in one conversation. A stage
is complete only when its acceptance criteria and handoff exist. Do not interpret
completed scaffolding as a completed production migration.

**Resume here: Stage 3, Luna medium.** Stages 1–2 and their historical handoffs
are preserved below. Their passing reader/derived measurements remain evidence
for those workloads, but the expanded comparison is incomplete. Stage 2c reviewed
prerequisites and skipped timing; it did not evaluate a Java writer. SQL has not
been evaluated. The revised Stages 3–10 require both candidates to be implemented,
tested and measured before selection. Their requirements supersede the historical
conditional Java-writer skip and provisional implementation-selection instructions.

| Stage | Work | Luna |
|---|---|---|
| 3 | Carry forward evidence and define required comparisons | Medium |
| 4 | Recheck existing source acceptance without resampling | Medium |
| 5a0, 5a, 5b, 5c, 5d | Identity, precision, evidence, audit, snapshot contracts | High |
| 6 | Disposable harness and Python baselines | High |
| 7a, 7b | Java writer and z-score candidates | High |
| 8a, 8b, 8c | SQL raw, derived and z-score candidates | High |
| 9a, 9b, 9c | Measure raw, derived and z-score comparisons | Medium |
| 10 | Select backends and freeze rollout | High |
| 11a, 11b | Integrate selected backends and prove recovery | High |
| 12 | Restore check, schema migration and pilot | Medium |
| 13 | Populate remaining raw/derived history | Medium |
| 14 | Populate z-scores | High |
| 15 | Independent final audit and handoff | Medium |

Historical numbering map: old Stage 3 selection moves to Stage 10; Stage 4 and
Stage 5 keep their numbers; old Stage 6 becomes 11a–11b, old Stage 7 becomes 12,
old Stage 8 becomes 13, old Stage 9 becomes 14 and old Stage 10 becomes 15.
Historical handoffs and `docs/legacy_migration_results.md` describe the original
numbering and measured scope; use this file's revised sequence for new work.

## Shared instructions

- Work in `C:\Projects\StockGuidance`. Read applicable AGENTS.md instructions,
  `docs/operations_guide.md`, `docs/database_validation.md`, this file and the
  previous stage's handoff. Inspect git status; preserve unrelated files.
- Consult python-tools and rediscover PyCharm's interpreter before every Python
  invocation. Set PYTHONPATH to the checkout's `src`. No package installation
  or interpreter changes without checking the existing environment.
- Run only one candidate benchmark at a time. Use one or three Java application
  preparation threads, or one or three Python preparation processes. No nested
  pools; cap native libraries at one thread. Count runtime/monitoring overhead
  separately. Use at most three preparers, at most three queued sessions and one
  database writer, including disposable trials. SQL uses one writer connection;
  report engine background work separately rather than calling it client workers.
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
- Stages 3–11 may write artifacts and explicitly isolated disposable databases,
  never production schema, data, routines, receipts or source bindings. Stage 3
  and Stage 4 are database-read-only. Require a distinct disposable schema and
  explicit target validation before candidate DDL/DML; never reset a schema named
  by production configuration. Preview routine names and DDL through the selected
  table registry, preserve caller-controlled transaction boundaries, and do not
  introduce implicit commits inside data-publication transactions.
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
Stage 5 must resolve both issues before candidate writes in Stages 6–9 and
production writes in Stage 12.

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

## Stage 3 — Evidence carry-forward and mandatory comparison plan (medium)

Prerequisite: committed Stage 2c review and existing Stage 2a/2b reports. Read
their requests, results and historical handoffs. Preserve existing files and
report `complete` flags as historical workload-specific evidence; do not relabel
them as completed write or SQL comparisons. Verify report hashes and record
missing/drifted inputs without silently rerunning or substituting new samples.

Carry forward these measured facts in `evaluation-plan.json` and a short handoff:

- Stage 2a: 25 files, 217,465 records, cross-language/worker/repeat parity;
  Java three-reader warm reconcile task median 13.7581895 seconds versus Python
  three-reader about 20.03 seconds. This remains a provisional reader choice,
  not a startup-inclusive writer result. Report SHA-256:
  `517bf3d872e7149254493ae3177467653e4f7accf4c3daf798939119f035e0bd`.
- Stage 2b: all 16 derived columns matched across languages/workers/repeats;
  warm task medians were Java 99.784/78.569 seconds and Python 173.765/108.100
  seconds at one/three workers. Existing persistence had 412 missing rows and
  zero of 25 sessions matching every column. Keep those findings visible.
  Report SHA-256:
  `adb098d4afebc186484553500d4b241301296e8be821293207b9d166fc98bf7e`.
- Stage 2c: prerequisite review only; no Java writer, disposable write trial or
  production write occurred. No SQL candidate or z-score benchmark exists.
- Source acceptance: nine files/77,783 records passed in both readers. Retain
  exact-case key differences, fractional volumes and oversized prices as repair
  prerequisites, not language-parity failures or reasons to discard source data.

Freeze the required matrix: Python, Java and SQL for each of raw adoption/repair,
derived computation plus persistence, and both z-score families/windows plus
persistence. Java and SQL candidate implementation is explicitly authorized and
required in disposable storage. Lack of an existing implementation cannot waive
evaluation; the 10% rule controls later selection, not whether to build a trial.

Record artifact paths/hashes, exact existing 25 target paths, dependency rules,
stage prerequisites, pending matrix cells and the protocol specified in Stage 6.
Keep `decision.json` provisional; do not select a production processor/writer.
Record that the expanded Stage 2 comparison objective remains incomplete until
Stages 9a–9c supply the missing measurements, although old report flags stay intact.

Acceptance: `evaluation-plan.json` distinguishes measured, pending and blocked
work; all nine workload/backend cells are mandatory. Historical evidence remains
unchanged, no database mutations occur, and the next prompt is Stage 4 only.

## Stage 4 — Recheck existing random archive source acceptance (medium)

Prerequisite: Stage 3 handoff and existing Stage 2a/source-acceptance evidence.
The nine-file acceptance already passed in Python and Java. Recheck report and
original-file hashes, inventory identity, source identity and cross-language
evidence. Reuse the recorded sample and seed 20261019, three files per
chronological third; do not resample or rerun the whole audit when evidence is
unchanged. Missing checks must use those same files, validating every record,
exchange calendar membership and timestamp in America/New_York. Do not substitute
files after failures; filename dates alone are insufficient.

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

These are existing commands for missing or explicitly required revalidation;
inspect their selection behavior before running and retain the recorded sample.
The first command checks all records in nine selected files and records original
hashes and schema-range diagnostics. The second rechecks those hashes and compares
the nine files against raw stock tables in one read-only transaction, saving
per-field difference counts and bounded examples. It returns 2 for differences;
that is a completed audit, not a setup failure. It creates no import receipts.
Complete the cross-language sample comparison and retain the benchmark
prerequisite before marking the stage complete.

## Stage 5 — Precision, archive evidence and complete audit (high)

Prerequisite: Stage 4 source gate rechecked. Execute separate prompts in this
order: 5a0, 5a, 5b, 5c, 5d. Each needs its own tests, artifacts and handoff
before the next. Final backend selection is not a prerequisite for this stage.

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
Python/Java, preserving IDs and testing reconstruction of damaged per-session
raw/derived data from validated files in disposable fixtures. Apply production
DDL and repairs only in Stage 12 after restore checks. SQL candidates must later
pass the same exact-case identity fixtures.

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
Generate migration DDL but apply it to production only at Stage 12 after backup
checks. Recheck bounded Python/Java reader parity under the updated contract;
version new digests explicitly and preserve the original Stage 2 measurements.
Record derived column precision/storage rules as well as raw-column rules so all
three candidates target compatible schemas and persistence tolerances.

**5b — Retained archive evidence.** Add a versioned typed evidence contract and
store containing source identity, attestation ID, original path/hash, canonical
records ID/count, validation time and unknown original network receipt time.
No fabricated HTTP 200, status OK, resultsCount or pagination evidence. Adapt
shared consumers to distinguish adopted archive snapshots from captured network
responses. They must reject source conflicts, changed files and corrupt metadata.

**5c — Streaming complete audit.** Implement an offline migration frontend with
audit as default, explicit apply mode, bounded dates, decision/evidence paths
and checkpoints. Use proven readers/the semantic reference without requiring a
final backend decision. Accept a provisional decision for audit only; reject
production apply until Stage 11 integration and Stage 12 gates exist. Audit all
files and database rows, compare exact normalized
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

## Stage 6 — Disposable harness and Python baselines (high)

Prerequisite: Stage 3 plan and all Stage 5 handoffs. Implement a backend-neutral
trial interface for `raw`, `derived` and `zscores`, an explicit disposable schema,
workload manifest, repetitions, preparer count, timeout and output path. Reuse
compatible study infrastructure. Record actual CLI/API commands in the handoff.

Create disposable schemas from corrected Stage 5 DDL and the authoritative
registry, including equivalent indexes, constraints and applicable production
triggers/routines. Inventory their overhead rather than silently omitting it.
Keep source data immutable; reset targets to identical starting states outside
timing and report setup/reset time and disk separately. Refuse production DDL/DML
targets. Production access is limited to explicit bounded read-only extraction.

Implement Python reference baselines for all workloads using Stage 5 contracts,
existing formulas, evidence and z-score semantics. Raw scenarios must include
matching/adoption-only, missing/different-row repair and unchanged rerun. Use the
same transaction units for every candidate: one session per raw/derived transaction
and one series/session per z-score transaction. Data, receipts and database
completion metadata commit atomically; external checkpoints follow commit.

Freeze this protocol in `benchmark/candidates/manifest.json`:

1. Reuse the exact 25 Stage 2 paths. Derived targets need preceding/following
   exchange sessions; z-score targets need 60 preceding exchange sessions,
   including prior-close dependencies. Preserve missing dependencies as explicit
   outcomes. Add one chronological 20-session target block ending on the largest
   selected file's session, with required dependency context. Record every path,
   original hash, mapping, source/snapshot identity and corrected schema hash.
2. Use all retained ticker IDs and identical dense NULL/status policy for every
   z-score candidate. Stream staged inputs; never buffer all target sessions'
   dependency history in each worker's memory.
3. Measure first invocation plus three warm repeats per backend/workload/preparer
   configuration. Rotate candidate order across rounds: Python/Java/SQL,
   Java/SQL/Python, SQL/Python/Java, Python/SQL/Java. Run sequentially. Report
   first-run cache state as unknown unless actually controlled.
4. Use one and three preparers where preparation can overlap, but always one
   writer and at most three queued sessions. Show SQL's one-connection
   database-resident path separately; mark inapplicable preparer configurations
   explicitly instead of adding SQL writers. Cap native threads at one, set Java
   `-Xmx512m`, and keep each preparer's application data within 512 MiB. Report
   total process-tree RSS/runtime overhead separately: heap size does not cap
   total JVM RSS or database-server memory.
5. Report per-trial startup-inclusive end-to-end wall time through durable commit
   and required publication/checkpoint work, task/phase times, throughput, actual
   query counts, summed client process CPU and sampled process-tree RSS. Include
   spawn, serialization, JVM startup, language handoffs, hashing, evidence and
   metadata. Run independent correctness audits outside timing equally for all
   candidates; passing them remains a ranking prerequisite.
6. For SQL raw trials include file decoding/validation, staging loads and final
   writes in end-to-end time. Also report database-resident derived/z-score time
   separately from common source staging. Disclose staging/orchestration code;
   calling SQL from Python/Java does not make its computation that language's
   candidate. Measure each alternative's complete pipeline.
7. Capture available server CPU, temporary-table spills/disk, I/O and statement
   metrics plus sanitized EXPLAIN plans. Label unavailable permissions/metrics;
   client RSS alone cannot establish SQL resource efficiency. Record engine
   settings, disk capacity and absence of competing trials/writers.
8. Save partial reports after each trial, use explicit bounded timeouts, and
   classify failures. Validate exact raw decimals/keys, existing derived
   tolerances, z-score float32 tolerances and exact statuses/metadata semantics
   before ranking. Do not round inputs or loosen tolerances to pass a candidate.

Persist backend/workload/configuration, input/schema/code identities, phase
metrics, correctness/recovery outcomes and completion status in versioned JSON
with a concise table. Do not rank incompatible timing boundaries; old Stage 2
task medians remain context only.

Acceptance: working bounded harness, explicit manifest, disposable target guard,
all three Python baselines and meaningful rollback/restart/parity fixtures.
Record reproduction commands; leave Java/SQL timings pending. If needed continue
Stage 6 in another conversation; do not bundle Stage 7 or claim partial completion.

## Stage 7 — Required Java candidates (high; execute 7a then 7b)

**7a — Java writer.** Prerequisite: Stage 6. Implement a bounded JDBC candidate
for raw adoption/repair and derived persistence in disposable storage. Use
BigDecimal, exact-case mappings, batch statements, and Python's input contract,
session transactions, evidence and metadata. Reuse the existing Java derived
formulas after Stage 5 parity updates. Include matching raw adoption without
rewriting bars and file-hash/source/dependency rechecks. Prove rollback,
before/after-commit interruption, changed input, stale checkpoint and idempotent
rerun behavior. Record the harness entry point and runnable command.

**7b — Java z-scores.** Prerequisite: 7a. Implement both price and price-delta
20/60 candidates with the Python semantic reference as oracle. Use consecutive
exchange-session windows inclusive of the target, population variance, stable
float64 computation and float32 persistence. Read stored `close_ratio_to_prior - 1`
inputs and validate their closes/dependencies. Match full retained-ticker scope,
status precedence, zero variance, missing/unverified/invalid input, boundaries,
snapshot provenance, dependency digests and atomic series/session publication.
Do not depend on future-label columns.

Acceptance per substage: actual implementation and passing shared disposable
parity/recovery tests, including fractional volumes, extreme prices, AAP/AAp,
missing sessions, constant/nearly constant windows and chunk boundaries as
applicable. Record commands/artifacts in each handoff. Speedup is not required
to build/test a candidate; Stage 9 measures it and Stage 10 applies the 10% rule.

## Stage 8 — Required set-based SQL candidates (high; execute separately)

Prerequisite: Stage 6 harness and Stage 7 handoffs. Implement database-resident
set-based operations, with stored procedures for bounded orchestration where
appropriate. A procedure wrapping per-row client loops is not this candidate.
Check actual MySQL version/features and permissions. Install routines only in
the disposable schema with registry-resolved identifiers and explicit versions.
Preserve caller-controlled commits; install DDL before trial transactions.
Unsupported features/privileges are blockers, not a completed SQL evaluation or
permission to select another backend before the comparison is complete.

**8a — SQL raw adoption/repair.** Stream validated exact-decimal/exact-case archive
records into isolated staging tables through a measured loader. Implement bounded
`INSERT ... SELECT`/joins and equivalent set-based reconciliation, preserving
existing exact ticker/date IDs and Stage 5 collision policy. Match Python's
adoption-only, repair, receipt and restart behavior. Validate before narrowing
numeric types and reject overflow/truncation. Do not silently enable global
server/client options or assume file-loading privileges.

**8b — SQL derived population.** Prerequisite: 8a. Reproduce all supported legacy
derived columns using bounded joins/window/aggregate expressions with correct
preceding/following exchange-session dependencies, ranking/ties and NULL rules.
Match the reference under existing persistence tolerances and corrected DDL.
Preserve dependency invalidation, receipts and session transaction units.

**8c — SQL z-scores.** Prerequisite: 8b. Compute both families/windows using
`AVG`/`STDDEV_POP` or a numerically stable equivalent and set-based publication.
Represent consecutive exchange sessions explicitly: the last N available raw
rows must not bridge gaps. Aggregate status/validity as well as values; SQL
aggregates ignoring NULL must never make an incomplete window ready. Match
population variance, float32 storage/tolerances, zero-variance classification,
status precedence and full retained-ticker scope. Use verified stored delta-minus-one
inputs, required close dependencies and the same snapshot/evidence/publication
contract. Measure any client hashing/metadata work; shared orchestration may
retain it if semantics match.

Acceptance per substage: actual SQL implementation, bounded EXPLAIN evidence,
registry/schema/routine identity, runnable harness commands and passing shared
numerical, identity, rollback/interruption, changed-input and idempotency tests.
SQL must pass Python/Java's same fixtures; no speedup is required to evaluate it.

## Stage 9 — Mandatory three-backend measured comparison (medium)

Prerequisite: Stages 6–8 complete. Execute separate conversations in order:
**9a raw adoption/repair**, **9b derived computation plus persistence**, then
**9c both z-score families/windows plus persistence**. Each runs Python, Java
and SQL against the same manifest under Stage 6's protocol. Separate candidate
debugging from measurement; changed code/schema/inputs invalidate affected trials
and require their complete paired comparison to be rerun.

Include fixed targets and the chronological block, one/three-preparer results
where applicable, first plus three warm repeats, all raw scenarios and full
z-score status populations. Prove identical starting targets and immutable source
inputs; independent repeatable-read transactions are not a shared snapshot.
Check canonical results and recovery outcomes before ranking any candidate.

Write `benchmark/candidates/raw/`, `derived/` and `zscores/` per-run JSON, sanitized
plans/metrics, warm medians and throughput. Aggregate all nine workload/backend
cells in `benchmark/candidates/study.json`; distinguish measured, failed,
timed-out and blocked cells. Preserve old reports/hashes. Tests/read benchmarks
cannot substitute for actual committed write timings. No candidate may be omitted
because it was absent before this revision.

Acceptance per substage: all three correct candidates measured with comparable
end-to-end boundaries and recovery evidence, or an explicitly incomplete/blocked
handoff with partial results. Failure, timeout or absence cannot yield a completed
comparison. After 9c passes, mark the expanded comparison objective complete in
the new aggregate/handoff without editing historical Stage 2 flags. Next: Stage 10.

## Stage 10 — Select implementations and freeze rollout (high)

Prerequisite: all Stage 9 comparisons and parity/recovery gates complete. For
each workload compare the best valid configuration's warm median startup-inclusive
end-to-end time. Improvement is `(Python - alternative) / Python`. Java and SQL
must each be at least 10% faster than that workload's Python baseline to qualify.
Select the lowest eligible median; retain Python if neither alternative qualifies.
For exactly equal eligible medians prefer SQL for database-resident derived/
z-scores and Java for raw ingestion. Report single-/three-preparer speedups
separately. Do not infer write speedup from readers or database-only SQL timing.
Allow mixed pipelines only with measured transfer/staging/orchestration costs.

Recheck counts and disk capacity. Earlier inventory had 36,866 ticker IDs and
5,130 sessions: four dense z-score tables could contain 756,490,320 rows before
metadata, indexes, staging, backups and rebuild overhead. Preserve NULL/status
populations; do not estimate dense storage from raw-row counts. Extrapolate
runtime/disk from measured workloads/counts, label estimates and include DDL/
table-copy, temporary storage and recovery headroom.

Version and update `decision.json`, retaining the provisional decision's identity
in the new report. Record report/input/schema/code hashes, reader, per-workload
processor/writer, SQL loader/orchestrator, transaction boundaries, batch/chunk
limits, workers, memory, threshold/tie calculations and reasons. Start with one
session per raw/derived transaction, one series/session for z-scores and a
chronological 20-session processing chunk; reduce measured limits for memory.
Retain at most three queued sessions and one writer. Production readiness waits
for Stage 11 integration/recovery and Stage 12 restore/pilot gates.

Acceptance: all comparisons considered, parity-backed per-workload selection,
explicit capacity limits and updated Stages 11–15 commands/configuration using
measured entry points. No production mutations occur in this stage.

## Stage 11 — Selected population and recovery integration (high)

**11a — Integrate selected backends.** Prerequisite: Stage 10 decision and Stage 5
contracts/audit. Wire selected raw, derived and z-score backends into the bounded
offline frontend; do not assume one language wins all workloads. Preview/version
selected SQL routine DDL through the registry only if SQL won. Do not reimport
matching raw rows merely to establish evidence: atomically verify current
content and insert the adoption receipt. Replace only the authorized validated
session's differing raw set, preserving mapped IDs and database-only history.

Commit session data, receipts and database completion metadata together; expose
completion after commit and then persist external checkpoints. Retain provenance
kind/adoption time and unknown original receipt time. Recheck raw dependencies
inside the transaction, invalidate affected prior/current/next derived results
and dependent z-score windows, and rebuild chronologically. Retain file/source/
record identities in restart checks. Record actual audit/apply/pilot/z-score
commands with explicit decision/evidence/cutoff paths.

**11b — Integrated recovery proof.** Prerequisite: 11a. Exercise the complete
selected pipeline in disposable storage, including any language/SQL handoffs.
Prove rollback, before/after-commit interruption, stale checkpoints, changed
input/schema/code rejection, dependency races, invalidation and idempotency.
Match candidate decimals, tolerances, evidence and commit units. Independently
recompute derived/z-scores and check exact raw adoption. If integration changes
measured work materially, return to affected Stage 9 trials and Stage 10 selection.

Acceptance per substage: runnable selected integration and passing disposable
end-to-end/recovery tests, concrete commands and a handoff. No production mutations
occur before Stage 12.

## Stage 12 — Backup, precision migration and pilot (medium)

Prerequisite: all preceding acceptance criteria, including Stage 11b recovery.
Inspect current source binding, engines, column definitions, date/ticker IDs,
disk space and competing writers.
Prove backup usability by restoring to an isolated disposable database and
checking schema and representative row counts/hashes. Refresh the backup if
needed for a consistent current restore point. Never restore over production.

Preview the selected registry/DDL. Apply tested ticker-collation/numeric-column
migrations, preserving IDs and indexes; account for table-copy disk and duration
on the 44-million-row table. Initialize only missing compatible support tables
and install versioned registry-resolved routines if SQL was selected. Confirm
production schemas/routines match the benchmarked definitions before applying data.
Choose the latest complete calendar month represented in the archive, requiring
every expected exchange session, plus neighboring dependencies. If no complete
month exists, use the latest 20 consecutive represented exchange sessions and
record the fallback. Run the explicit offline pilot through selected raw/derived
backends and bounded 20/60 z-score samples for both families, with required
history/status outcomes. Independently audit each selected backend's persisted
rows, receipts, dependencies and metadata; repeat the pilot to prove idempotency.

Acceptance: restore report, before/after schema and pilot reports, unchanged
second-run receipts/rows and no unexplained raw, derived or z-score differences.
Mark production readiness only after these gates pass. Stop full rollout on pilot
failure and preserve evidence for diagnosis.

## Stage 13 — Populate remaining retained history (medium)

Prerequisite: Stage 12 pilot passed. Run chronological bounded chunks using the
Stage 10 decision, Stage 11 entry points and the same source/evidence/cutoff.
Use the selected raw/derived backends, measured limits and one writer. Write
durable completion markers after validated commits. Recheck each file hash before
consumption and database
revision before applying cached repairs. Finish all valid archive sessions;
record invalid files and missing dependencies without synthetic replacements.

Validate each chunk and its boundary dependencies. Recover interrupted chunks
using the implemented restart protocol; do not reset progress or blindly trust
the newest date. Save coverage, original/current row counts, receipt counts,
repaired/matching sessions and unresolved issues to `population.json`.

Acceptance: every adopted session reconciles exactly, raw/derived receipts are
valid, original ticker/date mappings are retained, and rerun is idempotent.

## Stage 14 — Populate z-scores (high)

Prerequisite: Stage 13 verified raw/derived archive inputs, Stage 5d snapshot
availability, Stage 12 pilot and the Stage 10 selected z-score backend. Use its
Stage 11 entry point, including versioned routines only if SQL was selected.
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

## Stage 15 — Independent final audit and operating handoff (medium)

Prerequisite: Stage 13 population and Stage 14 z-score reports. Run bounded
independent archive/raw comparison over the complete retained scope and recompute
derived/z-score values
using the semantic reference. Recheck global mapping/orphan integrity once.
Aggregate reports without hiding failed or omitted findings. A gap can remain
unresolved, but must prevent a claim of fully verified continuous coverage.

Update operations/database-validation/performance documentation with actual
commands, all three candidates' benchmark findings, per-workload selected backends,
SQL routine/loader versions if used, decimal migration, evidence
semantics, table populations, known limitations and resume instructions. Correct
the old six-table base-schema wording to eight. Keep live refresh policy and
training/simulation setup separate from this offline archive migration.

Acceptance: final audit reports exact adopted raw agreement, valid derived and
z-score dependencies, counts for every support/materialization table, tested
recovery and a complete list of unresolved gaps and excluded data. Validate using
the independent semantic reference, not only the selected backend's own formulas.

## Revised-sequence handoff — 2026-10-19

- Stage 2c's committed review did not time Java writes; SQL was not implemented
  or measured. Neither absence is a reason to waive the required comparisons.
- Stages 1–2 and historical handoffs below are retained. Their reader/derived
  parity and reports remain workload-specific evidence. The expanded comparison
  objective is pending until Stages 9a–9c pass; old `complete` flags are not proof
  of writer, SQL or z-score evaluation.
- Stage 3 now carries forward evidence; Stages 5–8 establish contracts, a Python
  baseline and actual Java/SQL candidates before measurement/selection in 9–10.
  The same at-least-10% improvement over Python applies to Java and SQL selection.
- Production integration/recovery is Stage 11; backup/schema/pilot gates are 12;
  full raw/derived population, z-scores and final audit are 13–15. No candidate
  benchmarks, implementation changes or database mutations were performed by
  this documentation revision. Next prompt: execute Stage 3 only, Luna medium.

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
