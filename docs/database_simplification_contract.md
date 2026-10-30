# Database simplification replacement contract

Status: Stage 5 contract update with the Stage 3 preparation amendment. This document specifies the replacement that
later stages must implement. It does not initialize, migrate, or alter a
production database. The Stage 2 disposable-MySQL measurement gate passed and
selected the raw-column layout below under the required decision rule. Stages
4-5 now provide the pure dependency planner and recovery boundary described
below.

## 1. Scope and compatibility rules

The database is the authoritative owner of the values used by training,
prediction, recommendation, simulation, and historical evaluation. A database
has exactly one source configuration. An import is admissible only when its
source family, market, universe, interval, and raw price-adjustment semantics
match that configuration. Endpoint and provider receipt evidence are also
checked against the configured source identity. A mismatch is rejected; it is
never made safe by adding a second configuration row or by mixing rows from
two providers.

The redesign keeps the current close-to-close feature and target definitions,
the 16 `legacy-open-to-close-v1` derived columns, the two z-score families,
their configured windows, Decimal source precision, stable ticker IDs, and the
existing strict and retrospective availability policies. Stage 3 adds only the
explicitly versioned volume z-score materialization family required by the
accepted preparation contract; it does not add general feature vectors or
models. Saved models are never deleted or silently retrained.

The current packaged registry remains the authority for the existing physical
names during the staged cutover. The target registry adds logical roles for
the new tables below in the schema-versioned Stage 3 work. Stage 1 does not
change `src/stockguidance/database/table_names.json`, `update_schema.py`, or
any production entrypoint.

## 2. Target table inventory

All target tables are InnoDB. SQL values are parameters. Only registry-resolved
identifiers may be interpolated into SQL and every resolved identifier must
pass the existing `[A-Za-z_][A-Za-z0-9_]*`, maximum-64-character validation.
Foreign-key relationships are represented by integer IDs and are checked by
the repositories; the target does not require a permanent dependency graph.

The target logical registry contains the current roles `source`, `sessions`,
`tickers`, `prices`, `deltas`, `zscore_series`, `zscore_sessions`, and the
per-window z-score tables, plus these roles:

| Logical role | Target physical name | Purpose |
| --- | --- | --- |
| `data_states` | `stockguidance_data_states` | Small immutable source-state lookup. |
| `pending_updates` | `stockguidance_pending_updates` | One durable active operation and its immutable scope. |
| `repair_metadata` | `stockguidance_repair_metadata` | Exceptional source observations and repair provenance. |

`stockguidance_imports` is not a target role. Its receipt fields move to
`transaction_dates`. Stage 2 records `raw_stock_data.state_id` as the selected
layout. The separate `stockguidance_raw_data_states(date_id,ticker_id,state_id)`
candidate remains the benchmark comparison only. Both layouts have exactly the
same logical API and completion semantics. The measured result and decision
rule are recorded in the Stage 2 handoff and benchmark output; Stage 3 must
implement the raw-column selection.

### 2.1 Singleton source configuration

`stockguidance_source` has one and only one row:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `id` | `INT UNSIGNED` | Primary key; must equal `1`. |
| `configuration_key` | `CHAR(64)` | Hash of the canonical semantic data configuration. |
| `source_key` | `CHAR(64)` | Hash of the complete non-secret source identity. |
| `source_family` | `VARCHAR(64)` | Canonical provider family. |
| `endpoint` | `VARCHAR(512)` | Configured endpoint/request family. |
| `market` | `VARCHAR(64)` | Configured market. |
| `universe` | `VARCHAR(128)` | Configured universe. |
| `interval_code` | `VARCHAR(8)` | Current supported daily interval. |
| `calendar_id` | `VARCHAR(32)` | Exchange calendar used for session ordinals. |
| `raw_price_split_adjusted` | `TINYINT(1)` | Exact adjustment flag. |
| `raw_price_dividend_adjusted` | `TINYINT(1)` | Exact adjustment flag. |
| `raw_price_vendor_defined_adjustment` | `VARCHAR(128) NULL` | Exact vendor adjustment policy. |
| `created_at` | `DATETIME(6)` | UTC database creation time, not market time or receipt time. |

The primary key plus `CHECK (id = 1)` enforces the addressable singleton.
Initialization is explicit and must reject a nonempty table with zero rows,
multiple rows, or incompatible values. A second configuration is never
inserted. The source configuration is immutable for ordinary imports; changing
it requires the separately authorized migration/cutover process.

### 2.2 Sessions, tickers, raw observations, and deltas

`transaction_dates` retains its existing stable `id` and `transaction_date`
identity and gains persistent completion and receipt fields:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `id` | `INT UNSIGNED AUTO_INCREMENT` | Integer relationship key; never reused. |
| `transaction_date` | `DATE` | Exchange session date, unique. |
| `raw_complete` | `TINYINT(1)` | True only after the raw session unit commits, including explicit no-trading/missing outcomes. |
| `raw_available_at` | `DATETIME(6) NULL` | Actual source response receipt/availability time in UTC. |
| `raw_completed_at` | `DATETIME(6) NULL` | UTC database commit time for the completed raw unit. |
| `raw_revision` | `CHAR(64) NULL` | Canonical validated response identity; null until raw completion. |
| `delta_complete` | `TINYINT(1)` | True only after the delta session unit commits. |
| `delta_completed_at` | `DATETIME(6) NULL` | UTC database commit time for the completed delta unit. |

`raw_complete` and `delta_complete` are independent. A row in
`transaction_dates` is not evidence that either phase is ready. A completed
raw session may have zero valid market rows, explicit known-missing rows, or
bad rows; those outcomes are represented by source state, not by a false
completion flag. Completion flags are cleared before their descendants are
rebuilt and are set in the same transaction as the unit they describe.

`ticker_symbol` retains stable integer `id`, binary/case-sensitive unique
`label VARCHAR(16)`, and nullable `exchange VARCHAR(10)`. Ticker identity is
case-sensitive and is never repaired by case folding or by replacing an ID.

