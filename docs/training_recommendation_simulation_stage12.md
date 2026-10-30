# Stage 12 acceptance and operator handoff

Stage 12 is the fixture-only acceptance path for the configurable training,
recommendation, simulation, saved-run, replay, and standalone preparation
contracts. It does not contact a provider, use the configured production
database, submit orders, or expose credentials.

## Deterministic fixture

`tools/training_recommendation_simulation_stage12.py` creates 124 XNYS sessions
from 2024-01-02 through 2024-06-28 and uses 60-plus sessions of feature
warm-up. The training entries are 2024-04-12 through 2024-04-16 and the ten
evaluation entries are 2024-04-23 through 2024-05-06. Ticker `GAP` has an
interior raw-observation gap on 2024-04-05. `LOWVOL` has complete observations
but fails the enabled 20-session average-volume lower bound. `AAA` and `BBB`
are selected in stable-ID order.

The rehearsal runs two small configured fits: raw-return/RMSE with standard
input and min-max output normalization, and cross-sectional return z-score with
pairwise ranking and normalization disabled. It reloads the raw artifact, uses
that v2 artifact for a validate-only recommendation with zero provider calls,
then uses the existing v1 raw-return simulator for both cash-rest schedules.
The v1 simulation boundary is intentional: the current historical simulator
contract still requires its fixed `FeatureSpec`; this stage does not add a new
v2 simulation model path.

## Copy/paste phases

Run from `C:\Projects\StockGuidance` with the configured project interpreter:

```powershell
$pythonExe = '.venv\Scripts\python.exe'
$stage12Out = 'output\stage12'

# 1. Read-only date/dependency preview.
& $pythonExe -m stockguidance.cli.preview_date_ranges --config examples\stage12\date-preview.json

# 2. Render read-only frozen-universe SQL. Rendering writes a script only.
& $pythonExe -m stockguidance.cli.select_universe --config examples\stage12\universe-selection.json --step plan --sql-output "$stage12Out\universe-plan.sql"
& $pythonExe -m stockguidance.cli.select_universe --config examples\stage12\universe-selection.json --step select --sql-output "$stage12Out\universe-select.sql"

# 3. Validate configured jobs without fitting or database access.
& $pythonExe -m stockguidance.cli.train_model --config examples\stage12\configured-training-raw-rmse.json --preview
& $pythonExe -m stockguidance.cli.train_model --config examples\stage12\configured-training-ranking.json --preview

# 4. Full offline cross-stage rehearsal. All generated files stay below this
#    disposable directory and the command never opens a database/provider.
& $pythonExe tools\training_recommendation_simulation_stage12.py --output-dir $stage12Out

# 5. Inspect the saved runs without database, network, Torch, or model calls.
& $pythonExe -m stockguidance.cli.runs --runs-root "$stage12Out\runs" list
& $pythonExe -m stockguidance.cli.runs --runs-root "$stage12Out\runs" list --operation simulation
```

The fixture command prints and saves `stage12-summary.json`. Its summary gives
the paired simulation parent and child run IDs. Open a returned ID with:

```powershell
& $pythonExe -m stockguidance.cli.runs --runs-root "$stage12Out\runs" open '<run-id>'
& $pythonExe -m stockguidance.cli.runs --runs-root "$stage12Out\runs" validate '<run-id>'
```

Replay and pinned rerun are explicit typed services rather than hidden viewer
actions. The fixture command invokes `replay_saved_simulation` and
`rerun_pinned_simulation`; it verifies that offline replay has identical marked
equity and that the rerun is a new child. Opening or selecting a saved run does
not invoke either operation.

## Standalone preparation and SQL debugging

Preparation is independent of training. On an operator-approved disposable
MySQL instance, use the existing sanitized example and keep the operation ID
returned by `create`:

```powershell
$prep = 'examples\stage3\volume-zscore-preparation.json'
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step plan
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step create
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step status --operation-id '<operation-id>'
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step select --operation-id '<operation-id>'
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step calculate --operation-id '<operation-id>'
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step render-sql --operation-id '<operation-id>' --sql-step select --sql-output "$stage12Out\volume-select.sql"
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step render-sql --operation-id '<operation-id>' --sql-step calculate --sql-output "$stage12Out\volume-calculate.sql"
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step apply --operation-id '<operation-id>'
& $pythonExe -m stockguidance.cli.prepare_database --config $prep --step validate --operation-id '<operation-id>'
```

The rendered files are database-interface scripts, not execution requests.
Review the bounded scope, resolved registry identifiers, result columns and
verification SELECTs first. `select` exports typed rows with their source
revision; `calculate` runs the reviewed Python population-z-score reference;
`apply` rechecks dependencies, takes the writer lock, upserts stable keys, and
updates readiness only after validation. If a process is interrupted, inspect
the manifest and rerun `resume` or the named bounded step; do not issue a
direct UPDATE that bypasses pending-operation and completion metadata.

For the equivalent SQL/code rehearsal, run the disposable MySQL tool:

```powershell
& $pythonExe tools\database_preparation_stage3_mysql.py --dbinfo-file replacement-dbinfo.id --prefix stockguidance_disposable_preparation_stage12_YYYYMMDD_01 --output-dir "$stage12Out\mysql-preparation"
```

It creates only explicitly prefixed disposable schemas, demonstrates source
revision/correction invalidation, interruption and resume, rerun, validation,
and EXPLAIN availability, then reports the comparison. It must not be pointed
at the configured production database. The Stage 3 SQL route is a complete
debug/rendering route where supplied; the reviewed code calculation remains
the semantic reference and the report must state which route was applied.

DDL/setup is explicit and has separate auto-commit behavior from transactional
data batches. Production operators must separately authorize schema setup,
backup/restore, capacity checks, maintenance locks, source quiescence, and
cutover. None of those operator-only steps are performed by Stage 12.

## Acceptance boundary

The checked-in integration test runs the same offline fixture and asserts
warm-up, interior-gap and filter evidence, artifact reload, raw/z-score target
and loss identities, validate-only/no-provider behavior, both schedules,
offline replay equality, and child rerun creation. MySQL behavior is covered
only by the disposable Stage 3 rehearsal; SQLite or the in-memory fixture is
not claimed as proof of MySQL transaction, DDL, or capacity behavior.
