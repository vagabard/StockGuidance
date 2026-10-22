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

**Resume here: Stage 7b Java z-scores, Luna high.** Stage 7a's MySQL
parity/recovery evidence is recorded in the latest handoff. Stage 5c's complete read-only audit passed
its readiness gate on 2026-10-20; documented data gaps remain (see the latest
handoff). Stages 1–4 and their historical handoffs
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
- The database engine for candidate writes and comparisons is MySQL. Use a
  distinct `stockguidance_disposable_...` schema on the configured server, with
  corrected Stage 5 numeric types, exact-case keys, InnoDB, and applicable
  validation triggers/routines. Read credentials from the existing `dbinfo.id`
  and override only the disposable schema in memory. SQLite is an explicit
  offline fixture option; its measurements cannot satisfy MySQL parity or
  performance gates. Never add a SQLite JDBC driver to resolve a target mismatch.
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

Create disposable MySQL schemas from corrected Stage 5 DDL and the authoritative
registry, including equivalent indexes, constraints and applicable production
triggers/routines. Inventory their overhead rather than silently omitting it.
Keep source data immutable; reset targets to identical starting states outside
timing and report setup/reset time and disk separately. Refuse production DDL/DML
targets. Production access is limited to explicit bounded read-only extraction.
The benchmark CLI defaults to MySQL and requires `--disposable-schema` for
candidate operations; `--storage sqlite` explicitly selects offline fixtures.
Use fresh schema names for independent recovery trials. Existing schemas are
reused only after manifest, registry, precision, actual DDL and routine/trigger
fingerprints pass; the MySQL harness never drops or resets a schema.

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

**7a — Java writer.** Prerequisite: Stage 6. Implement a bounded MySQL JDBC candidate
for raw adoption/repair and derived persistence in a disposable MySQL schema. Use
BigDecimal, exact-case mappings, batch statements, and Python's input contract,
session transactions, evidence and metadata. Reuse the existing Java derived
formulas after Stage 5 parity updates. Include matching raw adoption without
rewriting bars and file-hash/source/dependency rechecks. Prove rollback,
before/after-commit interruption, changed input, stale checkpoint and idempotent
rerun behavior. Record the harness entry point and runnable command.

**7b — Java z-scores.** Prerequisite: passing Stage 7a MySQL trial in
`output/legacy-migration/benchmark/candidates/java-writer-mysql.json` and its
latest handoff. Reuse the guarded MySQL target/registry and the unchanged
Stage 6 manifest. Historical SQLite trials do not satisfy this prerequisite.
Implement both price and price-delta
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
All three backends must use equivalent disposable MySQL targets with the same
server version, corrected column definitions, indexes and validation objects.
Replace the historical SQLite Python write timings with MySQL measurements for
every workload. Stage 7a correctness trials and single-process MySQL diagnostics
are also excluded from ranking; they do not include the Stage 9 timing protocol.

Include fixed targets and the chronological block, one/three-preparer results
where applicable, first plus three warm repeats, all raw scenarios and full
z-score status populations. Prove identical starting targets and immutable source
inputs; independent repeatable-read transactions are not a shared snapshot.
Check canonical results and recovery outcomes before ranking any candidate.

Stage 9c execution revision (2026-10-22): use the independently resumable
measurement/audit steps in `docs/stage9c_execution.md`. The user-requested
`compact` profile may replace the 45 target slots with five fixed and three
chronological dates, while retaining all tickers, dependency history, both
families/windows and all four repetitions for every backend. Record its narrower
scope explicitly; never merge full-profile and compact-profile timing cells.
Stage 9a/9b remain unchanged. A completed compact report establishes the revised
sampled comparison, with Stage 10 still responsible for interpreting variability
and deciding whether more paired evidence is needed before selection.

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
- Revised Stage 3 is complete. `output/legacy-migration/evaluation-plan.json`
  preserves the historical evidence and defines all nine required comparisons.
  Stages 5–8 establish contracts, a Python baseline and actual Java/SQL candidates
  before measurement/selection in 9–10. The same at-least-10% improvement over
  Python applies to Java and SQL selection.
- Production integration/recovery is Stage 11; backup/schema/pilot gates are 12;
  full raw/derived population, z-scores and final audit are 13–15. No candidate
  benchmarks, implementation changes or database mutations occurred in Stage 3.
  Next prompt: execute Stage 4 only, Luna medium.

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

### Revised Stage 3 evidence plan completed — 2026-10-19

- Verified the committed Stage 2c review and the Stage 2a/2b report SHA-256
  values. The exact selected 25 paths agree across both studies and the saved
  request; all 25 targets and all 75 recorded target/dependency inputs matched
  their original hashes. No inputs were missing or drifted, and no benchmark was
  rerun or sample substituted.
- Carried forward Stage 2a reader results and Stage 2b's 16-column computation
  parity, plus its 412 missing stored rows and zero of 25 sessions matching all
  columns. The provisional decision remains reader-only; production processor
  and writer are unset.
- Flagged a historical count mismatch for review: the hashed Stage 2a study
  reports 217,734 rows per repeat, while its older handoff and Stage 3 prompt
  state 217,465, a difference of 269. Both values and the original reports remain
  unchanged; this stage did not rerun the study.
- `output/legacy-migration/evaluation-plan.json` freezes the nine mandatory
  Python/Java/SQL workload cells and Stage 6 protocol; SHA-256:
  `9ef501a41f3f5348ff27f4e456326674a4979375b8be563d02f970bb82d30911`. All
  comparisons remain pending until Stages 6–9; the expanded objective is
  incomplete. No database access or mutations occurred. Next: Stage 4 only.

### Revised Stage 4 source acceptance recheck completed — 2026-10-19

- Rechecked the exact recorded nine-file sample under seed `20261019`; no
  files were resampled or substituted. All records were parsed again with the
  exchange-calendar and `America/New_York` timestamp checks. The sample passed
  with 77,783 records, original file hashes, normalized record IDs and counts
  unchanged.
- Inventory identity remains `06048bc49a326b585376b2d3a5a401664bfb28895e2ffa9f0f2e48bab7b60ebd`
  across 5,130 retained files. The explicit source identity remains adjusted
  Polygon/Massive grouped US stocks excluding OTC. Operator attestation remains
  the only provenance claim; original network receipt times remain unknown.
- Python recheck evidence is persisted in
  `output/legacy-migration/source-acceptance.json` under `stage4_recheck`.
  Java's three-worker evidence matches all nine file hashes, record IDs and
  counts; `cross_language.passed` is true. The read-only database comparison
  was rerun against the same paths and still reports the known differences
  (`matches: false`, `import_receipts_created: false`); this does not fail the
  source gate and created no writes or receipts.
- Added the deterministic `--recheck` path to
  `src/stockguidance/cli/verify_archive_sample.py`. Focused validation passed:
  `python -m pytest tests/stage6/test_archive_sample.py` (5 tests). Stage 4
  source acceptance is complete; next prompt: Stage 5a0 only.

### Revised Stage 5 contracts and audit — 2026-10-19

- Implemented the separately gated Stage 5 contracts. The new
  `stockguidance.cli.legacy_migration` frontend defaults to audit mode and
  rejects `--apply` until the Stage 11/12 gates. It supports bounded identity,
  precision, evidence, audit and fixed-cutoff snapshot operations; checkpoints
  and reports contain no credentials or fabricated HTTP receipt metadata.