`raw_stock_data` retains the existing keys and Decimal widths:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `ticker_id` | `INT UNSIGNED` | Relationship to `ticker_symbol.id`. |
| `date_id` | `INT UNSIGNED` | Relationship to `transaction_dates.id`. |
| `open`, `high`, `low`, `close` | `DECIMAL(28,8) NULL` | Exact source/effective prices. |
| `volume` | `DECIMAL(20,8) NULL` | Exact nonnegative quantity. |
| `vwap` | `DECIMAL(42,22) NULL` | Exact source VWAP. |
| `transactions` | `BIGINT UNSIGNED NULL` | Exact nonnegative count. |
| raw state | `SMALLINT UNSIGNED` | `data_states.id`, stored as `raw_stock_data.state_id` under the Stage 2 raw-column selection. |
| `observed_at` | `DATETIME(6) NULL` | Provider market-observation timestamp, if supplied. |

The primary key is `(ticker_id,date_id)`. For state `good`, values are direct
validated source values. For `interpolated`, values are the effective values
and the original values/endpoints are in repair metadata. For `missing`, all
observation value columns are NULL. For `bad`, invalid values are not used as
effective feature inputs; any representable raw fragments and the complete
original payload are retained in repair metadata. A row is created for a
known missing observation only when coverage rules explicitly identify that
key; absence of a ticker/date by itself does not justify a placeholder row.

`daily_delta_stock_data` retains all 16 columns in the current order and
`DOUBLE NULL` numerical representation so existing formula and artifact
contracts remain recognizable. It additionally has:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `ticker_id`, `date_id` | `INT UNSIGNED` | Primary key relationships. |
| existing 16 derived columns | `DOUBLE NULL` | Current formulas; NULL is an intentional unavailable outcome, never zero substitution. |
| `calculation_status` | `VARCHAR(32)` | Readable derived status, independent of source `data_states`. |
| `quality_flags` | `SMALLINT UNSIGNED` | Aggregated provenance flags over the actual input set. |
| `gap_length` | `INT UNSIGNED` | Maximum propagated interpolation gap. |
| `provenance` | `JSON` | Structured input revisions/quality evidence, not a dependency graph. |
| `completed_at` | `DATETIME(6)` | UTC completion time for this calculation unit. |

The delta row is present after its unit commits even if a field is NULL or the
status is not ready. `calculation_status` values include `ready`,
`insufficient_history`, `missing_input`, `invalid_input`, and
`not_yet_available`; these are calculation outcomes, not raw source states.

The derived columns, in order, are exactly:

```text
open_ratio_to_prior, high_ratio_to_prior, low_ratio_to_prior,
close_ratio_to_prior, volume_ratio_to_prior, vwap_ratio_to_prior,
transactions_ratio_to_prior, change_in_price_over_day,
next_day_change_in_price, zero_centered_next_day_price_change,
next_day_price_change_to_extremes_0_1, next_day_raise1_lower0,
next_day_top100_raise1_lower0, zero_center_extremes_neg1_pos1,
zero_center_top100_1_other_neg1, zero_center_raise1_lower_neg1
```

### 2.3 Existing z-score table fields

The retained z-score tables have these exact logical fields. Their physical
per-window names continue to come only from the selected registry template.

| Table | Fields and types |
| --- | --- |
| `stockguidance_zscore_series` | `series_key CHAR(64)`, `schema_version INT`, `source_scope CHAR(64)`, `calendar_id VARCHAR(128)`, `family VARCHAR(32)`, `window INT`, `calculation_version VARCHAR(128)`, `delta_definition VARCHAR(128)`, `precision VARCHAR(32)`, `missing_value_policy VARCHAR(64)`, `created_at DATETIME(6)`, primary key `series_key`, and the existing definition uniqueness key. |
| `stockguidance_zscore_sessions` | `series_key CHAR(64)`, `date_id INT UNSIGNED`, `dependency_digest CHAR(64)`, `availability_digest CHAR(64)`, `actual_input_available TINYINT(1)`, `completed_at DATETIME(6)`, `row_count INT`, `ready_count INT`, `status_counts JSON`, `content_digest CHAR(64)`, primary key `(series_key,date_id)`. |
| each `zscores_price_{window}` / `zscores_price_delta_{window}` / `zscores_volume_{window}` | `date_id INT UNSIGNED`, `ticker_id INT UNSIGNED`, `zscore FLOAT NULL`, `status TINYINT`, `quality_flags SMALLINT UNSIGNED`, `gap_length INT UNSIGNED`, `provenance JSON`, primary key `(date_id,ticker_id)`. |

The quality/provenance fields in the last row replace the current separate
derived-output quality storage; the score/status and fingerprint semantics do
not change. The series definition uniqueness key remains
`(source_scope,calendar_id,family,window,calculation_version,delta_definition,precision,missing_value_policy)`.

### 2.4 State and repair metadata

`stockguidance_data_states` is immutable reference data:

| `id` | `code` | Meaning |
| ---: | --- | --- |
| 1 | `good` | Valid direct source observation. |
| 2 | `bad` | Source observation exists but failed validation and has no usable effective value. |
| 3 | `missing` | The requested observation is explicitly known to be absent/unavailable. |
| 4 | `interpolated` | Source observation was invalid and a contract-approved effective value was calculated from bounded endpoints. |

Its fields are `id SMALLINT UNSIGNED PRIMARY KEY`, `code VARCHAR(16) UNIQUE
NOT NULL`, and `description VARCHAR(255) NOT NULL`. IDs and codes are fixed;
readers reject unknown rows rather than assigning a new meaning.

