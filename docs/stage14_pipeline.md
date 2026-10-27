# Resuming Stage 14 with a shared Java copy

> Historical migration-runbook reference. The Stage 14 Python runners and
> helper scripts were retired after the simplification handoff. Keep this file
> for recorded evidence only; do not use its commands for a new migration.

Run `tools/resume_stage14.py` directly in the project's configured Python
interpreter. It takes no parameters and resolves the checkout from its own
location, including when launched from the IDE. The Java classes must have been
built with `mvn -o test` in `java`; the current change was built and tested.
It uses the existing `dbinfo.id` without exposing its contents.

The operator authorized correcting the dependency contract to 61 raw dates:
60 daily returns require the preceding raw close as well. Migration archives
the old manifest/report under `stage14/history/<old-manifest-id>/`, retains its
chunk reports/checkpoints, and creates a distinct new checkpoint namespace.
Previously completed dates are revalidated. Matching materialization rows are
preserved; only differing output rows are replaced, while dependency/completion
metadata is refreshed. Unrelated source or registry changes prevent migration.

The settings at the top of the script include `AUTO_TUNE = True`,
`UPDATE_WORKERS = 1`, and `JAVA_HEAP = "64g"`. These configure threads in one
long-lived JVM, with a maximum 64 GiB heap. The heap is not preallocated.
The tuner measures 1 through 10 calculation workers on the same ten real target
dates, after loading their shared inputs. It discards warmup, measures two trials
per count with the second sweep reversed, and requires identical canonical
results for every trial. It chooses the smallest count within 5% of the best
median, recording all samples in `stage14/thread-tuning.json`. These are cached
calculation/hashing measurements, not full end-to-end production benchmarks.
The selection is reused only when the manifest and compiled Java identities
match. One source loader and one publication worker are counted separately;
Python monitoring/coordinator and JVM housekeeping threads do not count against
the ten calculation workers.
The publication pool is separately configurable; keep one writer for the
current rollout. Additional writers need measured MySQL concurrency validation.

## Data flow

`JavaDbCopy` holds immutable `RawRow` and `DeltaRow` records grouped by session.
The records project the required source fields: ticker/date IDs, raw close,
and stored `close_ratio_to_prior`. Other raw/delta columns are not loaded.
Exact-case ticker mappings and receipt evidence remain part of the input
contract. The delta family continues to validate the stored ratio before
subtracting one; it does not substitute a newly computed raw return.

Requests for overlapping dependency dates share cached futures. An already
loaded session returns immediately; a session being loaded waits on the same
future. New sessions are read in batches of at most 20 dates, covering all
tickers. Each date's raw rows, delta rows, mapping, and receipt evidence are
queried once per JVM lifetime, including absent rows. Failed futures remain
failed and stop the operation; they do not silently retry changed source files.
Validated archive files are also checked once per loaded session.

The loader owns one read-only repeatable-read connection. Its first consistent
read establishes the source snapshot used for the JVM lifetime. Source queries
use ordinary `SELECT`, without `FOR UPDATE` or `FOR SHARE`. Immutable records
are published to calculation workers only after their complete batch loads.
The copy grows as processing advances and is retained across Python's
20-session chunk boundaries. Restarting the JVM rebuilds the necessary copy.

Each calculation task owns one target date and computes both families at windows
20 and 60 through the existing vector kernel. The kernel processes groups of
tickers in SIMD lanes across the exchange-session window. The task submits
immutable prepared results to the publication pool. At most three additional
session tasks are queued; publication permits bound queued prepared results to
12 plus the number of writers.

Publication validates the existing four series definitions. Only materialization
rows and session completion metadata are replaced, atomically per series/date.
No source-table updates, schema changes, server settings, or SQL routines are
introduced. Calculation completes before the publication transaction starts.
Source rows are not selected on the publication connection. Expected deadlocks
and lock timeouts retry at most twice after transaction rollback; other failures
stop the run. Checkpoints are atomically replaced after commit.

## Resume and completion

The coordinator holds an OS file lock at
`output/legacy-migration/stage14/run.lock`, protecting the shared report from
another updated Stage 14 coordinator. Use one coordinator per checkout. The
Java child monitors its Python parent's lifetime; normal interruption also
closes or terminates the child before the coordinator releases its lock.

The existing manifest and corrected checkpoint directory are reused:

- `output/legacy-migration/zscores.json`
- `output/legacy-migration/stage14/zscore-manifest.json`
- `output/legacy-migration/stage14/java-checkpoints-d93c397c3c1a/`
- `output/legacy-migration/stage14/java-reports-d93c397c3c1a/chunk-NNNN.json`