- Stage 5a0's exact retained-archive key inventory found 33,277 keys and the
  read-only database label inventory found 36,866 IDs: 29,882 exact matches,
  6,984 one-to-one uppercase aliases eligible for ID-preserving rename after
  collation migration, 103 folded collisions and one unresolved alias
  (`KBRW` against `KBRw`/`KBrw`). The required exact-case examples include
  `AAP`/`AAp` and `ABKPZ`/`ABKpZ`; IDs are never merged by case folding.
  Evidence: `output/legacy-migration/ticker-identity.json`.
- Stage 5a streamed all 5,130 retained files. It validated 42,482,212
  records in 4,898 files and quarantined 232 files with invalid archive
  records. Valid source maxima require `DECIMAL(28,8)` for OHLC,
  `DECIMAL(42,22)` for VWAP and `DECIMAL(20,8)` for fractional volume;
  transactions remain integral. The measured schema hash is
  `00d2f5da9dbc1e0353a7fbf9da8fb9fd78aa1d0fb57ac63d2caecf3def7f6101`.
  Evidence: `output/legacy-migration/precision-inventory.json`.
- Stage 5b materialized typed evidence for all 4,898 valid files under
  `output/legacy-migration/evidence/`. It stores source identity, attestation
  ID, original path/hash, versioned exact records ID/count, validation time and
  an explicit unknown original network receipt time. It rejects network
  status/count/pagination fields and changed file content.
- Stage 5d's offline snapshot contract passed a fixed aware-cutoff smoke test
  against a materialized evidence record. It requires evidence/file hash
  agreement, a nonfuture aware cutoff and validated-at-or-before-cutoff data;
  it does not construct provider responses or invented observations.
- Stage 5c's archive-side audit report covers all 5,130 retained sessions with
  the 232 invalid files explicit, but the configured read-only database
  connection timed out after the bounded retry. No database rows, receipts,
  source files or schema were changed. `output/legacy-migration/audit.json`
  therefore records `complete: false` and `database_available: false`; the
  database-side audit, orphan counts and exact row comparison remain blocked
  until connectivity is restored. Do not advance to Stage 6 until 5c is
  rerun successfully.
- Python focused validation passed: 32 Stage 5/archive contract tests; the
  existing Stage 3/4 workflow set passed 65 tests. Offline Java validation
  passed all 32 tests. Production DDL/repairs were not applied.

### Stage 5c audit recovery completed — 2026-10-20

- Database connectivity is healthy. The first failing stored row is `TTE` on
  2005-12-16 with zero OHLC; the original archive also contains these zeros and
  the file was already quarantined by Stage 5a. The audit incorrectly required
  every stored row to satisfy the adoption contract, aborting instead of
  reporting defects. Cursor cleanup then masked the cause with `Unread result
  found`. Invalid stored values now remain explicit per-session findings, and
  failure cleanup closes the connection without hiding the original exception.
- Comparisons now use exact archive decimals rather than the historical
  four-place reader. All selected file hashes and valid-file record identities
  are rechecked; supplied typed evidence must match. Changed files, corrupt
  inventories/evidence and conflicting source bindings prevent completion.
  Case-distinct keys, duplicate database keys and database-only dates remain
  explicit. Global orphan queries check both session and ticker mappings once
  per table. The snapshot records the actual schema hash and source binding.
- The unnecessary secondary ticker ordering forced MySQL to sort the entire
  history. Ordering by session alone uses the existing session/date indexes;
  the live `EXPLAIN` confirms that the global sort is eliminated. No index or
  production schema changes were needed. New snapshots recompare every session
  rather than trusting database comparisons from an older checkpoint.
- Run `scripts/test_stage5c_readiness.py --verbose` directly in PyCharm. Paths
  default to this checkout independently of the working directory. The script
  prints progress, full exception chains and bounded examples of invalid rows.
  `--probe-only` tests connectivity without running the full audit and does not
  establish Stage 6 readiness. Configuration contents are not printed.
- Validation: the complete Python suite passed 818 tests; 14 focused Stage 5
  and Stage 8 checks passed after the query-plan adjustment.
- The final live command was `scripts/test_stage5c_readiness.py --verbose`,
  using PyCharm's configured Python. It completed in 4,299.04 seconds and exited
  0. `output/legacy-migration/audit.json` records `database_available: true`
  and `complete: true` across all 5,130 retained sessions (2005-12-16 through
  2026-05-07), including 44,199,460 stored raw rows. All 4,898 valid files passed
  fresh hash, exact records identity and typed evidence checks; 232 unadoptable
  files remain explicitly quarantined. There are 230 invalid stored rows,
  zero duplicate database keys, zero database-only dates and zero global
  raw/derived orphans against either session or ticker mappings.
- `matches: false` is expected and preserved. Among valid archive sessions,
  the audit found 1,945,736 missing keys, 1,896,456 extra keys and 4,145,887
  shared rows with differing fields, including 3,823,262 volume differences.
  These findings and the existing unresolved ticker alias remain inputs to the
  later gated repair stages; audit completion does not certify clean data or
  continuous verified coverage. No production schema, rows, receipts, source
  bindings or original files were changed.
- Evidence: `output/legacy-migration/audit.json`, `audit-checkpoint.json`,
  `audit-summary.json` and `stage5c-readiness.log`. The summary binds the full
  report SHA-256 `04708b2a2ba212c9d3a42915dc7defee7b023b9622ff197fe023323b4d17d60a`;
  the actual read-only database schema hash is
  `54ef733f88da618d05913e329956907c5936d6c4de54a37af6d1932baa45e15c`.
- This result supersedes the timeout blocker above. All Stage 5 handoffs now
  exist; Stage 6 may proceed with explicit gaps and isolated disposable targets.
  Next prompt: **Read docs/legacy_migration_prompts.md and execute revised
  Stage 6 only.** Stage 6 was not started by this audit recovery.

### Historical Stage 6 SQLite reference trials — 2026-10-20

This records the original offline implementation. The MySQL correction below
supersedes its target and comparison-completion claims; preserve its artifacts
as historical evidence only.

- Added `stockguidance.performance.candidates` and the
  `legacy_migration_benchmark` CLI. The harness refuses source/configuration
  targets, uses the authoritative registry, records corrected schema/registry
  identity, and creates only isolated SQLite disposable schemas with equivalent
  raw/derived/z-score tables, date indexes, constraints and explicit empty
  trigger/routine inventories. Production DDL/DML flags remain false.
- `output/legacy-migration/benchmark/candidates/manifest.json` binds the exact
  25 Stage 2 paths and hashes, 1,416 required dependency files, preceding and
  following derived context, 60-session z-score context, the chronological
  20-session block ending 2026-04-29, 36,866 retained ticker IDs, database
  mappings, source/snapshot identity and corrected schema hash
  `00d2f5da9dbc1e0353a7fbf9da8fb9fd78aa1d0fb57ac63d2caecf3def7f6101`.
- Python reference trials completed with four sequential invocations per
  workload (first plus three warm repeats): raw adoption/repair/rerun in
  `python-raw.json`, exact-decimal legacy-derived persistence in
  `python-derived.json` (217,734 rows across 25 sessions), and dense price/
  price-delta z-scores for windows 20/60 in `python-zscores.json` (2,949,280
  rows across all 36,866 ticker IDs and 20 target sessions). The aggregate
  `study.json` marks Python complete and Java/SQL pending. Z-score preparation
  streams one ticker at a time; statuses and NULL policy are retained.