`stockguidance_repair_metadata` contains exceptional rows only. Its target
fields are:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED AUTO_INCREMENT` | Integer metadata identity. |
| `date_id`, `ticker_id` | `INT UNSIGNED` | Integer source relationships. |
| `revision` | `CHAR(64)` | Hash of this canonical repair record. |
| `state_id` | `SMALLINT UNSIGNED` | One of the four source states. |
| `original_values` | `JSON` | Ordered seven-field array; Decimal values are JSON strings or null. |
| `effective_values` | `JSON NULL` | Ordered effective array, or null when unusable. |
| `invalid_fields` | `JSON` | Ordered names from `open,high,low,close,volume,vwap,transactions`; `[]` for an explicit missing record. |
| `interpolation_method` | `VARCHAR(64) NULL` | Current `linear-trading-session-v1`, only for interpolation. |
| `prior_date_id`, `following_date_id` | `INT UNSIGNED NULL` | Integer interpolation endpoints; both present for interpolation. |
| `endpoint_values` | `JSON NULL` | Exact endpoint arrays used by interpolation. |
| `observed_at` | `DATETIME(6) NULL` | Source market-observation time. |
| `available_at` | `DATETIME(6) NULL` | Effective observation availability under the policy. |
| `endpoints_available_at` | `DATETIME(6) NULL` | Latest endpoint availability. |
| `recorded_at` | `DATETIME(6)` | UTC persistence time. |

The current repair record for a `(date_id,ticker_id)` is selected by its
committed revision; a later committed revision replaces the current record.
The table may retain older revisions for reproducibility/audit, but it is not
used as a general row-lineage graph. Repair metadata is deleted only when its
row is in the durable pending clear scope.

### 2.5 Pending updates

`stockguidance_pending_updates` contains at most one active row while the
maintenance advisory lock is held. Completed rows are deleted. The table
fields are:

| Column | MySQL type | Contract |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED AUTO_INCREMENT` | Integer operation ID used by Python and Java. |
| `operation_kind` | `VARCHAR(32)` | `initial_import`, `forced_replacement`, `derived_repair`, or `recovery`. |
| `scope_version` | `SMALLINT UNSIGNED` | Version of the structured scope; current target is `1`. |
| `scope` | `JSON` | Complete clear/rebuild/cache scope and prepared-payload references. |
| `created_at` | `DATETIME(6)` | UTC operation registration time. |
| `clearing_started_at` | `DATETIME(6) NULL` | UTC time the first clear transaction began. |
| `affected_rows_cleared` | `TINYINT(1)` | False until every scoped clear and completion deletion has committed; then true in one flag transaction. |
| `failure_code` | `VARCHAR(64) NULL` | Stable readable failure category. |
| `failure_detail` | `TEXT NULL` | Non-secret diagnostic detail; never credentials. |
| `failed_at` | `DATETIME(6) NULL` | UTC last failure time. |

The operation is considered unfinished while this row exists. Failure details
do not clear the row. A scope is inserted before the first delete and is
immutable from insertion onward; only failure fields and the clear flag may
change. New requests wait for the active operation, then plan against the
post-operation state. A pending row is never inferred complete from date
presence or from a partially populated table.

The scope JSON is canonical and versioned. Version 1 has exactly these top
level fields:

```json
{
  "scope_version": 1,
  "operation_kind": "forced_replacement",
  "configuration_key": "<64 lowercase hex>",
  "calendar_id": "XNYS",
  "requested_at": "2026-10-26T16:00:00.000000Z",
  "cutoff": "2026-10-26T16:00:00.000000Z",
  "payloads": [{
    "payload_id": "<operation-local id>",
    "session_date": "2026-10-23",
    "temporary_path": "<operation-owned temporary path>",
    "sha256": "<64 lowercase hex>",
    "raw_id": "<64 lowercase hex>",
    "available_at": "2026-10-24T00:00:00.000000Z",
    "prepared_at": "2026-10-26T16:00:00.000000Z"
  }],
  "clear": {"raw": [], "repairs": [], "deltas": [], "zscores": [], "completion": []},
  "rebuild": [{"unit_kind": "raw_session", "date_id": 42, "session_date": "2026-10-23"}],
  "cache_invalidations": [{"revision_scope": "...", "session_dates": ["2026-10-23"]}]
}
```

The example is illustrative data, not a production record. `clear` and
`rebuild` must enumerate the same complete affected scope using integer date
and ticker IDs, series keys, windows, and bounded session ordinals as
appropriate. Work-unit order is deterministic. Payloads must be validated and
durably available before clearing. A forced replacement prepares and hashes
the intended replacement before clearing; a pre-refresh canonical file, even
if its path and file statistics match, cannot satisfy a resume unless it is
the payload recorded for this operation. These payloads are operation-owned
temporary artifacts, not a permanent file catalog. Completed database rows do
not require the original canonical file to remain present.

### 2.6 Z-score status meanings

The current `stockguidance_zscore_series`, `stockguidance_zscore_sessions`,
and per-window tables remain. Their existing definition identity, float32
ready-score precision, dependency/availability digests, status counts, and
content fingerprints remain required. New timestamps are `DATETIME(6)` UTC.
Per-window rows retain `(date_id,ticker_id)` and nullable `FLOAT zscore` plus
the current numeric statuses:

| Code | Status | Value rule |
| ---: | --- | --- |
| 0 | `ready` | Finite float32 score required. |
| 1 | `insufficient_history` | Score must be NULL. |
| 2 | `missing_input` | Score must be NULL. |
| 3 | `unverified_input` | Score must be NULL. |
| 4 | `invalid_input` | Score must be NULL. |
| 5 | `zero_variance` | Score must be NULL. |

These are derived calculation statuses, not `data_states`. A completed z-score
session records every requested ticker outcome, including non-ready outcomes;
missing rows are never interpreted as zero or as an unfinished retry. The
actual input session set, quality flags, gap length, and provenance are part of
the dependency/content metadata so interpolation and future-leakage rules are
auditable. Existing artifact/model/cache fingerprints are retained.

## 3. Time, quality, and availability semantics

