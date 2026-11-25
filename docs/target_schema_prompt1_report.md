# Prompt 1: live MySQL state inspection

Inspected 2026-11-23. All database statements in this inspection ran inside
`START TRANSACTION READ ONLY` and ended with rollback. No database or
configuration file was changed. Credentials were not included in the report.

## Result

**Cutover gate: blocked by code/schema identity mismatch.** The live target
`stock_data` is still at migration phase `validated`; it has no completed
`cutover:confirmed` step. The active `dbinfo.id` already names `stock_data`,
but there is no local cutover marker. The intent and timing of that config
switch cannot be established from the available files.

| Check | Live value | Current code expects | Result |
|---|---|---|---|
| Source/target server | Same MySQL server UUID | Same server | Pass |
| Migration engine | `2` in control identity | `2` | Pass |
| Migration ID | `ca806ebf70e9fa4882433cd904cfb72542c4275170a9bc1e64a67b9a59687711` | Same | Pass |
| Registry digest | `f09f7886715ed8fea752d783f39c44f2225847f3b520cd2e16640ca56178d347` | `2ebd98883a98e77686a5803e8c0eb49c4df395bd3fb7d8aa844619b4a3a07e3d` | Fail |
| Schema metadata | `stockguidance_simplified`, schema `1`, registry `2`, old digest | Schema `1`, registry `3`, new digest | Fail |
| Migration checkpoint | Phase `validated`, 38 complete steps, 0 incomplete, 0 cutover confirmation | Cutover requires compatible validated state | Incomplete |
| Pending updates | 0 rows | 0 for idle cutover | Pass |
| Source singleton | One row in each database; same configuration key and semantic source fields | Compatible source | Pass for inspected fields |
| Existing target tables | Base target tables and four price/price-delta z-score tables | These plus `zscores_volume_20` and `zscores_volume_60` | Fail |
| Completion metadata | 5,247 sessions, 5,015 raw complete, 5,011 delta complete, 236 deferred | Defer unknown completion explicitly | Consistent with saved journal; not fully ready |

The legacy source is `stockforecasting`; the target is `stock_data`. Both are
on the same server. The source singleton has the same configuration key,
provider family, endpoint, market, universe, interval and raw adjustment flags
in both databases. The target has the expected `XNYS` calendar field, which
the legacy singleton does not store. The target source key differs from the
legacy key because the target identity includes the calendar; this is not the
checkpoint mismatch.

## Exact registry difference

Recomputing the earlier registry digest from the current `target_tables()` and
`templates()` payload produces the live old digest exactly. Commit `4298e4f`
changed the digest payload by adding only:

```json
{"preparation_templates":{"volume":"zscores_volume_{window}"}}
```

That commit also raised `REGISTRY_VERSION` from `2` to `3` and made the schema
initializer create volume materialization tables for configured windows. The
live target contains the four old price/price-delta tables for windows 20 and
60 and lacks both volume tables. No other registry role or template difference
was found. The migration ID and server identity match; the added volume
template alone reproduces the digest mismatch. The current
`require_simplified_schema()` guard rejects the target before ordinary
training or prediction reads because the registry version and digest differ.

The live z-score series definitions contain price and price-delta for windows
20 and 60; there are no volume definitions. That is consistent with an empty
new volume materialization, but the additive upgrade needs an explicit plan.

## What must change before cutover

1. Reconcile the validated v2 target with the v3 runtime contract through an
   explicit, tested upgrade or a compatible v2 cutover followed by a separate
   v3 upgrade. The upgrade must create `zscores_volume_20` and
   `zscores_volume_60` with the current initializer's keys, columns, indexes and
   foreign keys, and move schema metadata to registry version 3 only after
   independent checks. It must also handle the migration control identity and
   journal consistently. Editing either digest by hand would discard the
   evidence that the copy was validated under v2.
2. Re-run the migration status and bounded source/target validation using the
   reconciled implementation. October's 38 completed checkpoints establish
   what was checked then, not whether either database has changed since. The
   checkpoint rows have no historical value digest that can prove current
   equality; a fresh full validation is required before cutover.
3. Reconcile the active `dbinfo.id` switch with the still unconfirmed cutover.
   Audit any work performed against `stock_data` since the switch and keep its
   values stable during renewed copy acceptance. The provided files do not
   show whether the switch was deliberate.
4. Review the 236 deferred completion entries for the intended training and
   prediction dependency ranges. They are explicitly incomplete; do not
   promote them from migration evidence. Target maintenance/readiness handles
   relevant gaps after cutover.

No cutover command was run. `migrate_database status` currently exits with
`Database checkpoint identity does not match`; this read-only inspection read
the control and metadata rows directly to identify why. Follow
[Prompt 2](target_schema_prediction_finish.md) for the implementation gate and
the [Stage 12 SQL runbook](database_simplification_stage12_flow.md) for the
eventual validation and cutover sequence.