- Correctness fixtures cover the disposable-target guard, registry-resolved
  schema, exact fractional volume, exact-case `AAP`/`AAp`, nullable derived
  inputs, rollback/checkpoint behavior and unchanged rerun idempotency. The
  focused command is `python -m pytest tests/stage6/test_legacy_candidates.py`.
- Reproduction commands from the checkout are:
  `PYTHONPATH=src <configured-python> -m stockguidance.cli.legacy_migration_benchmark --build-manifest`
  and `PYTHONPATH=src <configured-python> -m stockguidance.cli.legacy_migration_benchmark --run-python --storage sqlite --manifest output/legacy-migration/benchmark/candidates/manifest.json --workloads raw,derived,zscores`.
  Aggregate with `--aggregate --report output/legacy-migration/benchmark/candidates/python-raw.json --report output/legacy-migration/benchmark/candidates/python-derived.json --report output/legacy-migration/benchmark/candidates/python-zscores.json`.
  Java and SQL timings remain pending Stage 7/8; no production schema, rows,
  receipts, source bindings or source files were changed. Next prompt: Stage 7a
  only.

### Historical Stage 7a implementation-only handoff — 2026-10-20

The implementation-only state below is superseded by the completed MySQL
parity/recovery handoff that follows.

- Added the bounded `stockguidance.migration.JavaWriterCandidate` JDBC writer
  and `stockguidance.migration.JavaWriterMain` entry point under
  `StockGuidance/java`. The candidate accepts only a disposable target marked
  `production_target=false`, verifies the manifest registry and schema marker,
  and records its manifest/source/schema identities in disposable metadata.
- Raw adoption streams one exact archive session, rechecks the original file
  hash and exact-decimal records identity, preserves unchanged bars, uses
  exact-case ticker mappings and batch statements, and commits raw rows,
  import evidence and completion metadata as one transaction. Derived
  persistence reuses `LegacyDerivedCalculator`, requires matching raw
  evidence for the target and both neighboring sessions, and records the
  dependency/content identities atomically.
- `ArchiveCandidateReader` preserves fractional volumes, extreme adjusted
  prices, nullable fields and case-distinct keys such as `AAP`/`AAp`. Changed
  source identities and stale checkpoints fail before publication. Explicit
  before/after-commit interruption hooks support rollback and restart probes;
  after-commit reruns remain idempotent by comparing stored canonical values.
- Focused Java tests cover the exact reader, changed inputs, fractional and
  extreme quantities, exact-case keys, nullable derived inputs and the
  interruption contract. Offline Maven validation passed 36 tests:
  `mvn -o test` from `StockGuidance/java`.
- No disposable JDBC trial was run because the Stage 6 target is SQLite while
  the Java module currently bundles only MySQL Connector/J; no SQLite driver or
  target credentials were added. No production schema, rows, receipts, source
  bindings or source files changed. Stage 7b z-score implementation and all
  Java timings remain pending. Complete the disposable JDBC trial before
  starting Stage 7b.

### Revised Stage 7a MySQL correction and acceptance — 2026-10-20

- Stage 7a is complete. The candidate engine is MySQL 9.5.0 with InnoDB and
  the server's strict SQL mode. The CLI defaults to MySQL; an explicit
  `--disposable-schema` selects a separate database while credentials remain
  in `dbinfo.id`. Schema names must begin `stockguidance_disposable_` and
  differ from the configured production database. No production DDL/DML,
  receipts, source bindings or retained source files changed.
- Added `performance/mysql_candidates.py` and `performance/java_writer_trial.py`.
  They provision registry-resolved MySQL tables with Stage 5 DECIMAL types,
  binary ticker collation, retained ticker/date IDs and date indexes. Existing
  targets require matching manifest, registry, corrected precision, actual DDL
  and routine/trigger fingerprints. Targets are never dropped/reset.
- Read-only inventory found four production raw/derived ticker-validation
  triggers calling `check_ticker_exists`; equivalent validation is installed
  only in the disposable schemas. The legacy `populate_daily_delta_stock_data`
  routine is inventoried but not invoked. This supersedes Stage 6's incorrect
  empty applicable-trigger/routine inventory.
- Fixed decimal parsing in the Java candidate reader: Jackson now constructs
  BigDecimal values directly, retaining `1702500000000000.1` exactly. Also
  aligned derived dependency/content digest encoding with Python, preserved
  derived metadata during unchanged raw adoption, and rejected altered stored
  raw dependencies before derived publication. File identities are rechecked
  before commit.
