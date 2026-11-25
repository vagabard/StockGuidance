# Live Stage 12 upgrade and cutover record

Run started 2026-11-23. This record is updated as the authorized live work
progresses. The legacy database is retained.

## Preflight and upgrade

- Legacy `stockforecasting` and target `stock_data` are on the same MySQL
  server. The active `dbinfo.id` already points to `stock_data`.
- Target started at schema version 1, registry version 2, migration phase
  `validated`, with 38 complete migration steps, no cutover confirmation and
  no pending updates. The existing maintenance writer lock was free. Two
  legacy connections were sleeping; no active legacy or target writer query
  appeared in the inspection.
- MySQL data directory is on `C:`. At preflight, `C:` had about 1.95 TB free;
  the source and target had approximately 42.9 and 64.2 GiB of allocated
  tables/indexes according to `information_schema`. The backup volume `D:`
  had about 3.63 TB free.
- `verify_backup_manifest` hashed both 33,401,586,424-byte files and accepted
  their recorded SHA-256 digests. The separate
  `stockforecasting_stage12_restore` schema exists. Its table count (26),
  session count/range (5,247; 2005-12-16 through 2026-10-23), ticker count
  (41,275), source singleton fields and sampled raw keys/values matched the
  legacy database. This is bounded restore evidence; it does not prove every
  restored numerical row equals the source today.
- The explicit `migrate_database upgrade` command completed with phase
  `upgraded`. Read-only inspection confirmed registry version 3 and digest
  `2ebd98883a98e77686a5803e8c0eb49c4df395bd3fb7d8aa844619b4a3a07e3d`,
  both InnoDB volume tables, a v2-to-v3 audit lineage row, and the original v2
  migration control identity. Session raw/delta completion counts remained
  5,015/5,011 across 5,247 sessions; pending updates remained zero.
- `migrate_database status` subsequently reported phase `validated`, engine 2,
  38 complete steps and no incomplete steps. The sanitized output is under
  `output/database-simplification/stage12-live-20261123/status-after-upgrade.json`.

## Fresh validation

The bounded `migrate_database validate` run completed at
2026-11-24 04:26:35 UTC. It began at approximately 02:34 UTC with 10,000-row
batches, then resumed from its durable checkpoint with 100,000-row and
500,000-row batches. The final result reports phase `validated`, all 38 steps
complete, and no source/target comparison failure. It covered 5,247 sessions,
with 5,015 raw complete, 5,011 delta complete, and 236 deferred sessions.
The result and progress files are
`output/database-simplification/stage12-live-20261123/validate-result.json`
and `validate-progress.log` in that directory. The local journal was updated
at the same time. The validation process finished after its agent session
disconnected, so the earlier in-session progress observations are superseded
by this result and the subsequent live status check.

Deferred completion review found 10 sessions in 2005, 222 in 2006, and four
in November 2025. The latest stored October 2026 sessions inspected were raw
and delta complete. A bounded read-only calculation using AAPL/MSFT closes
from 2026-10-19 through 2026-10-23 produced finite close-normalized features
and mature next-session targets; all four existing price/price-delta z-score
statuses on 2026-10-23 were `0` for those two tickers. Ten sampled raw
close/volume keys and ten `price_20` score/status keys matched legacy source
values. This is a data-path spot check, not an executed model prediction.

## Verified state and next gate

A read-only live check at 2026-11-24 06:09 UTC confirmed phase `validated`,
38 complete steps, no incomplete steps, registry version 3 with digest
`2ebd98883a98e77686a5803e8c0eb49c4df395bd3fb7d8aa844619b4a3a07e3d`,
the v2-to-v3 audit row, and zero pending updates. There is no completed
`cutover:confirmed` step and no live `cutover-marker.json` in the export
directory. Active `dbinfo.id` already names `stock_data`; the legacy
`stockforecasting` database remains available.

Before cutover, review the completed validation result and the 236 deferred
sessions against the intended prediction range, confirm the prior representative
read-only calculations are sufficient for acceptance, and keep source and target
quiescent. Then run the runbook's explicit `cutover` command with confirmation
`CUTOVER stock_data`. The command starts its own fresh, full validation under
the writer lock; the earlier `validate` result does not skip that work. After
it succeeds, verify live phase `cutover`, the completed confirmation step, the
marker, the journal, and active configuration. Keep the legacy database and
backup for rollback. Prediction readiness and a model run are later gates.

## Completed cutover and post-cutover validation

The resumed cutover completed at 2026-11-24 08:15:35 UTC. Its result reports
phase `cutover`. A live check confirmed 39 complete migration steps including
`cutover:confirmed`. The export marker records `CUTOVER stock_data` with the
matching migration identity and registry digest. Active `dbinfo.id` and
`target-dbinfo.id` both connect to `stock_data`; the legacy database remains.

The independent read-only target validator returned `complete: false` (exit 2).
Its report is `output/database-simplification/stage12-live-20261123/target-validation-after-cutover.json`.
It records 84,445 blocking `delta/missing_input` findings, two blocking
`delta_completion_missing` findings, and one missing requested session under
its broad default scope. It also records 2,856 nonblocking expected non-ready
z-score statuses. Findings with dates span 2024-11-25 through 2026-10-23.
Select a concrete training and prediction range, then check that range and
repair in-scope inputs through maintained target maintenance before training.

## Latest stored AAPL/MSFT readiness investigation