Completed chunks are skipped. `batches-<manifest-prefix>.json` explicitly lists
every planned chunk as `pending`, `in_progress`, or `complete`. Only `complete`
means accepted publication; pending work is never inferred complete from the
absence of an in-progress marker. Before any publication request, the runner
durably records the attempt and saves compressed, hashed before-images of the
four output tables and completion metadata for that batch's exact dates.

On restart, a batch whose durable report was accepted is finalized without
rollback. An unaccepted publishing batch is restored from its before-image in
one transaction across all four tables and metadata, then returned to pending.
An interruption during rollback leaves it in progress, and rollback is safe to
repeat. An attempt interrupted before publication can return to pending without
database restoration. Missing/changed backups or source identities stop recovery.
Unrelated dates and series are excluded from rollback. Completed batch backups
are retained for audit. No rollback support tables or schema changes are used.

Run `tools/rollback_stage14.py` with no parameters to recover unaccepted batches
without starting further population. It takes the same coordinator lock, so it
cannot run concurrently with the updater. The ordinary resume script performs
the same recovery before starting more work.

The cache does not survive process termination. The script prints a progress
line after each accepted chunk and an estimate every 15 seconds. A live JSON
snapshot is saved as `stage14/progress.json`; it includes batch phase, observed
series throughput, and estimated population time remaining. No ETA is invented
before progress establishes a rate. Estimates include batch backup/publication
overhead and exclude final independent audits. Tuning progress is identified
separately. Live publication progress is written to `pipeline-progress.json`
in the corrected checkpoint directory.
Per-chunk Java reports record worker counts, cached-session counts, and source
batch counts. Earlier chunk reports keep their original measurements.

Keep raw/derived inputs and their receipts unchanged during the run. The Python
coordinator checks the existing source revision before and after each chunk and
rejects completion when it changes. These revision checks use mappings, receipts,
completion markers, and table size observations; they are not an independent
hash of every source value. An out-of-band edit bypassing receipts cannot be
claimed detectable by these checks. The read-only snapshot itself keeps the
Java copy internally consistent. It remains open until population ends or the
JVM exits; concurrent source writers can therefore retain old undo history.

After population, the JVM and its snapshot close. The existing independent
Python boundary/random-session recomputations and full materialization/metadata
coverage checks must pass before `status=complete` and `production_ready=true`.
Population progress alone is not Stage 14 acceptance. Stage 15 is separate.

The live implementation was exercised against MySQL on 2026-10-22. The tuner
tested calculation worker counts 1 through 10 in two sweeps over a warm cache;
10 workers was selected with a two-trial median of 4.867 seconds for the
ten-session tuning sample, compared with 36.818 seconds for one worker. The
source loader and one publication worker are separate utility workers and are
not included in that calculation-worker count. The first repaired batches took
305.316 seconds including startup/tuning and 22.126 seconds for the second;
the fresh 20-session batch took 43.023 seconds end to end. The repaired rows
preserved all 6,410,560 existing materialization rows, while the fresh batch
created 3,205,280 new rows. These were bounded observations rather than a
full-history runtime guarantee; the full population subsequently completed in
approximately 4.47 hours.

The interrupted first attempt was recovered from its durable before-image in
one transaction and is recorded as rolled back/restored. The independent
bounded repair audit is saved at
`output/legacy-migration/stage14/bounded-repair-audit.json`; it passed 28
series across seven boundary/random sessions with an unchanged source
revision. Run `tools/audit_stage14_repair.py` with no parameters to resume or
repeat that read-only audit for the accepted scope.

The resumed run completed the full retained scope on 2026-10-23. The durable
report is `output/legacy-migration/zscores.json` with `status=complete` and
`production_ready=true`: 4,898 sessions, 40,066 tickers, all 19,592 series,
and four-family/window coverage checks passed. The final independent audit
passed 1,972 series across 493 boundary/random sessions. The source revision
was identical before and after population, and the batch journal records all
245 batches complete with no in-progress work. Stage 15 is next.

Validation for this implementation uses offline fixtures and JDBC doubles for
query deduplication, immutable/null input semantics, parallel scalar/vector
parity, source read-only behavior, output-only publication, commit recovery,
bounded retry, coordinator exclusion, and durable chunk resume. The shared-cache
pipeline has now also passed a bounded live MySQL run and independent output
audit. The earlier roughly 55-hour estimate describes the original
repeated-read path and does not predict the new pipeline's runtime.
