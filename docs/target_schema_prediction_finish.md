# Finish the MySQL target migration and run predictions

Updated 2026-11-23. Use this as a sequence of prompts for a coding/operator
session. Stop at each gate and save its evidence before moving on. The source of
truth for migration progress is the database checkpoint, not this document or
the local JSON journal.

## Observed state

- Stages 1–12 of database simplification are implemented. The Stage 12 SQL
  engine copies and validates bounded MySQL ranges; the old whole-table copy
  was replaced. Its disposable MySQL rehearsal passed. See
  [implementation results](database_simplification_results.md) and the
  [SQL migration runbook](database_simplification_stage12_flow.md).
- `output/database-simplification/stage12/export/migration-journal.json` says
  engine 2, phase `validated`, updated 2026-10-27. All 38 recorded validation
  steps are complete. It reports 236 deferred session-completion entries,
  spanning 2005-12-16 through 2025-11-10. These are not certified coverage.
- The journal's target registry digest is `f09f7886715ed8fea752d783f39c44f2225847f3b520cd2e16640ca56178d347`.
  Current code computes `2ebd98883a98e77686a5803e8c0eb49c4df395bd3fb7d8aa844619b4a3a07e3d`.
  The migration ID still matches the journal. The later additive volume z-score
  template is a likely cause of the registry change; verify the exact role and
  schema difference rather than assuming it is the only one.
- A live, read-only `migrate_database status` against `replacement-dbinfo.id`
  and `target-dbinfo.id` failed with `Database checkpoint identity does not
  match`. The command checks engine, migration ID, server UUID and registry
  digest. No live phase could therefore be confirmed through this CLI.
- `dbinfo.id` currently names `stock_data`, the target database, while the
  journal remains `validated`. No local `cutover-marker.json` was found. This
  may be a manual configuration switch ahead of the recorded cutover; verify
  its history and current readers before further writes.
- The training and recommendation entrypoints exist (`train_model`,
  `recommend`). They use target-schema readiness, but no successful prediction
  against this live target is established by the migration journal or fixture
  tests. `training-job.json` names `dbinfo.id` and has a fixed 2026-07-03 end
  date; review it before using it as a live job.

## Prompt 1: establish the authoritative live state (read only)

```text
Inspect the current MySQL source and target configurations without printing
credentials. Read the target migration control row, schema version, selected
registry/table names, server UUID, pending update rows, and source singleton
through read-only connections. Compare the stored checkpoint identity field by
field with the identity constructed by the current code. Verify whether the
target is still phase validated, whether any cutover marker exists in MySQL,
whether the active dbinfo switch was intentional, and whether source/target
values changed since the October validation. Explain the exact registry delta,
including the volume template. Do not edit the journal, checkpoint, registry,
schema or active dbinfo. Save a sanitized state report with a clear pass/fail
result for each finding.
```

Gate: explain the identity mismatch and active-config state using live evidence.
The current `status` CLI cannot certify phase until the mismatch is resolved.
Avoid a full `plan` as a quick status probe: it may scan and hash substantial
backup/source evidence.

## Prompt 2: reconcile registry and schema evolution

```text
Design and implement the smallest explicit, versioned path from the validated
target's schema/registry to the current runtime contract. Determine whether
the target lacks only the newer volume preparation objects or has other drift.
Preserve the completed copy, raw/delta/z-score values, checkpoints, source
identity and legacy rollback database. Do not spoof the old digest, overwrite
the checkpoint identity, hand-edit the journal, or silently initialize missing
tables through an ordinary read. If the current migration engine cannot safely
resume/cut over an older validated registry, add an audited compatibility or
upgrade path with disposable MySQL rehearsal. Show the exact DDL, transaction
boundary, repeat-run behavior, rollback method, and independent validation.
Run the focused Python/MySQL checks and report any remaining incompatibility.
```

Gate: the current code can read the target and report its phase with an exact
identity, and schema/readiness checks pass. If this cannot be safely achieved,
prepare a new distinct target and repeat the verified migration instead of
changing checkpoint evidence by hand.

