# Stage 4: frozen training-universe selection

Stage 4 adds a typed selector for the ordered training universe. It consumes the
resolved Stage 2 membership/dependency sessions and a read-only
`UniverseSourceData` snapshot. It does not build a model, contact a provider, or
create/migrate a database schema.

## Selection contract

`UniverseSelectionRequest.from_dependency_plan(...)` derives the raw sessions
needed for training membership, feature/target warm-up, mature training labels,
and enabled filters. The evaluation plan is not included. A volatility window of
`W` sessions requests `W + 1` closes because the first close is the predecessor
for the first close-to-close return. Average volume requests exactly `W` sessions.

The default selection mode sorts stable ticker IDs numerically and stores their
symbol labels separately. An explicit `ordered_ticker_ids` tuple is allowed for a
controlled experiment only after the same complete-coverage and filter checks.
IDs remain identities; they are not tensor offsets or descriptors.

Whole-session evidence is handled before ticker selection:

- `COMPLETE` permits evaluation of individual ticker rows.
- `MISSING` is recorded as a verified-absence exclusion for every candidate.
- `INCOMPLETE`, `INCONSISTENT`, `UNVERIFIED`, and `NOT_YET_AVAILABLE` block the
  selection instead of silently shrinking the universe.

Each ticker receives all applicable exclusion reasons. Enabled filters are
calculated only from training information at the request's filter cutoff:

- average volume is the arithmetic mean of shares over the exact trailing
  session window;
- volatility is the population standard deviation of fractional close-to-close
  returns and is not annualized;
- `legacy_transactions` is separately named and does not mean volume.

Bounds are inclusive and every enabled filter requires at least one bound.
Disabled filters set both optional read flags to false and do not add optional
dependency sessions.

## Python workflow

The database adapter is read-only and returns typed source evidence:

```python
from stockguidance.database.repositories.universe import MySQLUniverseRepository
from stockguidance.universe import UniverseSelectionRequest, select_universe

request = UniverseSelectionRequest.from_dependency_plan(
    resolved_plan,
    exchange_calendar,
    filters=filters,
)
source_data = MySQLUniverseRepository(source_config, connect).read(request.read_request())
selection = select_universe(request, source_data)

print(selection.ordered_ticker_ids)
print(selection.ordered_ticker_labels)
```

The source adapter checks simplified raw completion/revision evidence and reads
only requested optional columns. `select_universe` raises with the complete
empty result attached when no ticker passes; callers can persist that result as
a useful failed-run explanation before marking the Stage 1 run failed.

Use `persist_universe_evidence(run_service, run_id, selection)` to populate the
existing Stage 1 `universe` section and the `results.exclusions` field. It does
not create a second run store. For an empty result or blocked whole-session
coverage, use `persist_universe_failure_evidence(...)` before the caller records
the failed Stage 1 run.

## SQL debugging route

SQL rendering is read-only. It exports ticker labels, whole-session completion
evidence, bounded raw rows, and verification SELECTs. It does not claim a
SQL-only implementation of the selection formulas: Python's typed selector is
the reference calculation and the SQL route is for inspection/export.

Render SQL without opening a database:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step plan --sql-output output\stage4-universe-plan.sql
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step select --sql-output output\stage4-universe-select.sql
.venv\Scripts\python.exe -m stockguidance.cli.select_universe --config examples\stage4\universe-selection-sql.json --step validate --sql-output output\stage4-universe-verify.sql
```

Review the resolved registry names and the bounded session/ticker scope before
executing any exported SQL in a disposable database interface. The scripts
contain no credentials and do not execute when rendered. Production execution,
if later authorized, must use the current simplified completion and provenance
rules; direct writes are not part of Stage 4.
