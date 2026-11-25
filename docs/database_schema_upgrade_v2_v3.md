# Validated target registry v2 to v3 upgrade

Implemented and rehearsed 2026-11-23. This is an explicit MySQL operator path
for the validated Stage 12 target described in
[Prompt 1's live report](target_schema_prompt1_report.md). The live `stock_data`
database was not changed during implementation or rehearsal.

## Design

The v3 registry adds `preparation_templates.volume =
zscores_volume_{window}`. For the configured 20/60 windows, the physical
addition is `zscores_volume_20` and `zscores_volume_60`. The ordinary schema
initializer rejects an existing v2 target, so the upgrade uses a separate
`migrate_database upgrade` phase.

The phase requires the exact validated v2 journal, verified backup and restored
copy, matching immutable v2 migration control identity, complete copy
checkpoints, no pending update, the expected v2 table set, and an exact target
confirmation. It holds the existing maintenance writer lock throughout. It
creates an audit table `stockguidance_schema_upgrades` and the two volume
tables using the current initializer DDL. On resume, it checks any already
created volume table's engine, columns, primary key, foreign keys and emptiness
before proceeding. Only after both tables pass does one InnoDB transaction
promote `stockguidance_schema_version` to registry 3 and insert the immutable
v2-to-v3 audit row. The migration control JSON retains the original v2 copy
identity. `migrate_database status` and the validation/cutover checkpoint path
accept that old identity only when the audit row, v3 schema metadata and volume
table shapes agree. The local journal remains the validated v2 record until a
subsequent migration validation writes new v3 journal evidence.

MySQL DDL commits independently. An interruption before metadata promotion can
leave the audit table and one or two empty volume tables. Re-running the same
upgrade checks and adopts them. If it fails after promotion, run read-only
`status` and repeat `upgrade`; a successful repeat reports `already_upgraded`.
Ordinary transaction rollback cannot undo the DDL. For rollback after a
committed promotion, restore the verified backup into a separate database and
repoint configuration through the operator process. Do not edit registry,
checkpoint or journal digests manually.

## Commands for a later operator session

From the checkout, using the existing validated journal and verified backup:

```powershell
$python = '.venv/Scripts/python.exe'
$common = @(
  '--legacy-dbinfo-file', '.\replacement-dbinfo.id',
  '--target-dbinfo-file', '.\target-dbinfo.id',
  '--windows', '20,60',
  '--backup-manifest', '.\output/database-simplification/stage12/backup-manifest.json',
  '--export-dir', '.\output/database-simplification/stage12/export'
)
& $python -m stockguidance.cli.migrate_database upgrade @common --upgrade-confirmation 'UPGRADE stock_data'
& $python -m stockguidance.cli.migrate_database status @common
```

These are reviewable commands, not evidence that the live upgrade ran. Before
executing them, confirm backup usability, target disk capacity, source
quiescence and the active `dbinfo.id` switch. After the upgrade, run the full
bounded migration `validate` and inspect its report before any `cutover`.
Volume materialization remains empty until explicitly prepared; the upgrade
does not compute or claim volume z-scores.

## Disposable MySQL rehearsal

Command:

```powershell
.venv/Scripts/python.exe tools/rehearse_schema_upgrade.py `
  --dbinfo-file replacement-dbinfo.id `
  --prefix stockguidance_disposable_upgrade_20261123_e `
  --output-dir output/database-simplification/schema-upgrade-rehearsal-e
```

Result: **passed**. The runner created two new prefixed schemas and dropped
both after the rehearsal. It removed the volume tables from a fresh fixture and
seeded v2 schema/control metadata plus sample raw and price z-score rows.
It rejected a wrong confirmation, interrupted after nontransactional DDL,
confirmed metadata remained v2, resumed successfully, accepted an identical
repeat run, passed the migration checkpoint/reference initialization used by
validation and cutover, and verified v3 schema, preserved raw/z-score rows and
unchanged original control identity. It used a synthetic backup manifest only;
it does not prove production restore usability or full source/target parity.
The sanitized report is at
`output/database-simplification/schema-upgrade-rehearsal-e/report.json`.

Verification: `mypy` passed for the upgrade module, rehearsal, SQL migration
engine and CLI. `pytest tests/database tests/cli -q --tb=short -p
no:cacheprovider` passed with 205 tests and 28 existing pandas warnings.
A final read-only live query still showed schema version 1/registry 2, phase
`validated`, the old digest, no volume or upgrade-audit tables, and zero
cutover confirmations.

## Remaining gate

This completes the Prompt 2 implementation and disposable rehearsal. The
live upgrade, renewed full validation, representative prediction check, and
cutover belong to [Prompts 3–5](target_schema_prediction_finish.md). The
existing active-dbinfo switch must be reconciled before those operations.
