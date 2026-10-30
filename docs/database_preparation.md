# Standalone database preparation and SQL debugging

Stage 3 adds a bounded preparation operation for volume z-score materializations.
It is separate from training, recommendation, simulation, and provider refresh.
The pure reference calculation is population rolling z-score over exchange
sessions, with the current value included in its declared window. Ready values
are stored as float32-compatible scores; unavailable values retain NULL and an
explicit status.

The target schema now has an additive `volume` preparation template and creates
`zscores_volume_<window>` beside the existing price and price-delta families.
The registry's historical `tables()` and `templates()` payloads are unchanged;
the selected additive template is exposed through `preparation_templates` and
is included in the target registry digest. Existing target databases are not
silently migrated: initialize a new disposable target or use the explicit
operator migration boundary when one is authorized.

## Operation lifecycle

Each attempt is stored below the configured `operations_root` (default
`database-preparations/<operation-id>/`) with an atomic `manifest.json`,
`inputs.json`, and `results.json`. The manifest records the sanitized request,
resolved dependency/write sessions, selected registry names, input/result
digests, and failure state. The input scope is `L + W - 1` in the more general
feature terminology; this operation has no model lookback, so its dependency
warm-up is `W - 1` sessions before the first materialized session.

The lifecycle is independently executable:

```text
plan -> create -> select/export -> calculate -> apply -> validate
                                      ^                 |
                                      +---- resume -----+
```

`select` accounts for every requested session/ticker key. A complete session
with no raw row becomes an explicit missing input; an incomplete session is
unverified. No unready value becomes zero. `calculate` is pure Python and the
JSON result is the reviewable intermediate format. `apply` reselects the exact
bounded source scope and rejects a changed input digest before registering any
write operation.

Apply uses the existing maintenance advisory lock and pending-operation table.
It clears only the recorded volume output/completion units, commits the clear
flag, writes stable-key upserts, validates completion metadata, and removes the
pending row only after the write is complete. If the process stops after the
clear flag, `resume` reuses the same result and finishes without duplicating
rows. Readers using the existing readiness guard see the pending scope while it
exists. No schema setup is implicit in any read or calculation step.

## Commands

The checked-in sanitized example is
[`examples/stage3/volume-zscore-preparation.json`](../examples/stage3/volume-zscore-preparation.json).
The database path is intentionally a fixture placeholder. Replace it only with
an explicitly authorized disposable target.

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step plan
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step create
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step status --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step select --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step calculate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step apply --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step validate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step resume --operation-id <operation-id>
```

`resume` is safe after an interruption and is also the recovery command for a
manifest whose pending operation has `affected_rows_cleared = 1`. A source or
table-registry change requires a new bounded export; it is not treated as an
exact rerun.

## SQL/interface debugging

Rendering is read-only: it writes only the requested `.sql` file and never opens
the database. It validates and quotes registry-selected identifiers and emits
no Python `%s` placeholders.

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step render-sql --operation-id <operation-id> --sql-step plan --sql-output output\stage3-plan.sql
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step render-sql --operation-id <operation-id> --sql-step select --sql-output output\stage3-select.sql
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step render-sql --operation-id <operation-id> --sql-step calculate --sql-output output\stage3-calculate.sql
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step render-sql --operation-id <operation-id> --sql-step apply --sql-output output\stage3-apply.sql
```

The `plan` script contains an `EXPLAIN` for the bounded cross-join/left-join
input selection. The `select` script exposes the exact columns, types, order,
dependency dates, and ticker scope. The `calculate` script is a read-only SQL
diagnostic using window functions; the Python calculation remains authoritative
for status precedence, float32 quantization, provenance digests, and result
review. The `apply` rendering supplies a CSV staging/import and stable-key
upsert template, but it deliberately says to use the lifecycle-safe CLI wrapper
for pending registration, clearing, completion, and recovery. Do not paste a
direct update outside that wrapper.

The verification route is:

1. run `EXPLAIN` and record plan shape, rows examined, and elapsed time;
2. export the bounded input rows and inspect `inputs.json`;
3. calculate in Python and review `results.json` status/value rows;
4. optionally execute the rendered SQL calculation in a disposable interface
   and compare keys, NULL/status values, and scores within float32 precision;
5. apply through the CLI wrapper and run `validate`.

The SQL and code routes use the same `(date_id, ticker_id, window)` keys,
population formula, dependency scope, status codes, NULL policy, and timing
contract. SQL is a debugging/calculation view, not a second hidden feature
algorithm. DDL is explicit and may auto-commit on MySQL; preparation data
transactions are bounded and resumable separately.

## Current rehearsal boundary

The checked-in tests use a disposable SQLite adapter for pure calculation,
custom registry rendering, source-change rejection, idempotent apply, and
crash/resume state transitions. SQLite does not establish MySQL lock or window
function behavior. The checked-in rehearsal runner uses an explicit disposable
database prefix, keeps credentials in memory, drops the created database in a
`finally` block, and emits only sanitized timing/EXPLAIN evidence:

```powershell
.venv\Scripts\python.exe tools\database_preparation_stage3_mysql.py --dbinfo-file replacement-dbinfo.id --prefix stockguidance_disposable_preparation_YYYYMMDD_01 --output-dir (Join-Path ([System.IO.Path]::GetTempPath()) 'stockguidance-stage3-mysql')
```

It performs target-schema setup, custom-registry selection, SQL/code parity,
source-change rejection, interruption after durable clear, resume, rerun, and
validation. No production configuration, provider, credential, or live
database is part of this workflow.
