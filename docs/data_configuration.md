# Data configuration

> The source-verification workflow described below is historical legacy
> behavior. The versioned target schema stores one immutable source singleton
> and rejects incompatible imports; it does not create verification tables.

The active data configuration is a separate JSON document referenced by
`data_configuration_file` in `dbinfo.id`. The example configuration is
`examples/data_configuration.json`.

`source_family` is canonicalized before it is stored. The `polygon` and
`massive` aliases currently resolve to the same `polygon_massive` family.
`endpoint` is retained for provenance and endpoint verification, but it does
not change the semantic configuration key. `market` is the broad market scope;
security records retain their exchange metadata and stable identifiers.

`calendar_id` is the fallback `exchange_calendars` calendar for broad jobs that
do not specify ticker IDs. Explicit ticker queries resolve the calendar from
the selected rows' `ticker_symbol.exchange` values in the database; mixed
database exchanges are rejected because one query cannot use multiple session
calendars. The setting must be a supported calendar name such as `XNYS` or
`XNAS`, and existing configurations that omit it continue to use `XNYS`.
It does not change the semantic data-configuration key.

The raw-price basis is described independently by
`raw_price_split_adjusted`, `raw_price_dividend_adjusted`, and the optional
`raw_price_vendor_defined_adjustment` value.

A source-family change registers a verification run. The default `block` mode
prevents normal database use until 100 stored-overlap session dates have been
compared. `gate_writes` permits reads while blocking writes, and `advisory`
permits both. Endpoint changes register a weaker 50-pull verification and are
advisory by default.

## Stage 12 source singleton migration

The updated [SQL migration runbook](database_simplification_stage12_flow.md)
documents the runnable batch copy, console progress, status, and resume commands.
Use explicit data-configuration paths in both dbinfo files. The source and
target must resolve to the same MySQL server and table registry. A checkpoint
identity includes engine version, server UUID, migration/configuration identity,
and registry digest; changing those cannot silently reuse another run's progress.

The Stage 12 migration carries the database-owned singleton into a separate,
versioned target database. The target data-configuration JSON must have the
same source family, market, universe, daily interval and raw adjustment flags
as the legacy singleton. A changed source identity is a separate authorized
data migration and is rejected by this tool; it must never mix old rows with a
new configuration.

Use a distinct target dbinfo file and keep credentials in those files or the
approved secret provider. The migration plan and journal contain only
non-secret source/configuration identity, database names, checksums, counts and
operator confirmations. They do not contain passwords or provider keys.

```powershell
$python = '.venv/Scripts/python.exe'
& $python -m stockguidance.cli.migrate_database plan `
  --legacy-dbinfo-file dbinfo.id --target-dbinfo-file target-dbinfo.id `
  --data-root data --windows 20,60
```

The target singleton is written only during the explicit `apply` phase after
the backup/restore manifest is verified. If a phase is interrupted, rerun it
with the same source/target configurations and journal; do not edit source
configuration, table names, completion flags, or the journal to force progress.
After the target passes validation, `cutover` records the exact schema version
and registry digest. The operator switches the active dbinfo atomically, and
only then may the separately confirmed `retire` phase remove legacy tables.