The three time concepts are never interchangeable:

* `observed_at` is when the market/provider says the observation occurred.
  It is a source fact and may be absent for a daily response.
* `available_at` is when the complete response or effective repaired value was
  actually available to the application. It gates strict historical reads.
* `ingested_at` is represented by the UTC completion timestamp of the database
  unit (`raw_completed_at`, `delta_completed_at`, or `recorded_at`). It says
  when this database revision was committed and does not prove historical
  availability.

Strict reads accept only values and repair endpoints available by the query
cutoff. Retrospective research may use a later effective interpolation value
only under the existing explicit retrospective policy and must propagate
`future_leakage` provenance. Importing a row today never proves that it was
available at an earlier decision.

The existing interpolation algorithm remains `linear-trading-session-v1`:
it operates over consecutive exchange-session ordinals for one ticker, repairs
only invalid fields, rounds prices/quantities to current Decimal precision,
requires valid bounded endpoints, and records the endpoint dates, endpoint
values, endpoint availability, effective values, and original values. Missing
or bad observations without both endpoints stay NULL/unusable. Quality flags
are propagated over the actual calculation inputs, including all rolling
windows and ranking inputs.

## 4. Transaction, lock, and recovery boundaries

1. A writer acquires the existing MySQL advisory lock
   `stockguidance_daily_maintenance` and holds it through recovery, planning,
   registration, clearing, rebuilding, cache invalidation, validation, and
   pending-row deletion. Python and Java use the same name and wait semantics.
2. Before accepting a new request, the writer loads an unfinished pending row.
   Its scope is immutable. If `affected_rows_cleared = false`, it validates the
   recorded payloads and repeats idempotent bounded clearing. Rebuild is
   unreachable until the flag is durably true.
3. Planning reads a consistent database snapshot and computes the complete
   transitive closure before any dependency evidence is removed. Registration
   commits one full pending row before the first delete.
4. Clearing deletes only bounded primary-key/date chunks listed in `clear`.
   Each chunk may commit separately. Repeating a chunk is harmless. The final
   `affected_rows_cleared = true` transition is committed only after every
   affected raw row, repair row, delta row, z-score row, completion record, and
   relevant cache-revision record is clear.
5. Each `rebuild` calculation unit commits atomically. A raw session unit
   commits raw rows, source states, repair metadata, receipt/completion fields,
   and its raw completion together. A delta unit commits delta values,
   calculation status/provenance, and delta completion together. A z-score
   unit commits all rows and its existing session completion metadata together.
   A committed explicit non-ready result is complete work.
6. If a process stops before the clear flag commit, recovery repeats clearing
   and does not rebuild. If it stops after the flag commit, recovery preserves
   already committed rebuild units and resumes only incomplete units. If a
   unit transaction rolls back, its completion marker and its output rows are
   both absent and the unit is retried. Failure details remain on the pending
   row until successful completion.
7. After all units pass validation, affected dataset/prediction caches are
   invalidated or revision-bumped, saved models remain, and the pending row is
   deleted in the final operation transaction. An artifact trained on an old
   data revision is reported as old; it is not silently reused as current and
   is not automatically retrained.

Readers start a repeatable-read transaction, inspect pending state in that same
transaction, and compare their complete requested scope with the pending
`clear`/`rebuild` scope. An overlapping reader blocks or returns a distinct
`unfinished_operation` result. An unrelated reader remains available from its
consistent snapshot. A missing row is never used as the reader guard. A
reader whose transaction began before operation registration may finish its
already-established snapshot; a reader beginning after registration observes
and honors the guard.

## 5. Dependency map

The planner must use exchange-session ordinals and the actual feature/target
windows. It must not use a fixed calendar-day radius or a permanent lineage
graph. All affected units are deduplicated deterministically before they are
persisted.

### Source and raw state

* A source replacement or initial import for session `d` affects the raw
  session unit for `d`, its raw completion and receipt, all exceptional repair
  rows for `(d,ticker)`, and any explicit missing/bad source keys in the
  replacement payload.
* A changed interpolation endpoint affects the complete consecutive-session
  interval for that ticker bounded by the endpoint pair, including any
  dependent repairs whose endpoint availability or effective value changes.
  A newly filled gap is planned the same way. A withdrawn ticker changes only
  the actual source keys and dependent calculations; it does not create a
  Cartesian product of placeholders.

### Legacy derived columns

For current row `s`, the formulas use prior session `p`, current session `s`,
and following session `f` as follows:

| Derived columns | Actual inputs |
| --- | --- |
| `open/high/low/close/volume/vwap/transactions_ratio_to_prior` | Same ticker at `p` and `s`. |
| `change_in_price_over_day` | Same ticker at `s`. |
| `next_day_change_in_price`, `zero_centered_next_day_price_change`, `next_day_price_change_to_extremes_0_1`, `next_day_raise1_lower0`, `zero_center_extremes_neg1_pos1`, `zero_center_raise1_lower_neg1` | Same ticker at `s` and `f`. |
| `next_day_top100_raise1_lower0`, `zero_center_top100_1_other_neg1` | The full eligible cross-ticker ranking at `f`, plus the current ticker at `s`. |

Therefore a raw correction at `d` plans delta rows at `d-1`, `d`, and `d+1`
where those exchange sessions exist, with the exact ticker scope determined by
the formula. The two cross-sectional ranking columns conservatively invalidate
the entire affected ranking session when its boundary can change. Holiday
boundaries use session ordinals, not adjacent calendar dates. Each affected
delta row's quality is recomputed from every actual raw/repair input used by
its columns.

### Z-scores and completion

For a price family window `W`, a scored session `s` depends on the same
ticker's close observations for the `W` exchange sessions ending at `s`. For
the price-delta family it depends on the `W` close-ratio inputs and therefore
the `W+1` closes ending at `s`, including the predecessor close. A changed raw
or repaired input plans all configured scored sessions whose actual window
contains it, the affected ticker IDs, each series/window row, and the
corresponding `zscore_sessions` completion metadata. A delta correction also
plans the z-score units that consume the changed delta values. Future-dependent
label columns are included in invalidation and rebuild scope but never enter
feature inputs.