- `output/legacy-migration/benchmark/candidates/java-writer-mysql.json` records
  the unchanged manifest ID
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`,
  source-code hashes, schema/routine inventory and passing checks for all 25
  fixed real target sessions: 217,734 raw rows and 217,734 derived rows. Matching
  Java adoption changed zero raw rows; every raw decimal/key matched exactly,
  every derived column matched the existing `1e-10` relative / `1e-12` absolute
  tolerance, and persisted dependency/content digests passed independent checks.
  Completed schemas are `stockguidance_disposable_s7a_20261020d` and its `_fx`
  fixture schema; earlier disposable debugging schemas are not acceptance evidence.
  Report SHA-256:
  `7ddbfaf71fe90e6e8fcd4f4ce1e896dc415cab8773ef3c4102b76bbb1da6720c`.
- Eleven MySQL recovery checks passed: raw and derived rollback before commit,
  durable commit before checkpoint, idempotent restart, missing/differing-row
  repair, unchanged adoption preserving derived metadata, stale checkpoint,
  changed input, corrupted stored dependency, and missing-session rejection.
  Fixtures include fractional volume, extreme prices, exact-case `AAP`/`AAp`
  and nullable inputs. `java-writer.json` now points to this passing trial.
- Reproduce from the checkout after `mvn -o test package` in `StockGuidance/java`:
  set `PYTHONPATH=src`, rediscover `$pythonExe` with `get_python_environment`,
  cap native thread variables at one, then run
  `& $pythonExe -m stockguidance.cli.legacy_migration_benchmark --run-java-writer-trial --dbinfo-file dbinfo.id --disposable-schema stockguidance_disposable_s7a_<fresh_suffix> --output output/legacy-migration/benchmark/candidates/java-writer-mysql.json`.
  Use a fresh schema suffix and output for independent repeats. Java checks
  passed 37 tests and packaging; `python -m pytest tests/stage6 -q` passed
  121 tests. Focused MySQL/offline harness tests also passed after the final
  precision-identity and aggregation changes.
- Historical SQLite Python reports remain offline evidence and are excluded
  from MySQL rankings. The new MySQL Python adapter supports the existing
  workloads, but diagnostic runs do not supply Stage 9's startup/resource
  measurement protocol. All three backends still need comparable MySQL
  timings in Stages 9a–9c; no production backend is selected. Stage 7b must
  implement and test both z-score families/windows and their stored-delta,
  provenance/status and atomic publication contracts on this MySQL harness.
  Next prompt: **Read docs/legacy_migration_prompts.md and execute revised
  Stage 7b only.** Stage 7b was not started in this correction.

### Revised Stage 7b Java z-score candidate completed — 2026-10-20

- Added `JavaZScoreCandidate` and `JavaZScoreMain` for guarded disposable MySQL
  publication of price and price-delta z-scores at windows 20 and 60. The
  candidate uses the frozen Stage 6 manifest and registry, dense manifest ticker
  scope, consecutive dependency sessions, population variance, stable float64
  calculation and float32 storage.
- Stored `close_ratio_to_prior` values are read and checked against stored raw
  closes. Status precedence, missing/unverified/invalid inputs, insufficient
  history, zero variance, exact-case mappings and chunked ticker loading are
  explicit. Series/session metadata records source scope, dependency and
  availability digests, status counts and content identity; each series/session
  publishes atomically with its rows and supports post-commit checkpoints.
- Added shared Java parity fixtures for fractional/extreme values, `AAP`/`AAp`,
  constant/nearly constant behavior, missing/unverified inputs and float32
  quantization. `mvn -o test` passed 40 tests; focused Python candidate tests
  passed 11 tests.
- The final disposable MySQL trial used server 9.5.0/InnoDB schema
  `stockguidance_disposable_s7b_20261020h`, 61 dependency sessions and all
  36,866 manifest ticker IDs. Python parity passed for all four series: price
  ready rows 10,951/10,513 and price-delta ready rows 10,917/10,503 for
  windows 20/60. Recovery checks passed for rollback, durable post-commit
  restart, stale checkpoints and changed source input. Artifact:
  `output/legacy-migration/benchmark/candidates/java-zscores-mysql.json`.
  Report SHA-256:
  `267bd4e31a1d9e8faedf6bebd54d7c784630ffe71a3e30f2abb5848e50022663`.
- No production DDL/DML, receipts, source bindings or retained source files
  changed. This is correctness evidence only; Stage 9 still owns comparable
  startup-inclusive MySQL measurements. Next prompt: **Read
  docs/legacy_migration_prompts.md and execute revised Stage 8a only.**

### Revised Stage 8a SQL raw candidate completed — 2026-10-20

- Implemented the guarded SQL raw candidate in
  `src/stockguidance/performance/sql_raw_candidate.py`. Validated archive files
  are loaded one session at a time into the binary-collated
  `stage8a_raw_stage` table with exact Decimal strings, corrected Stage 5
  numeric definitions, full file hashes and exact records IDs. The candidate
  does not assume `LOAD DATA` or file-loading privileges.
- The registry-bound `stage8a_adopt_raw` MySQL procedure performs set-based
  session/ticker mapping, raw `INSERT ... SELECT`/duplicate-key reconciliation,
  import receipt publication and completion publication. It preserves existing
  exact ticker/date IDs and derived metadata, has no internal commit, and is
  installed only in disposable schemas. Its bounded EXPLAIN uses the staging
  primary key for the session restriction and the ticker label index.
- The final trial used MySQL `9.5.0`, InnoDB, and disposable schemas
  `stockguidance_disposable_s8a_20261020g` plus its `_fx` fixture schema. The
  unchanged Stage 6 manifest identity is
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`.
  The trial matched all 25 fixed sessions and 217,734 raw rows exactly,
  including fractional volumes, extreme prices and exact-case `AAP`/`AAp`.
- Seven recovery checks passed: before-commit rollback, durable post-commit
  restart without a checkpoint, idempotent restart, missing/differing-row
  repair, unchanged adoption preserving derived metadata, changed-input
  rejection and stale-checkpoint rejection. The report records production DDL
  and DML as false and includes the full registry, corrected schema hash
  `00d2f5da9dbc1e0353a7fbf9da8fb9fd78aa1d0fb57ac63d2caecf3def7f6101`, actual
  schema hash `b7d22d4a943ced2eaf832ddf4625640f67d583d96114921165d014280a7e067a`,
  routine `stage8a_adopt_raw`, routine hash
  `f85fb7df0cbc924f64147358a3b9fef86f5408aadbedb4490a2e41af12ad349a` and
  object hash `36fa235486140ebf1439aa06126968b428454a1877d5002f9a33ff2366a0ed8d`.
- Artifact: `output/legacy-migration/benchmark/candidates/sql-raw-mysql.json`;
  SHA-256 `ef6f43b7e4a507448f42fe1b8608cc36af8f61a66b740ac720678a78394b7d2e`.
  Reproduce with a fresh `stockguidance_disposable_...` schema using
  `& $pythonExe -m stockguidance.cli.legacy_migration_benchmark --run-sql-raw-trial --dbinfo-file dbinfo.id --disposable-schema <fresh_schema> --manifest output/legacy-migration/benchmark/candidates/manifest.json --output output/legacy-migration/benchmark/candidates/sql-raw-mysql.json`.
- Focused SQL/MySQL tests passed 13 tests; the complete Stage 6 suite passed
  126 tests with three pre-existing PyTorch warnings. No production schema,
  rows, receipts, source bindings or retained source files changed. Stage 8a
  correctness is complete; Stage 9 still owns comparable startup-inclusive
  timings. Next prompt: **Read docs/legacy_migration_prompts.md and execute
  revised Stage 8b only.**

### Revised Stage 8b SQL derived candidate completed — 2026-10-20

- Implemented the guarded SQL derived candidate in
  `src/stockguidance/performance/sql_derived_candidate.py` and added the
  `--run-sql-derived-trial` CLI entry point. The candidate reuses Stage 8a's
  registry-bound raw target, stages one target/dependency identity per session,
  and invokes `stage8b_populate_derived`, a MySQL set-based procedure using
  bounded date-ID joins, `ROW_NUMBER()` ranking with label tie-breaks, and
  NULL/zero-denominator rules matching `legacy-open-to-close-v1`. It uses an
  approximate numeric operand for float64-equivalent division precision.
- The routine publishes all 16 derived columns, dependency/content receipts
  and `stage6_completion` in the caller's one-session transaction. It has no
  internal commit and rejects missing session mappings, mismatched raw receipt
  identities and incomplete current populations. Client work is limited to
  validated archive/raw dependency checks, reference tolerance checks,
  checkpoint handling and metadata staging; it does not loop over rows to
  publish derived values.