## Prompt 3: finish migration acceptance and cutover

```text
The MySQL databases are currently unused. Downtime and alterations to the
target are authorized for this stage. Carry out the live v2-to-v3 upgrade and
finish Stage 12 migration cutover, using
docs/database_schema_upgrade_v2_v3.md and
docs/database_simplification_stage12_flow.md. Do not stop merely to ask for
permission to take the databases down or alter the target.

First verify the selected legacy/target database names, server UUID, current
v2 validated checkpoint, active dbinfo target, absence of pending operations,
and available disk space. Stop all writers and keep both databases quiescent
through cutover. Verify the existing backup manifest against its actual backup
and independently restored copy; if that evidence is inadequate, create and
verify new backup/restore evidence before DDL. Keep all credentials out of
reports. Record the pre-upgrade metadata, table list, completion counts and
checkpoint identity in a sanitized report.

Run the explicit `migrate_database upgrade` command with confirmation derived
from the verified target database name. Resume the same command if interrupted;
never edit the checkpoint, registry digest or journal to force it through.
Verify v3 schema metadata, both empty volume tables, the audit lineage row,
preserved v2 control identity and unchanged existing numerical data. Run
`migrate_database status`, then the full bounded `validate` operation against
the quiescent source and target. Resolve any comparison failure before moving
on; do not use the October validation checkpoints as current evidence.

On complete validation, inspect its report, 236 previously deferred completion
entries, pending-operation count, and representative read-only training and
prediction calculations on known-complete ranges. The deferred entries need
explicit treatment for the later requested prediction range, but unrelated
incomplete dates do not require invented completion. Run the explicit
`cutover` phase only when the migration checks pass, then verify the durable
database cutover confirmation, marker file, schema/registry identity, and
active dbinfo mapping. Keep the legacy database and backup for rollback;
retirement is outside this stage.

Save exact commands, timings, database sizes, validation results, any repairs,
and the post-cutover state in a sanitized handoff. If an evidence gate fails,
stop at that gate with the precise blocker and recovery command; do not claim
prediction readiness yet.
```

Gate: durable `cutover` phase/marker, active configuration pointing at that
exact target, and no unresolved migration discrepancy. The exact commands and
confirmations are in the SQL migration runbook; derive the confirmation from
the currently verified database name.

## Prompt 4: make the requested prediction range ready

```text
Choose a concrete prediction entry session, source policy, universe, feature
specification and training range. Inspect the 236 deferred completion entries
against that dependency range; do not treat unrelated old dates as blockers.
Run the current independent target validator and z-score audit for the needed
sessions, including feature lookback, warm-up and mature training labels.
Resolve in-scope incomplete raw/delta/z-score work through maintained target
maintenance/readiness, with source evidence and pending-operation recovery.
Keep expected missing, non-ready and future-dependent outcomes explicit.
Repeat read-only validation and save before/after reports. Never mark unknown
completion true merely to pass readiness.
```

Gate: requested training inputs and prediction lookback have verified coverage;
no overlapping pending operation remains. Use [database validation](database_validation.md),
[daily maintenance](daily_maintenance.md) and the [operations guide](operations_guide.md).

## Prompt 5: train and prove one prediction

```text
Create a live job configuration from the current source/target settings and
chosen dates; do not reuse fixture IDs, dates or source identity. Review
training-job.json's older fixed range and model version before adapting it.
Preview the configured job, run a small CPU training fit over verified mature
outcomes, and save a new .sgmodel artifact with its content identity and
metrics. Run a validate-only recommendation/prediction using that artifact and
the chosen entry session, with no provider refresh. Verify its input cutoff,
ordered ticker IDs, eligibility exclusions, scores and artifact identity.
Then run the intended current/retrain policy using the maintained CLI. Record
the exact commands, output paths and any freshness or cutoff failure. Do not
claim readiness from fixture-only Stage 12 results.
```

Gate: a new artifact loads, a target-backed prediction succeeds for the
selected entry session, and the report identifies the source revision and
decision cutoff. See [training](model_training.md),
[recommendations](recommendations.md), and [frontends](frontends.md).