### Consumers, caches, and artifacts

The feature lookback, normalization warm-up, and target/label dependencies are
read from the existing feature and target specifications. Training datasets,
prediction outputs, recommendation scores, and historical evaluation caches
whose source revision intersects the closure are invalidated or revision-bumped.
The planner records this cache scope explicitly. It never deletes saved model
files. Existing artifact fingerprints remain required to explain whether an
artifact used the current or an older database revision.

## 6. Python/Java operation interchange

The durable pending scope is the interchange format. Python uses `date`,
timezone-aware `datetime`, `int` IDs, `bool`, `str`, and immutable tuples in its
typed contracts. Java maps dates to `LocalDate`, UTC timestamps to `Instant`,
IDs to `long`, and the structured scope to the existing canonical JSON support.
Both languages must reject unknown scope versions, duplicate JSON fields,
unknown operation/unit kinds, non-UTC timestamps, duplicate work units, and
invalid identifier/table names. Decimal payload values are strings in JSON so
neither implementation rounds them during interchange.

The shared enum values are:

```text
operation_kind: initial_import | forced_replacement | derived_repair | recovery
unit_kind: raw_session | repair_row | delta_session | zscore_session | cache_revision
data_state: good | bad | missing | interpolated
calculation_status: ready | insufficient_history | missing_input |
                    invalid_input | not_yet_available | zero_variance
```

The Java writer and z-score calculator must use the same current formulas and
status/value invariants as Python. The existing Java migration and benchmark
manifests are historical inputs and must not become a second live operation
protocol.

## 7. Exact later-stage interfaces

These are the interfaces later stages implement. Names are contractual even
if the concrete modules are split; no later stage may infer a different scope
or transaction boundary.

### Planner

```python
class OperationKind(StrEnum):
    INITIAL_IMPORT = "initial_import"
    FORCED_REPLACEMENT = "forced_replacement"
    DERIVED_REPAIR = "derived_repair"
    RECOVERY = "recovery"

@dataclass(frozen=True)
class UpdateRequest:
    source: SourceConfig
    sessions: tuple[TradingSession, ...]
    as_of: datetime
    operation_kind: OperationKind
    forced_sessions: frozenset[date] = frozenset()

class DependencyPlanner(Protocol):
    def plan(self, request: UpdateRequest, snapshot: DatabaseSnapshot) -> PendingScope: ...
```

Stage 4 implements this boundary in
`src/stockguidance/database/dependency_planner.py`. Its immutable
`DatabaseSnapshot` contains the exchange-session/date-ID inventory, actual
raw/delta/repair ticker keys, prepared replacement ticker keys, interpolation endpoint intervals, configured
`ZScoreSeriesDefinition` windows and completion IDs, and explicit cache
revision evidence. `FeatureSpec`, `TargetSpec`, and normalization warm-up
settings are carried with that snapshot for bounded cache invalidation. The
planner performs no database, provider, or filesystem operation and rejects a
snapshot whose supplied source configuration or calendar disagrees with the
request. A missing optional configuration key is accepted only for fixture
snapshots; a populated key must match the request.

The planner records raw sessions, exceptional repair rows, predecessor/current/
follower delta rows, ranking-session populations, z-score rows and completion
records independently in `clear` and `rebuild`. Price windows cover `W`
exchange sessions and price-delta windows cover `W+1` close sessions. Cache
entries with dependency evidence are selected by actual overlap; cache scopes
without entry lineage are bounded by the configured feature lookback,
normalization warm-up, and target horizon. No Cartesian placeholder keys are
created for withdrawn or unavailable tickers.

Stage 5 implements the coordination boundary in
`src/stockguidance/database/operation_recovery.py`. The coordinator acquires
the existing `stockguidance_daily_maintenance` advisory lock, loads and
validates any active pending scope before planning a new request, and validates
all `PreparedPayload` files and SHA-256 identities before registration or
recovery clearing. `DatabasePendingClearer` deletes only the persisted raw,
repair, delta, z-score, and completion units in bounded committed chunks. It
preserves sessions, tickers, source configuration, the data-state dictionary,
the pending scope, and prepared payloads. Cache invalidation is an injected,
retryable sink; saved model files are outside this operation and are never
deleted.

The `affected_rows_cleared` transition is committed only after the pending
scope is reloaded and every scoped database row/completion record is absent.
The transition remains false after chunk or cache failure, so repeating the
clear is harmless. The coordinator has no rebuild implementation: an injected
later-stage runner is called only after the true flag is committed, and a
Stage 5-only coordinator raises `RebuildRequiredError` instead of accepting a
new operation over uncleared or unrecalculated work.

`DatabaseReadinessGuard.check` reads pending state through the transaction
supplied by the caller. `consistent_read_transaction` establishes a MySQL
`REPEATABLE READ` snapshot before the guard and dependent reads; overlapping
scopes return `unfinished_operation`, while unrelated scopes remain readable.
Readers never infer readiness from a missing data row. `MaintenanceWriterLock`
also verifies the owning MySQL connection with `IS_USED_LOCK` before each clear
chunk, and lock loss aborts without setting the clear flag.

The supporting types are fixed at this boundary:

```python
@dataclass(frozen=True)
class PendingScope:
    scope_version: int
    operation_kind: OperationKind
    configuration_key: str
    calendar_id: str
    requested_at: datetime
    cutoff: datetime
    payloads: tuple[PreparedPayload, ...]
    clear: ClearScope
    rebuild: tuple[WorkUnit, ...]
    cache_invalidations: tuple[CacheInvalidation, ...]

@dataclass(frozen=True)
class WorkUnit:
    unit_kind: str
    date_id: int | None
    ticker_id: int | None
    session_date: date | None
    series_key: str | None
    window: int | None

@dataclass(frozen=True)
class PendingUpdate:
    operation_id: int
    scope: PendingScope
    created_at: datetime
    clearing_started_at: datetime | None
    affected_rows_cleared: bool
    failure: FailureDetail | None

@dataclass(frozen=True)
class FailureDetail:
    code: str
    detail: str
    failed_at: datetime
```