- The final disposable MySQL 9.5.0/InnoDB trial used
  `stockguidance_disposable_s8b_20261020h`, the unchanged Stage 6 manifest
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`, and
  all 25 fixed sessions. It matched 217,734 derived rows and passed the
  fixture/reference parity, bounded EXPLAIN, exact receipt identity,
  before-commit rollback, durable post-commit restart, idempotent rerun, stale
  checkpoint, changed input, stored dependency corruption and missing
  dependency checks. Schema hash:
  `e8e96f2fc30cf7c68fef49865d371b685e378644f995a1659603b11e0ffe0e7f`;
  object hash:
  `82ac39c5d35c0d20d46f82f60ebdd72ba29ab614e92e98b7c34580e506949463`;
  `stage8b_populate_derived` definition hash:
  `d0f6cef39af9aa504db84b8f84d05cf59fdfe88c93ce695e044b55220d0cb0ae`.
- Artifact: `output/legacy-migration/benchmark/candidates/sql-derived-mysql.json`;
  SHA-256:
  `49e04e0b84a3fc3ca462e0b9825832ce603b9784e0be5c5fbb4d052281f48d3`. Focused SQL/MySQL
  tests passed 15 tests. Production DDL/DML, receipts, source bindings,
  retained source files and production rows were unchanged. Stage 9 still owns
  comparable startup-inclusive timings; no backend is selected. Next prompt:
  **Read docs/legacy_migration_prompts.md and execute revised Stage 8c only.**

### Revised Stage 8c SQL z-score candidate completed — 2026-10-20

- Implemented the guarded set-based SQL z-score candidate in
  `src/stockguidance/performance/sql_zscore_candidate.py` and added the
  `--run-sql-zscore-trial` CLI entry point. Dependency sessions are staged with
  explicit ordinals and expected raw identities; the SQL procedures use the
  retained manifest ticker scope, verified stored `close_ratio_to_prior`
  inputs, bounded consecutive-session windows, `STDDEV_POP`, status precedence,
  zero-variance classification and float32 publication. SQL aggregates cannot
  make an incomplete or NULL-containing window ready.
- Publication is registry-bound and atomic in the caller transaction. The
  candidate records series/session metadata, dependency and availability
  digests, status counts, `stage6_completion`, and checkpoint identities. Client
  work is limited to validated source checks, input hashing, reference parity
  checks and disposable raw/delta seeding; z-score rows and metadata are
  published by `INSERT ... SELECT` procedures without a client row loop.
- Focused SQL candidate coverage passed 10 tests, including registry/staging
  collision guards, set-based routine structure, shared series identity and
  no internal commits. The final disposable MySQL trial used version 9.5.0,
  InnoDB, schema `stockguidance_disposable_s8c_20261020g`, target
  `2026-04-29`, 61 dependency sessions and all 36,866 retained ticker IDs.
  Python reference parity passed for all four series: price ready rows
  10,951/10,513 and price-delta ready rows 10,917/10,503 for windows 20/60.
  Recovery checks passed for before-commit rollback, durable post-commit
  restart, idempotent rerun, stale checkpoint rejection and changed input.
- Artifact:
  `output/legacy-migration/benchmark/candidates/sql-zscores-mysql.json`;
  SHA-256 `745feb7f472505093d40309bf1232c4f3645fbea4dee0bc90b7daa9ebd381320`.
  The report is `complete`, comparison-eligible, records MySQL/schema/routine
  identity and reports client hashing separately. No production DDL/DML,
  receipts, source bindings, retained source files or production rows changed.
  Stage 9 still owns comparable startup-inclusive timings; no backend is
  selected. Next prompt: **Read docs/legacy_migration_prompts.md and execute
  revised Stage 9a only.**

### Revised Stage 9a raw comparison completed — 2026-10-20

- Added the bounded MySQL Stage 9a runner in `src/stockguidance/performance/stage9_raw.py` and exposed it through `--run-stage9-raw`. Python uses one or three preparation processes with one writer; Java uses one or three preparation threads with one JDBC writer and `-Xmx512m`; SQL uses one database writer connection. The runner includes process startup, source hashing/decoding, staging, durable commit and checkpoint work in end-to-end timing, samples process-tree RSS/CPU when available, records query counts where candidates expose them, rotates the four manifest candidate orders, and saves partial reports after every trial.
- The exact Stage 6 manifest was used unchanged: 25 fixed target sessions and the original source identities. Each backend/configuration completed matching adoption, missing/different-row repair and unchanged rerun scenarios with one first invocation plus three warm repeats. All 60 timed trials passed independent exact-decimal, exact-case raw audits and receipt/completion identity checks. No source files, production DDL/DML, production rows, receipts or bindings changed.
- The disposable MySQL server reported version `9.5.0`; all candidate tables used InnoDB and the corrected Stage 5 schema. Candidate setup/reset and correctness audits are outside timed boundaries. Server CPU/I/O/temp-table metrics were unavailable to the configured account and are labeled unavailable; client process-tree resource sampling is retained per trial.
- Combined warm medians across the three raw scenarios were Python `p1` 12.840 seconds and `p3` 13.070 seconds, Java `p1` 43.280 seconds and `p3` 42.150 seconds, and SQL `p1` 23.910 seconds. These are comparison measurements only; Stage 10 selection has not run. The Java query counter is unavailable; Python and SQL report their bounded client statement-count scopes separately.
- Artifacts are under `output/legacy-migration/benchmark/candidates/stage9a-20261020/`: `stage9a-raw.json` (SHA-256 `4982D30F0A007B839C4993CC1FB64A9EDB47E1ED342537CA87FB5506B6ACED4C`), `study.json` (SHA-256 `0B06009984D10BCBE04EF7C93EA550F9BBDF82A1095F11F1519CC8DE7AF8B859`) and per-configuration reports under `raw/`. The Stage 9 aggregate has all nine cells, with raw adoption/repair measured and derived/z-score cells explicitly pending.
- Focused Python candidate tests passed 16 tests; Maven Java tests passed 40 tests and packaging succeeded. Existing Java and SQL raw recovery artifacts remain passing and are referenced in the Stage 9a report. Next prompt: **Read docs/legacy_migration_prompts.md and execute revised Stage 9b only.**

### Revised Stage 9a raw comparison rerun — 2026-10-21

- Reran the complete Stage 9a matrix after the Java raw candidate improvements
  from `5b1a2e5` (buffered archive reads, shared raw-write state and bounded
  JDBC batches). The later `38a9763` commit changed IDE build metadata only;
  it did not change the measured Java sources. `mvn -o test package` passed 49
  Java tests and rebuilt the packaged runtime dependencies.
- The unchanged Stage 6 manifest was used: ID
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`,
  manifest SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`.
  Python, Java and SQL each ran matching adoption, missing/different-row
  repair and unchanged rerun with one first invocation plus three warm
  repeats, rotated through all four candidate orders. All 60 trials passed
  independent exact-case, exact-decimal audits over 217,734 fixed-session
  rows or 7,758 repair/rerun rows as applicable. MySQL `9.5.0`, InnoDB,
  corrected disposable schemas and one writer per candidate were used; no
  production DDL/DML, receipts, bindings or source files changed.
- Fresh artifacts are under
  `output/legacy-migration/benchmark/candidates/stage9a-20261021/`:
  `stage9a-raw.json` (SHA-256
  `FB81A34D121ECB09261642E9D88B0394F8E7390249142D0DB4FC30339E7C2353`) and
  `study.json` (SHA-256
  `6E360243F90654404B0E66481111735A57950A6C5C803B19F8C677B134D3174A`).
  The raw report is `complete`; its aggregate remains `incomplete` only
  because Stage 9b and 9c are still pending. Server resource metrics remain
  unavailable to the configured account, while process-tree RSS/CPU sampling
  and all bounded timing fields were retained per trial.
- Warm medians across the three raw scenarios were Python p3 `11.31` seconds,
  Java p1 `10.36` seconds and SQL p1 `21.65` seconds. Java was approximately
  8.37% faster than the best Python configuration for this workload; this is
  below Stage 10's 10% qualification threshold and is not a backend
  selection. SQL was approximately 91.44% slower. The Java JDBC query count
  remains unavailable; Python and SQL statement-count scopes are recorded in
  the report.
- Focused validation passed: Python `tests/stage6` passed 131 tests with
  three existing model warnings, and Java packaging/tests passed 49 tests.
  Stage 9a is complete; preserve this rerun and the 2026-10-20 report as
  separate evidence. Next prompt: **Read docs/legacy_migration_prompts.md and
  execute revised Stage 9b only.**

### Revised Stage 9b derived comparison completed — 2026-10-21

- Implemented `src/stockguidance/performance/stage9_derived.py` and exposed
  `--run-stage9-derived`. The runner uses fresh guarded MySQL schemas, one
  writer, one or three Python/Java archive preparers, one SQL database writer,
  at most three queued sessions, four candidate-order rotations, and separate
  fixed-25 and chronological-20 target blocks. It saves partial per-trial and
  per-configuration reports, process-tree RSS/CPU, startup/handoff time,
  preparation/write phases, SQL database-resident time, checkpoints and
  correctness audits outside the timed boundary.
- Python now has a prepared exact-derived MySQL publication path. Java adds
  `JavaDerivedBenchmarkMain` and a prepared-archive writer path using one JDBC
  writer connection. SQL uses the existing Stage 8b set-based routine through
  a prepared staging identity; its source decoding/content identity phase is
  reported separately from database-resident publication. All candidates use
  `legacy-open-to-close-v1`, exact-case keys, existing derived tolerances and
  atomic session publication.
- The complete paired rerun used the unchanged Stage 6 manifest ID
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`,
  manifest SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`,
  MySQL `9.5.0`, InnoDB, corrected Stage 5 types and no production DDL/DML.
  All 40 timed trials passed independent derived parity, receipt and
  completion audits; no source files, production rows, bindings or receipts
  changed. The initial `stage9b-20261021` attempt is retained as a failed
  diagnostic after a SQL worker report-field bug; it is not comparison
  evidence. The valid rerun is under
  `output/legacy-migration/benchmark/candidates/stage9b-20261021-rerun/`.
- Warm end-to-end medians across both blocks were Python p1 `46.6790867`
  seconds, Python p3 `47.6287887`, Java p1 `41.1390557`, Java p3
  `42.0641026`, and SQL p1 `57.5223325`. Best derived throughput was Python
  p1 `9803.1695` rows/second, Java p1 `11123.3229`, and SQL p1 `7955.2233`.
  Java p1 is approximately `11.87%` faster than Python p1 for this workload;
  this is comparison evidence only and does not select a production backend.
  SQL database-resident phase timings and unavailable server metrics remain
  explicitly recorded in the report.
- Artifact `stage9b-derived.json` SHA-256 is
  `D381081B36F3B6D2688B518BE41FF3595B9AB034407492B7B3DED570F3CEF1ED` and
  aggregate `study.json` SHA-256 is
  `D8E9355DC86DE80D734AAEAA444010E6755B5096D1635074239C66C646EC6074`.
  The aggregate reports 9a complete, 9b complete and 9c pending; historical
  Stage 2 and Stage 9a reports remain unchanged. Java and SQL MySQL recovery
  artifacts passed their rollback/restart, stale-input and idempotency checks;
  Python recovery is retained as the existing offline reference evidence and
  is labeled as such.
- Reproduce from the checkout after `mvn -o test package` in `StockGuidance/java`:
  set `PYTHONPATH=src`, rediscover `$pythonExe` with `get_python_environment`,
  cap native thread variables at one, then run:

  ```powershell
  & $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
    --run-stage9-derived `
    --manifest output/legacy-migration/benchmark/candidates/manifest.json `
    --dbinfo-file dbinfo.id `
    --output output/legacy-migration/benchmark/candidates/stage9b-<fresh-run> `
    --stage9a-report output/legacy-migration/benchmark/candidates/stage9a-20261021/stage9a-raw.json `
    --stage9-timeout-seconds 3600
  ```