The selected historical scope is AAPL (ID 8) and MSFT (ID 1867), with the
latest stored market session on 2026-10-23. Both have 59 raw close rows from
2026-08-03 through 2026-10-23, 116 ready delta rows and two final-session
`not_yet_available` delta rows. Each of the four price/price-delta z-score
tables has 118 status-0 rows in this range. No pending operation existed before
the attempted refresh.

Many broad-validator `missing_input` rows reflect a ticker absent from its
previous session. The 2025-11-10 spike follows empty weekend dates in the
target date dictionary (2025-11-08 and 2025-11-09). The 2025-11-07 and
2025-11-10 delta completion flags remain false. Those older dates are outside
the selected recent AAPL/MSFT scope and were not edited here.

A bounded strict training attempt over 2026-09-01 through 2026-10-21 found no
eligible samples. The target's session availability timestamp for that range
is 2026-10-26, after the historical decision cutoffs. No artifact was exported.
Historical receipts were not backdated or marked complete.

The maintained daily refresh for 2026-10-26 through 2026-11-23 first failed
because it treated the XNYS session calendar as each ticker's listing exchange;
existing AAAP is XNAS. The grouped bars contain no per-ticker exchange. The
runtime and raw rebuild now preserve an existing ticker exchange. A retry
downloaded prepared payloads through 2026-11-23 but failed in raw rebuilding.
A one-session retry for 2026-10-26 committed raw data, then its derived rebuild
grew beyond 4 GiB of process memory and was interrupted to protect the machine.
Its pending operation (ID 3) is recoverable; raw completion is true and delta
completion is false for 2026-10-26. Do not start another writer until that
pending operation is resumed or resolved through the maintained coordinator.

The one-session coordinator was subsequently resumed from pending operation 3.
At the latest check it was still CPU-active, with raw completion true and delta
completion false for 2026-10-26. The machine had ample available memory; the
earlier 4 GiB stop threshold was unnecessarily conservative. Do not start a
second writer while this resume process remains active.

## November 25: current-history validation gate and maintenance resume

The preceding operation 3 notes are historical. At this resume, operation 10
for October 30 is the pending operation. October 29 is complete; October 30
raw and delta are complete, with two price z-score series committed and the
two price-delta series awaiting recovery.

The user approved current-history semantics: receipt/import times no longer
determine historical eligibility. Exchange-session cutoffs still prevent
future prices from entering features. Python numerical writers now emit
empty provenance arrays, readers select no historical provenance, and
session metadata uses compact session versions rather than a ticker-by-date
evidence graph. Existing quality flags and bad/missing/interpolated states
remain in force.

The superseded provenance compaction finished successfully. The activation
tool removed delta provenance for October 23–30 in batches of 500 and removed
the temporary shared-provenance table. It installed
`stockguidance_validation_gate`, keyed by date and phase, with a data version,
implementation/design hash, and recorded pass time. Data/repair revisions and
completion metadata identify the current raw version. Application close
reads require a matching pass and still reject overlapping pending writes.
This raw gate does not claim that missing or bad individual tickers are usable.

Verification: 35 focused raw/numerical/recovery/frontend/readiness tests passed,
including late imports, future-session rejection, changed data/design versions,
and unavailable future targets. Compilation and diff checks passed.

The bounded runner is active from October 30 through November 23:
`bounded-maintenance-current-history-progress.jsonl` and
`bounded-maintenance-current-history-error.log` under the Stage 12 output
directory. Once all date batches complete, it invokes the independent
AAPL/MSFT check for October 23–November 23. That check compares stored 20/60
price and price-delta z-scores with an independent population-standard-deviation
calculation, checks delta ratios, exercises gated historical app close reads,
and writes `bounded-maintenance/aapl-msft-readiness.json`. A clean result records
the `aapl-msft` validation phase; failures stop the runner with the report saved.
Model training and recommendation inference are separate from this data check.

## November 25: successful live training and recommendations

The bounded maintenance completed through November 23, with no pending
operations. The independent AAPL/MSFT report passed 176 z-score comparisons
for October 23–November 23 with zero findings. Scoped recommendation readiness
now validates the configured ticker universe; unrelated classified bad rows
do not block that universe. Ten focused frontend/readiness tests passed.

On the user's request, the November 24 maintenance command completed with
exit 0, a successful validator result, and four committed z-score series.
The recommendation command with `recommendation-aapl-msft.json`, `--policy
retrain`, and `--refresh-mode refresh` then completed with exit 0.

Training completed two epochs on 59 samples spanning September 1–November 23,
with last outcome November 24 and no excluded samples. The exported model
`output/aapl-msft-20261125.sgmodel` was independently reloaded and its artifact
identity checked:
`54eae40f4f63f38b018c96d1b6dbe17bf0257295b9cde678bdcd3aa3d731ca97`.

Both AAPL and MSFT passed the November 24 transaction-count eligibility check.
The November 25 entry / November 27 exit report ranks AAPL first with a
fractional-return score of `0.003237753175199032`, followed by MSFT with
`-0.008870208635926247`. These are outputs of the two-epoch baseline, without
held-out performance evaluation. No orders were submitted.

The run is
`output/live-recommendations/recommendation-20261125T155251424415Z-f8355bb11c02`.
The independent artifact, database-state, and score verification is saved at
`output/database-simplification/stage12-live-20261123/training-recommendation-20261125-verification.json`.
Final check: November 24 raw/delta complete, four z-score series, and zero
pending database operations.