`PreparedPayload`, `ClearScope`, and `CacheInvalidation` are immutable typed
records corresponding one-for-one to the JSON fields above. `WorkUnit` uses
integer date/ticker relationship keys where applicable; `session_date` is a
display/validation value and never replaces `date_id`. A raw session unit has
`date_id` and `session_date`, a repair row has both IDs, a delta session has a
date and explicit ticker scope in its `ClearScope`, a z-score unit has
`series_key`, `window`, `date_id`, and explicit ticker IDs, and a cache unit
has its revision scope. `None` is permitted only for fields not applicable to
that unit kind.

`PendingScope` contains the version, operation kind, configuration key,
cutoff, prepared payload descriptors, complete clear scope, complete rebuild
units, and cache invalidations. `plan` is pure with respect to the database;
it does not delete, insert, download, or mutate a repository.

### Pending repository

```python
class PendingUpdateRepository(Protocol):
    def register(self, scope: PendingScope, created_at: datetime) -> PendingUpdate: ...
    def load_active(self) -> PendingUpdate | None: ...
    def begin_clearing(self, operation_id: int, started_at: datetime) -> None: ...
    def record_failure(self, operation_id: int, failure: FailureDetail, failed_at: datetime) -> None: ...
    def mark_cleared(self, operation_id: int, cleared_at: datetime) -> None: ...
    def delete_completed(self, operation_id: int, completed_at: datetime) -> None: ...
```

`register` commits the complete scope before returning. `mark_cleared` is the
only transition that sets `affected_rows_cleared`; it is rejected if any
scoped clear unit is not committed. `record_failure` and `delete_completed`
never rewrite scope. Implementations use parameterized values and the selected
registry for identifiers.

### Clearer

```python
class PendingClearer(Protocol):
    def clear(self, operation: PendingUpdate, connection: WriteConnection) -> ClearResult: ...
```

`clear` may commit bounded idempotent chunks but cannot call a rebuild runner,
change the scope, or set the clear flag. `ClearResult` reports exact completed
clear units and must be resumable from the durable scope.

### Rebuild runner

```python
class RebuildRunner(Protocol):
    def rebuild(self, operation: PendingUpdate, connection: WriteConnection) -> RebuildResult: ...
```

It rejects `affected_rows_cleared == False`. It processes only persisted units,
skips units with their committed completion evidence, and commits one raw,
delta, or z-score unit atomically. `RebuildResult` distinguishes committed,
explicitly non-ready, deferred-not-yet-available, and failed units; it never
represents NULL/unavailable values as zero.

### Readiness guard

```python
class ReadinessGuard(Protocol):
    def check(self, query: ReadScope, transaction: ReadOnlyTransaction) -> Readiness: ...
```

`check` runs after the repeatable-read transaction begins and before dependent
rows are read. `Readiness` is `ready`, `unfinished_operation`, or a structured
coverage failure containing phase, date, ticker/series, and reason. It does
not initialize schema, download data, repair rows, or clear pending state.

## 8. Current consumers and migration destinations

The following inventory was traced from the current repository. “Historical”
means it remains usable only against explicitly disposable fixtures and is not
part of the supported replacement runtime.

| Current consumer | Current behavior | Replacement destination |
| --- | --- | --- |
| `database/update_schema.py` | Historical fixture initializer for base/archive/source-file/partial-import/verification/quality tables. | Explicit versioned target schema and registry; legacy initializers reject target databases. |
| `database/repositories/mysql_sessions.py`, `mysql_market_data.py`, `mysql_market_read.py` | Reads/writes sessions, raw rows, deltas, imports, partial markers, and quality. | Session/raw/delta repositories behind the pending coordinator, source states, repair metadata, and readiness guard. |
| `market_data/archive.py`, `canonical_store.py`, `manifests.py` | Validates canonical NDJSON and stores local manifests. | Direct exact canonical-file lookup plus operation-owned prepared payloads; no permanent archive catalog. |
| `market_data/quality.py`, `mysql_quality.py`, `repositories/quality.py` | Classifies invalid rows, interpolates, stores JSON quality/current/audit/output tables. | `data_states`, structured repair metadata, raw/delta quality/provenance, and atomic calculation units. |
| `market_data/update_planning.py`, `coverage_planning.py`, `workflows/update_market_data.py` | Plans downloads and uses a fixed local dependency radius in places. | Pure dependency planner and pending scope; actual session windows and source payload preparation. |
| `workflows/daily_maintenance.py` and `daily_maintenance_*` | Historical fallback uses archive catalog, source-file flags, partial-import markers, and Java migration completion. | Target dispatch uses one Python coordinator, one advisory lock, pending scope, and shared interchange; legacy context rejects target schemas. |
| `workflows/update_zscores.py`, `database/repositories/mysql_zscores.py` | Reads quality and partial/source-file markers; persists z-score completion. | New operation scope and state/provenance inputs; retain z-score series/session completion contract. |
| `training/jobs.py`, feature/dataset builders | Read database-derived values and local opening evidence for training/evaluation. | Read-only repositories plus readiness guard; orchestration expands exact lookback/label dependencies and invalidates stale caches. |
| `workflows/recommend.py`, `recommendation_runtime.py` | Builds current prediction/recommendation workflow from database closes and opening files. | Same feature path and readiness guard; no low-level repository writes. |
| `workflows/simulation_runtime.py`, simulation modules | Historical score and close/open reads. | Same read snapshot and revision guard; preserve simulation policies. |
| `workflows/validate_database.py`, validation repositories | Independent read-only checks, with legacy quality/partial markers. | State/completion-aware diagnostics; never initialize, repair, or clear. |
| Java `JavaDailyMaintenanceMain`, `JavaWriterCandidate`, `JavaZScore*`, `OperationalDatabase`, `RowQuality` | Historical migration/benchmark writer logic uses old tables and manifests. | Retained only for disposable evidence; target registry exposes no retired operational roles. |
| Java `validation/*` | Read-only validation/comparison. | Target registry and readiness/state semantics; keep old-table readers fixture-only. |
| `performance/*`, `legacy_migration/*`, archived docs/tests | Disposable studies, migration evidence, or historical references. | No live destination; retain as historical/benchmark fixtures and label them. |