- Focused Python candidate tests passed 5 tests; Maven packaging/tests passed
  49 tests. Next prompt: **Read docs/legacy_migration_prompts.md and execute
  revised Stage 9c only.**

### Revised Stage 9c z-score comparison implemented — incomplete, 2026-10-21

- Implemented `src/stockguidance/performance/stage9_zscores.py`, the Java
  `JavaZScoreBenchmarkMain` worker, CLI dispatch through
  `--run-stage9-zscores`, and focused coverage in
  `tests/stage6/test_stage9_zscores.py`. The runner validates both fixed-25
  and chronological-20 target blocks, all four manifest candidate-order
  rotations, one first plus three warm repetitions, Python/Java/SQL workers,
  atomic series/session writes, checkpoints, process-tree metrics, dense
  correctness audits and the nine-cell Stage 9 aggregate. It uses one guarded
  disposable MySQL target, preserves seeded raw/delta inputs between output
  resets, batches disposable delta seeding, and supports explicit reuse of a
  verified disposable schema with `--stage9-disposable-schema`.
- Authorized bounded runs were started against the unchanged manifest ID
  `e5d9446e8bae97244edf17824be680eda6e8990bf1b80a2e6629809896022a14`
  (manifest SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`).
  The first rerun recorded a disposable-schema reclamation failure after a
  914.03-second partial Python trial (`Unknown database`); the keepalive and
  schema-reuse recovery paths were then added. Later runs completed seeding
  and reached the Python z-score worker, but the first atomic Python series
  remained CPU-bound beyond practical execution time and was stopped before
  producing a valid timed cell. No 9c backend measurement is comparison
  eligible; no backend is selected and Stage 10 must not run.
- Preserved diagnostics include
  `output/legacy-migration/benchmark/candidates/stage9c-20261021-rerun/`
  (report SHA-256
  `4BA3C386A88C09F036C4BF64C19EB1519E3D5AE0216B843D9A5448FFD8DDB0AA`),
  `stage9c-20261021-final2/`, `stage9c-20261021-final3/`,
  `stage9c-20261021-final4/` and `stage9c-20261021-final5/` (partial report
  SHA-256
  `DC57807FE3CFDC495BFA74663C20877F20BA597B5E08E3E3E09400B4E6296C37`).
  The final reports remain `incomplete`; partial checkpoints and stderr logs
  identify the bounded runtime state. Production DDL/DML, production rows,
  receipts, bindings and retained source files were unchanged.
- Validation passed: `tests/stage6` passed 134 tests with three existing
  model warnings; focused Stage 9c/SQL z-score tests passed 6 tests;
  `mvn -o test package` previously passed 49 Java tests. Do not treat the
  partial 9c runs as performance evidence. Next prompt: resume Stage 9c only
  after optimizing or otherwise bounding the Python z-score implementation.

### Stage 9c optimized and prepared for rerun — 2026-10-21

- Corrected the previous runtime diagnosis: `stage9c-20261021-final5` contains
  72 committed series checkpoints covering 18 of 25 fixed targets. Their file
  timestamps span 54 minutes 39 seconds. The trial was progressing slowly;
  it did not remain stuck on its first series. Its incomplete results remain
  ineligible for comparison.
- Python now uses a bounded NumPy kernel; Java uses primitive arrays and the
  Java 26 Vector API across tickers. Both preserve population windows,
  float64 arithmetic, float32 storage, status precedence and NULL outcomes.
  Exceptional Python precision cases retain the independent scalar fallback;
  Java uses compensated anchored moments and scalar fallback for non-finite
  SIMD results. Maven and Java worker launches enable `jdk.incubator.vector`.
- Each Python series loads at most 1,000 tickers per batch and shares that
  transaction's input snapshot between calculation and streaming canonical
  dependency hashes. The accumulating result cache is removed; calendar
  preparation is reused within a target. SQL hashing is also batched.
  No database snapshot is cached across commits. The scalar reference avoids
  constructing unused missing observations.
- All three timed workers verify each required valid source archive once per
  worker invocation, with independent caches for every repetition. They retain
  transactional schema/mapping/receipt/dependency checks. The orchestrator checks
  immutable archive hashes, seeded database fingerprints and candidate source/
  class hashes around trials. Reused schemas must carry a verified seed identity;
  earlier unmarked schemas cannot be reused. Fresh output directories are required.
- Reports include phase timings, verified-archive counts and atomic progress
  snapshots; timeouts preserve committed series. Independent scalar expectations
  use checksummed disk caches keyed by input and code identities. Numerical
  comparisons retain existing tolerances; metadata hashes are checked against
  actual stored float32 rows. MySQL reads widen FLOAT values before text transfer
  to avoid losing stored bits. SQL mixed delta issues now retain the highest
  status priority. The keepalive closes on exceptional exits as well.
- The bounded synthetic diagnostic passed for Python, Java and SQL in
  `output/legacy-migration/benchmark/candidates/stage9c-vector-diagnostic-20261021-ready/diagnostic.json`
  (SHA-256 `45E35552AE5501E6112B121F04F38E246ACB455654B09777426E3B1250992C70`).
  Each backend verified 61 archives, committed all four series/132 dense rows,
  passed four independent audits, and passed rollback, after-commit restart,
  idempotency, stale-checkpoint and changed-source checks. Missing/changed seed
  identities and a modified scalar audit cache were rejected. The synthetic
  calculation-only report `output/stage9c-kernel-diagnostic.json` measured
  approximately 4.8x lower NumPy median calculation-only time for 1,003 tickers and
  windows 20/60. Neither diagnostic is Stage 9 ranking evidence.
- The original manifest is unchanged (SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`).
  Production rows, receipts and retained archives are unchanged. Stage 9c remains
  incomplete until fresh paired measurements pass; no backend is selected.
  Do not execute Stage 10. The full benchmark has deliberately not been rerun.
- Validation: 121 relevant Python numerical, storage, update, candidate and audit
  tests passed (four existing SQLite adapter deprecation warnings); Maven tests
  and packaging passed all 52 Java tests. A wider run including unrelated model
  training tests encountered installed PyTorch/Triton ROCm-discovery failures
  (36 failures/15 errors); those dependencies and training paths were not changed.
  Python compilation and `git diff --check` passed. No commit was created.

From the repository root, rebuild and run the prepared full comparison:

```powershell
mvn -o -f java/pom.xml test package
$pythonExe = 'C:\Users\formm\AppData\Local\Python\bin\python.exe'
$env:PYTHONPATH = 'src'
& $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
  --run-stage9-zscores `
  --manifest output/legacy-migration/benchmark/candidates/manifest.json `
  --dbinfo-file dbinfo.id `
  --output output/legacy-migration/benchmark/candidates/stage9c-optimized-20261021 `
  --stage9a-report output/legacy-migration/benchmark/candidates/stage9a-20261021/stage9a-raw.json `
  --stage9b-report output/legacy-migration/benchmark/candidates/stage9b-20261021-rerun/stage9b-derived.json `
  --stage9-timeout-seconds 3600
```

The runner caps native threads at one and retains the original candidate orders,
both target blocks, one first plus three warm repetitions, and atomic
series/session transactions. Use another fresh output suffix if the named
directory already exists. To repeat only the bounded diagnostic:

```powershell
& $pythonExe -m stockguidance.performance.stage9_zscore_diagnostic `
  --output output/legacy-migration/benchmark/candidates/stage9c-diagnostic-<fresh-run>
```

### Stage 9c split into resumable units and reduced profile — 2026-10-22

- Inspected the still-running `stage9c-optimized-20261021-final2` attempt and
  stopped its parent/SQL worker before changing code. Its preserved top-level
  report SHA-256 is
  `AA7D093983F6C594F528E5304CD1A1FA5B82EE24ACBCB639FF7A9B5D26A09CE2`.
  Python passed both first blocks; Java passed two chronological trials but
  failed both fixed trials on 2018-08-29/price/20/ticker 19479. SQL timed out
  after one hour in each first block, with 63/100 and 65/80 committed series.
  These partial full-profile results are preserved, not merged into the revised
  comparison. Stage 9a and 9b remain complete and reusable.
- Fixed Java's constant-window scalar fallback: repeated 26.69 values could
  acquire a tiny variance from the rounded compensated mean, yielding READY
  instead of ZERO_VARIANCE. Exact constant windows now return NULL/status 5.
  Added a Java regression test for both windows and included the decimal
  constant case in the three-backend diagnostic fixture.
- Added `--stage9-profile compact`: five fixed target dates
  (2006-12-08, 2010-10-04, 2018-08-29, 2021-10-04, 2026-04-29) and the last three
  chronological dates (2026-04-27 through 2026-04-29). This reduces 45 target
  slots to eight, while retaining all ticker IDs, all 20/60 dependency history,
  both families, dense NULL/status outcomes, startup-inclusive persistence,
  candidate rotations and one first plus three warm repetitions. The report
  freezes this selection and explicitly limits extrapolation to these dates.
- Added independent measurement and audit phases, per-invocation trial/audit
  limits, atomic report/cache saves, per-series audit checkpoints and strict
  resume identity checks. Timings are saved before auditing. Successful audited
  cells are skipped on resume. Interrupted measurements restart their entire
  cell using fresh checkpoint paths. Explicit failed-cell retries preserve
  earlier attempts. Failures stop further trials. Pending audits require
  unchanged disposable output checksums. A schema advisory lock rejects another
  orchestrator; worker interruption cleans up its child and monitoring thread.
  Deliberate pauses return success while the comparison stays incomplete.
- Full execution breakdown, interpretation limits and copyable step/resume
  commands are in `docs/stage9c_execution.md`. Run one measurement cell, audit
  up to four series per invocation, then advance to the next cell. The same
  controls also support the original full workload in a fresh output directory.
- Validation: 162 Python tests passed before the final interruption/lock
  additions; the final focused candidate suite passed 18 tests, including
  interruption, resume, identity/output changes, failed retries, concurrency
  rejection and fail-fast audit behavior. Maven tests/package passed 53 tests.
  Final three-backend diagnostic passed in 16.45 seconds:
  `output/legacy-migration/benchmark/candidates/stage9c-independent-diagnostic-20261022-ready/diagnostic.json`
  (SHA-256 `581B52B287F3FA46A6320A367C99E0A7AFD3B1DF73D854D93FF11A7B61F5F51D`).
  The actual disposable synthetic workflow passed measure/pause, partial audit,
  remaining audit, the remaining 23 cells and a no-op completed resume:
  `output/legacy-migration/benchmark/candidates/stage9c-workflow-synthetic-20261022-ready/stage9c-zscores.json`
  (SHA-256 `3DC305A81809AF75B6F51D04ABA51CD934F5D499E60A39C4CD4525A709FB49E8`).
  Synthetic results are excluded from performance ranking.
- The final real-input bounded validation uses
  `output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready/`
  and disposable schema `stockguidance_disposable_s9c_shared_5769a5a6`.
  Python fixed-block repetition 0 finished all 20 series/737,320 rows in
  318.011 seconds, saved its timing and exited successfully at the measurement
  pause. A separate audit-only invocation reused that timing, passed all four
  series for 2006-12-08 (36,866 rows each), and exited successfully at its limit.
  The trial remains `awaiting_audit` with 4/20 audits passed; the aggregate is
  incomplete. Remaining work is 16 audits for this cell plus the other 23
  backend/block/repetition cells. No Stage 9c processes remain running.
  Current report SHA-256:
  `5475C5D4944E7D3FF613716F93D1435B100654690B0402FEB139745D104BB4B4`;
  current `study.json` SHA-256:
  `C65E071F1A57DB38B5A83290EE3001D51B60CBFFA73906240B4EAC727B2D1795`.
  The earlier `stage9c-compact-20261022/` partial smoke attempt was preserved
  when adding interruption/locking safeguards; it is ineligible for comparison.
  The original manifest SHA-256 remains
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`.
  Production DDL/DML, production rows and archives remain unchanged. No backend
  has been selected and no commit was created. Stage 9c remains incomplete;
  continue its resumable compact comparison before Stage 10.