Training, recommendation, simulation, and validation do not get independent
database writers. Opening observations remain their existing operation-owned
local evidence and are not part of the daily raw/delta replacement tables.

## 9. Retired tables and features

| Current table/feature | Target or retirement decision |
| --- | --- |
| `stockguidance_imports` | Retire; receipt/revision/completion moves to `transaction_dates`. |
| `stockguidance_archive_files` | Retire as a permanent database catalog; use exact canonical paths and operation-owned prepared payloads. |
| `stockguidance_daily_source_files` | Retire file size/mtime invalidation; file edits never trigger replacement. |
| `stockguidance_daily_import_in_progress` | Retire; pending operation scope and raw unit completion replace it. |
| `stockguidance_row_quality` | Retire; source state and current structured repair metadata replace it. |
| `stockguidance_quality_revisions` | Retain only the necessary revision history inside structured repair metadata; no separate quality protocol. |
| `stockguidance_output_quality` | Retire; delta/z-score rows carry committed quality summary/provenance. |
| `stockguidance_quality_scans` | Retire; validators inspect current state/completion directly and never cache scan progress as readiness. |
| `stockguidance_data_verifications` and `_samples` | Retire the source-mixing verification workflow; incompatible imports are rejected. Read-only comparison remains a disposable/historical diagnostic. |
| `stockguidance_migration_receipts` and `_completion` | Retire from ordinary runtime; target receipt/completion fields and z-score session metadata are authoritative. Historical migration tools remain fixture-only. |
| File fingerprint/mtime-triggered refresh | Retired behavior. Normal updates inspect database completion first. |
| Permanent row dependency graph or parallel job framework | Explicitly retired design; durable pending scope is the only operation plan. |

Stages 1–9 defined and exercised the replacement without dropping production
tables. Stage 10 classifies historical commands and isolates them from target
initialization/runtime; no production table is dropped.

### Stage 10 cleanup boundary

The target registry has a target-only view containing `source`, `schema`,
`sessions`, `tickers`, `prices`, `deltas`, `data_states`, `pending_updates`,
`repair_metadata`, `zscore_series`, and `zscore_sessions`; each configured
window also has price, price-delta, and additive volume z-score tables. The
historical `tables()` serialization remains available only for migration
manifests and fixture identity. The target registry digest is version `3`, so a
database
created with the former mixed registry is incompatible until explicitly
reinitialized or migrated by a later operator stage.

The target initializer refuses a database containing any retired operational
table, and target readers reject a mixed schema before reading data. The old
initializer, source verification, z-score inspection, and daily-maintenance
context reject a versioned target marker rather than creating or querying old
tables. The maintained `preview_schema` command emits only target DDL.

Remaining retired-name references are classified as follows:

* `database/update_schema.py`, the old repositories, `market_data/reimport.py`,
  and the old daily-maintenance modules are legacy compatibility code used by
  existing regression fixtures; target dispatch and schema guards isolate them.
* `legacy_migration`, `performance`, Java `migration`, and Java `validation`
  references are disposable migration/benchmark inputs or read-only historical
  comparisons. They are not target runtime dependencies; their old artifacts
  remain for regression evidence.
* `market_data/archive.py`, canonical NDJSON readers, and repair quality
  models remain because source payload validation and exceptional repair
  metadata are still necessary; they do not recreate retired database tables.
* References in `docs/archive/` and the historical sections explicitly marked
  in the maintained storage/maintenance guides are historical documentation,
  not operator instructions for a target schema.

## 10. Acceptance invariants for later implementation

The replacement is acceptable only if all of these are true:

1. Exactly one source configuration row exists at `id=1`; an incompatible
   import is rejected before any row is written.
2. Every pending operation has one immutable, versioned scope persisted before
   clearing; the scope contains all affected raw, repair, delta, z-score,
   completion, and cache units.
3. Prepared source payloads are validated, operation-owned, hash-bound, and
   available before clearing. Resume cannot adopt an older pre-refresh file.
4. A false `affected_rows_cleared` flag makes rebuilding impossible. A true
   flag is committed only after all scoped clear work is complete.
5. Every calculation unit commits its values/states/provenance and completion
   evidence atomically. Presence after clearing means rebuilt; explicit
   non-ready results count as completed.
6. Missing, invalid, insufficient-history, zero-variance, and not-yet-available
   results are explicit states with NULL values where required, never zeros or
   infinite retries.
7. Recovery is idempotent before and after the clear-flag commit and preserves
   already committed rebuild units.
8. Writers serialize through `stockguidance_daily_maintenance`; new requests
   wait and plan from the state after the active operation.
9. Readers check pending state and overlap inside their read transaction;
   overlapping reads are blocked/distinguished and unrelated snapshots remain
   available.
10. Completed pending rows are removed. Raw/delta completion remains on
    `transaction_dates`; z-score completion remains in z-score session
    metadata; date presence alone is never readiness.
11. Integer IDs, readable source/calculation states, Decimal strings in JSON,
    stable ticker identity, current formulas, availability cutoffs,
    interpolation timing, and future-leakage propagation are preserved.
12. A correction invalidates affected dataset/prediction caches and identifies
    older model/artifact revisions without deleting or silently retraining them.

Stage 2 implementation: `raw_stock_data.state_id` is the measured selected
layout. The reproducible runner is
`tools/database_simplification_stage2.py`; its machine-readable output and
concise report belong under `output/database-simplification/benchmark`. The
disposable MySQL run recorded equal result digests, plans, storage/index sizes,
insert/update costs, and the required 20 repetitions. Stage 3 must use the
recorded raw-column selection.

Stage 3 implementation boundary: the selected registry adds `schema`,
`data_states`, `pending_updates`, and `repair_metadata` roles plus the
additive volume z-score preparation template while the legacy `tables()`
serialization remains unchanged for artifact/manifest fingerprints;
replacement consumers request the explicit replacement role set.
`initialize_simplified_schema` creates a fresh target schema with
`stockguidance_schema_version` metadata at target schema version `1` and
registry version `3`, the
raw-column `raw_stock_data.state_id` layout, UTC `DATETIME(6)` timestamps,
singleton checks, fixed state rows, session completion fields, repair
metadata, pending scopes, retained price/delta z-score fingerprints, and
per-window volume z-score tables. It refuses legacy or partial databases and
never performs additive migration. New
repositories call the same version/registry guard before reading or writing.

Python contracts are in `stockguidance.database.simplified_contract` and the
independent repositories are in
`stockguidance.database.repositories.simplified`. Java uses the immutable
`stockguidance.database.SimplifiedContracts` records and strict scope codec;
both sides use the same operation/unit/state names and Decimal-string JSON
convention. No temporary adapter or dual-write path was introduced. At the
Stage 3 boundary, existing production entrypoints still used the legacy
initializer and repositories; Stage 10 now prevents those legacy writers and
inspectors from operating against the versioned target schema. The historical
registry serialization remains available only for legacy manifests and
fixtures.

### Stage 6 raw rebuild boundary

`stockguidance.database.raw_rebuild` implements the raw portion of the later
rebuild-runner boundary without wiring it into a production entrypoint. Its
interfaces are:

```python
class PreparedPayloadReader(Protocol):
    def read(self, payload: PreparedPayload,
             session: TradingSession) -> ValidatedRawPayload: ...

class RawPayloadPreparer:
    def prepare(self, session: TradingSession, *, force_refresh: bool = False,
                available_at: datetime | None = None) -> PreparedPayload: ...

class RawRebuildRunner:
    def rebuild(self, operation: PendingUpdate,
                connection: WriteConnection) -> RebuildResult: ...
```

Normal preparation reads only the exact canonical
`data/market_summary/YYYY/YYYY-MM-DD.ndjson` path and copies its validated
bytes into an operation-owned staged payload. A forced preparation always calls
the injected source provider and creates a new staged artifact; it cannot adopt
the old canonical file. Adjusted sources cannot use the unadjusted canonical
path. Complete responses are checked through the existing archive decoder and
response-count/timestamp/Decimal validation; explicit no-trading responses use
an operation-owned response envelope. The staged descriptor binds both the
payload checksum and the existing validated-record identity.

The raw runner requires `affected_rows_cleared` and commits each raw session
unit in one transaction, including raw rows, source states, exceptional repair
metadata, receipt timing, and `raw_complete`/`raw_revision`. It creates rows
only for returned source keys and explicit provider placeholders. Known
placeholders are NULL-valued `missing` rows; no-trading and absent/unlisted
keys do not expand into a ticker/date Cartesian product. Invalid source rows
retain representable fragments plus their original JSON values in `bad`
metadata. Repair units reuse `linear-trading-session-v1`, use exchange-session
ordinals from the injected calendar, refuse interpolation across explicit
missing rows, and commit their effective values/state and endpoint metadata
atomically. A completed raw unit is skipped on resume by its completion and
payload revision; a completed repair revision is skipped by its persisted
metadata. Delta and z-score units remain deferred to Stage 7.

### Stage 7 numerical rebuild boundary

`stockguidance.database.numerical_rebuild.SimplifiedNumericalRebuildRunner`
composes the Stage 6 `RawRebuildRunner` and is the target-schema Python
`RebuildRunner` implementation. It rejects an uncleared operation, processes
all raw and repair units first, then processes delta sessions, and only then
processes z-score session units. A committed `delta_complete` flag or a
validated `zscore_sessions` row plus matching output-row count is completion
evidence on resume; completed units are preserved rather than deleted again.

Delta values use the existing `legacy_rows` formula kernel and retain the full
sixteen-column order. A delta transaction replaces only the planned current
ticker rows, writes the readable `calculation_status`, quality flags, gap
length, structured provenance JSON, and `delta_complete` together. A current
close can remain available while future-dependent label columns are NULL; the
row is complete with `not_yet_available` rather than being retried forever.
Missing, invalid, and insufficient-history inputs remain NULL where the
existing formula has no value. Ranking quality includes the actual eligible
following-session population.

Z-score values use the existing `calculate_zscores` kernel with the exact
price or price-delta session window. Price-delta inputs are read only after
their delta session completion and are checked against the raw close ratio.
Affected ticker rows are merged with unaffected rows in the target session;
one z-score transaction writes those rows and the existing completion
metadata. The six existing z-score status codes, float32 ready precision,
NULL non-ready values, dependency/availability digests, content fingerprint,
quality flags, gap length, and provenance remain authoritative. Raw source
availability and repair timing are carried into observations so strict
cutoffs and future-leakage quality policy continue to apply.

The matching Java surface is
`stockguidance.database.SimplifiedNumericalKernel` plus
`SimplifiedNumericalWriter`. It consumes `SimplifiedContracts.PendingUpdate`
work units, parameterizes values, validates registry identifiers through the
Java `TableNames` registry, and commits each delta/z-score unit atomically.
It is not wired into a supported maintenance entrypoint; that coordinated
cutover remains Stage 8.