Next prompt: **Resume Stage 9c compact only using the existing ready entry;
audit at most four series and process at most one trial per invocation. Preserve
successful cells and all earlier evidence. Do not run Stage 10 or commit.**

```powershell
$env:PYTHONPATH = 'src'
& 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe' `
  -m stockguidance.cli.legacy_migration_benchmark `
  --run-stage9-zscores `
  --manifest output/legacy-migration/benchmark/candidates/manifest.json `
  --dbinfo-file dbinfo.id `
  --output output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready `
  --stage9-profile compact --stage9-timeout-seconds 3600 `
  --stage9-resume --stage9-max-trials 1 --stage9-audit-series-limit 4
```

Repeat this bounded command to finish the current audit and advance through the
remaining cells. Preserve code/classes/inputs and the current disposable outputs
between audit invocations; rebuilding or changing code can require a fresh paired
comparison. See `docs/stage9c_execution.md` for phase-specific commands.

### Stage 9c compact comparison complete — 2026-10-22

- Stage 9c is complete on the declared compact profile. All 24 cells (three
  backends, two blocks, four repetitions) passed correctness audits. Every
  fixed-block repetition audited 20 series and every chronological-block
  repetition audited 12 series. No Stage 9c process remains running.
- The profile measured five fixed target slots and three chronological slots,
  retaining all 36,866 ticker rows, both families, windows 20/60, dense statuses
  and all dependency history. It is a valid sampled comparison and makes no
  full-history runtime claim.
- Z-score persistence warm medians and aggregate throughput were Java
  `287.87s` / `4,098.10 rows/s`, Python `402.76s` / `2,929.06 rows/s`, and SQL
  `1,006.04s` / `1,172.63 rows/s`. Java was 28.52% faster than Python; SQL was
  149.73% slower.
- Across all Stage 9 workloads, Java's warm medians were raw `10.36s`, derived
  `41.14s`, z-score `287.87s`; Python's were `11.31s`, `46.68s`, `402.76s`;
  SQL's were `21.65s`, `57.52s`, `1,006.04s`. Java was faster than Python in
  all three measured workloads; SQL was slower in all three.
- The final files are
  `output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready/stage9c-zscores.json`
  (SHA-256 `264F1300DF7D3FFF4DBFFD8870A67AF505065B2AA8B8AF7930329780D43FC0F8`)
  and `study.json` (SHA-256
  `D81FCEC46BA161094204173F682BC4AE769F31D786FC92AA20CD7B654C5E1C15`).
  The manifest remains unchanged at SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`.
- No production backend was selected and Stage 10 was not run. The future Stage
  10 prompt should review the compact-sample limitation and warm variability,
  then apply the documented selection rule. No commit was created.

### Stage 10 backend selection and rollout freeze — 2026-10-22

- Prerequisites passed. The aggregate Stage 9 study is complete with all nine
  backend/workload cells measured, parity audits passed, raw/derived MySQL
  recovery evidence passed, and the independent three-backend z-score recovery
  diagnostic passed. The diagnostic remains recovery evidence only and is not
  ranking evidence. No candidate report recorded production DDL or DML.
- The selection rule used the best valid startup-inclusive warm median for each
  backend and required an alternative to be at least 10% faster than Python.
  Raw adoption/repair selects Python `python-p3` at 11.310634 seconds: Java's
  best overall improvement is 8.37%, below threshold, although same-preparer
  Java p1 is 13.54% faster and p3 is 6.02% faster. Derived computation plus
  persistence selects Java `java-p1` at 41.139056 seconds, 11.87% faster than
  Python `python-p1`; z-score persistence selects Java `java-p1` at
  287.868001 seconds, 28.52% faster than Python. SQL qualifies for none.
- The compact z-score profile is a sampled comparison over five fixed and three
  chronological target dates, not a full-history runtime claim. Mixed
  Python/Java activation is therefore frozen behind Stage 11 measurement of
  transfer, staging and orchestration cost; Stage 10 does not infer that cost.
  Production readiness remains false until Stage 11 integration/recovery and
  Stage 12 restore/schema/pilot gates pass.
- Rechecked scope is 36,866 ticker IDs and 5,130 retained sessions, with 4,898
  valid archive sessions, 42,482,212 valid archive records and 44,199,460
  physical database rows. Four dense z-score tables can contain 756,490,320
  rows before metadata, indexes, table-copy space, staging, backups and recovery
  headroom. Stage 12 must measure those bytes before full z-score population;
  Stage 10 records the local and benchmark-server free-space observations but
  does not claim a disk-size estimate from row counts.
- Limits are one writer, at most three preparers, at most three queued
  sessions, one session per raw/derived transaction, one series/session per
  z-score transaction, a chronological 20-session z-score chunk, 512 MiB per
  preparer, Java `-Xmx512m` and one native-library thread. Use one preparer and
  one queued session as the reduced-memory mode.
- `output/legacy-migration/decision.json` is the version-2 frozen decision.
  Its SHA-256 is `ed029ec095ba4c00accd4d9e41e94e631acc4379cab9701ab137f319a76b59ea`;
  it retains provisional identity
  `517bf3d872e7149254493ae3177467653e4f7accf4c3daf798939119f035e0bd`.
  Inputs are the compact study SHA-256
  `d81fcec46ba161094204173f682bc4ae769f31d786fc92aa20cd7b654c5e1c15` and
  unchanged manifest SHA-256
  `11bbcf4b57469c1256ab9caa474824a538ccbfb8bd3155de6515965e2d4fcf26`.
- The reproducible selector is
  `PYTHONPATH=src <configured-python> -m stockguidance.cli.legacy_migration_benchmark --run-stage10`
  with `--manifest output/legacy-migration/benchmark/candidates/manifest.json`,
  `--stage10-study-report output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready/study.json`,
  `--stage10-audit-report output/legacy-migration/audit.json`, and the
  independent diagnostic path recorded in the decision. It is read-only apart
  from the decision artifact.
- Stage 11 must wire Python raw adoption/repair, Java
  `JavaDerivedBenchmarkMain`/`JavaWriterCandidate`, and Java
  `JavaZScoreBenchmarkMain`/`JavaZScoreCandidate` in disposable storage, then
  measure handoffs and complete recovery. Stage 12 must restore-test the backup,
  preview/apply the precision and ticker-collation migration, and pass the
  bounded pilot. Stage 13 uses chronological raw/derived chunks and the limits
  above; Stage 14 publishes both z-score families at windows 20 and 60 with
  explicit NULL/status outcomes; Stage 15 performs the independent full-scope
  audit and operating handoff. No production work is authorized by this stage.
- Focused validation passed: `tests/stage10/test_legacy_migration_selection.py`
  passed 3 tests and the Stage 10 CLI help exposed the new bounded selector.
  No commit was created. Next prompt: execute Stage 11a only.
