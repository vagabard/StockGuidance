# Python frontend integration

The supported interfaces are the typed package frontends below. Root scripts,
legacy packages, and viewer shims have been retired; they do not provide
compatibility parsing, old artifact fallback, or controller-directory discovery.
The retirement history is recorded in [the retirement manifest](stage9_retirement.md).
Original sources remain in Git history and the verified local recovery archive.
User data/models are unchanged.

Use PyCharm's configured Python and package manager. All supported model inputs
are `previous_close` or the distinct Stage 10 `entry_open` specification; every
target is raw `close(T+1) / close(T) - 1`. Decisions
precede the entry close. Same-close execution is an explicit simulation
assumption. A recommendation produces a plan and records no fills.

## Environment

```powershell
$stockguidancePython = 'C:\Projects\StockGuidance\.venv\Scripts\python.exe'
& $stockguidancePython -m pip install -e '.[dev,training,database]'
```

Discover the interpreter through PyCharm before Python commands. This checkout's
configured Python 3.13.9 base installation is missing. The process-only matching
standard-library workaround is recorded in `REFACTORING_PROMPTS.md`; ordinary
commands require that workaround or repair of the configured base installation.
No replacement interpreter is selected by the project.

Database configuration is one explicitly named JSON file with `host`, `database`,
`user`, `password`, integer `port`, and `driver` (`auto`, `mysqlconnector`, or
`pymysql`). An equal `schema` alias is accepted; conflicting names fail. Files are
not searched in parent directories. Old KEY=VALUE/credential environment-default
parsing is superseded. The connector loads a driver only when invoked and leaves
transaction completion to the repository. Credentials are excluded from reprs,
models, reports and batch metadata.

The retained hardcoded Polygon key is `DEFAULT_API_KEY` in `polygon_fetch.py`.
Its value is intentionally absent here. `POLYGON_API_KEY` overrides it; resolving
credentials happens only when a fetch is needed. Move this constant to external
configuration eventually. No credential migration is required to use the new
workflows. The retained module is packaged for installed use outside the checkout,
is imported lazily only for provider access, and is not a supported command.

## Discover history and update

The updater inventories source-bound manifests and local observations before
planning. The earliest database row is not proof of continuous coverage, and a
local archive may predate current provider access. An explicit, evidenced
source-bound history override remains available, but current network requests
also require fresh verified provider capability evidence.

```powershell
& $stockguidancePython -m stockguidance.cli.update_market_data --data-root data --dbinfo-file dbinfo.id --end-date '<last-requested-date>' --publication-delay-minutes '<evidenced delay>' --publication-evidence '<publication evidence>' --recent-window-days '<correction window>' --recent-refresh-hours '<recent recheck interval>' --archive-refresh-days '<archive recheck interval>' --correction-evidence '<correction evidence>'
```

Source-bound history metadata persists in `data/history`, keyed by the complete
non-secret source/request identity, only when an explicit new boundary is
provided. Later updates can omit the boundary flags. Unknown or stale provider
capability blocks network refreshes while preserving local archives and exposing
the unresolved scope. Only an explicit `--initialize-schema` creates missing
compatible updater tables; it never converts existing tables. Full-coverage
reconciliation detects leading and interior gaps, interrupted imports and due
corrections.

## Read-only validation and explicit repair

```powershell
& $stockguidancePython -m stockguidance.cli.validate_database --data-root data --dbinfo-file dbinfo.id --publication-delay-minutes '<evidenced delay>' --publication-evidence '<publication evidence>' --ticker-ids '17,3' --lookback-sessions 3 --format json > validation.json
& $stockguidancePython -m stockguidance.cli.update_market_data --data-root data --dbinfo-file dbinfo.id --repair-plan validation.json --publication-delay-minutes '<evidenced delay>' --publication-evidence '<publication evidence>' --recent-window-days '<correction window>' --recent-refresh-hours '<recent recheck interval>' --archive-refresh-days '<archive recheck interval>' --correction-evidence '<correction evidence>'
```

Inspect `findings`, assessed sessions/checks and `repair_plan` before repair.
Missing, invalid, incomplete and provider-unverified evidence are distinct from
expected boundary absence and unavailable outcomes. Validation never writes,
downloads, deletes or initializes a schema. Manual schema/integrity proposals
require review and cannot be executed automatically by the updater.

## Configure and populate rolling z-scores

The schema/z-score rollout is explicit and ordered: select the mandatory
registry or an external `table_names_file`, preview the names and DDL, initialize
compatible updater tables and ingest data, preview the requested windows,
initialize/populate the optional z-score tables, then validate with the same
`--zscore-windows` selection. The complete operator sequence, including the
required evidence arguments, is in [the operations guide](operations_guide.md).

For two windows, the supported frontends are:

```powershell
& $stockguidancePython -m stockguidance.cli.preview_schema --windows 20,60
& $stockguidancePython -m stockguidance.cli.update_zscores --data-root data --dbinfo-file dbinfo.id --adjustment adjusted --windows 20,60 --start-date '<first-session>' --end-date '<last-session>' --publication-delay-minutes '<documented delay>' --publication-evidence '<publication provenance>' --initialize-schema
& $stockguidancePython -m stockguidance.cli.validate_database --data-root data --dbinfo-file dbinfo.id --adjustment adjusted --start-date '<first-session>' --end-date '<last-session>' --publication-delay-minutes '<documented delay>' --publication-evidence '<publication provenance>' --zscore-windows 20,60 --format json > output/validation-zscores.json
```

Use `--table-names-file '<absolute-selected-registry>'` with
`preview_schema` when an external registry is selected. A relative
`table_names_file` in `dbinfo.id` is resolved relative to that file; omitting it
uses the packaged resource. Missing or invalid selected files fail without a
fallback. The updater requires an already compatible six-table schema and does
not call providers. Re-run it explicitly after a dependency correction; stale
materializations remain rejected until rebuilt. Java's packaged registry is a
build-time copy of the canonical Python JSON, while Python-launched Java gets
the effective registry explicitly. Use one external registry for separately
installed distributions whose packaged files may differ.

## Train and rebuild

The full [training JSON example](model_training.md) records source, exact ordered
ticker/feature specifications, fitting cutoffs and optional chronological
holdout. Mature outcomes alone are eligible. Model output is unrestricted raw
return. Old model directories, NPZ caches, datasets and saved-score CSVs cannot
be reused. New samples are constructed on demand from verified closes; train new
artifacts instead of relabeling or converting old files. User data/model
directories are not deleted.

```powershell
& $stockguidancePython -m stockguidance.cli.train_model --config training-job.json --artifact output/model.sgmodel
& $stockguidancePython -m stockguidance.cli.inspect_features --config training-job.json --sample-count 5 --seed 31
& $stockguidancePython -m stockguidance.cli.inspect_features --config training-job.json --kind correlations
```

Feature inspection retains excluded samples, exact decision cutoffs and feature
identities. Seeded random selections are sorted chronologically. Correlations use
complete mature close-to-close targets and report undefined coefficients as
null; they never zero-fill missing prices. These commands are read-only.

## Recommend with either model policy

Use the [recommendation JSON schema](recommendations.md). `current` requires the
explicit existing artifact's SHA256 identity. `retrain` takes a historical
training configuration and uses the returned artifact. Both refresh coverage,
validate readiness, use the same previous-session eligibility and rank identically.

```powershell
& $stockguidancePython -m stockguidance.cli.recommend --config recommendation.json --policy current --artifact output/model.sgmodel --artifact-id '<artifact identity from training>' --portfolio-state portfolio.json
& $stockguidancePython -m stockguidance.cli.recommend --config recommendation-retrain.json --policy retrain --artifact output/retrained.sgmodel --portfolio-state portfolio.json
```

For initial-anchor-only planning, replace `--portfolio-state` with actual
`--capital '1000.00'`. Later decisions require the full actual snapshot. Omit both
to obtain ranked suggestions without a cash-rest claim. Email is separate:
`stockguidance.reporting.email.recommendation_draft` creates a draft;
`send_draft` requires an explicitly supplied transport. No frontend sends mail.

## Evaluate both allocation schedules

In [simulation JSON](simulation.md), set `allocation.policy` to
`alternate_sessions` or `staggered_halves`, with an explicit `anchor_date`.
The latter splits capital once and retains independent A/B cash, positions,
resting proceeds and gains. Both schedules prevent sale-close proceeds reuse.

```powershell
& $stockguidancePython -m stockguidance.cli.simulate --config alternate-fixed.json --model-policy fixed --artifact output/pre-period.sgmodel --artifact-id '<artifact identity>'
& $stockguidancePython -m stockguidance.cli.simulate --config staggered-fixed.json --model-policy fixed --artifact output/pre-period.sgmodel --artifact-id '<artifact identity>'
& $stockguidancePython -m stockguidance.cli.simulate --config alternate-retrain.json --model-policy chronological_retrain --artifacts-directory output/alternate-models
& $stockguidancePython -m stockguidance.cli.incremental --config staggered-retrain.json --model-policy chronological_retrain --artifacts-directory output/staggered-models
```

Fixed artifacts must precede the evaluation period. Chronological retraining
uses only mature outcomes at each scheduled fitting cutoff and retains anchored
cadence through skips. `incremental` enforces `chronological_retrain`; it uses the
same ledger and never concatenates or rescales unrelated simulations.
Missing fills/marks and open end positions remain visible. Historical decisions
cannot use data downloaded later; latest manifests cannot recover overwritten
historical revisions.

## Explicit batch plans

`stockguidance.workflows.batch.run_batch(BatchPlan(...))` is the typed API.
Each step executes an application workflow directly; no application constructs
CLI arguments or reads controller directories. The entire JSON plan and
dependencies are validated before the first step. Reports are atomic, paths are
explicit, and every failure or incomplete simulation stops subsequent steps.
An artifact dependency uses the actual reference returned by its producing step.
Relative paths resolve from the working directory, like individual frontends.

```json
{
  "schema": "stockguidance-batch-1",
  "steps": [
    {
      "name": "fit",
      "kind": "train",
      "config": "training-job.json",
      "report": "output/training.json",
      "options": {
        "artifact": "output/model.sgmodel",
        "checkpoint": null,
        "resume": null
      }
    },
    {
      "name": "select",
      "kind": "recommend",
      "config": "recommendation.json",
      "report": "output/recommendation.json",
      "options": {
        "policy": "current",
        "artifact": {"from_step": "fit"},
        "artifact_kind": "model",
        "portfolio_state": null,
        "capital": null,
        "fee_per_fill": "0.00"
      }
    }
  ]
}
```

```powershell
& $stockguidancePython -m stockguidance.cli.batch --config batch.json
```

For a retraining recommendation, use `policy: "retrain"`, an explicit artifact
output path string, and a configuration with `retraining`. Simulation steps use
`kind: "simulate"` with options `model_policy`, `artifact`, `artifact_kind`.
Fixed `artifact` is `{ "path": "pre-period.sgmodel", "artifact_id": "<digest>" }`
or an earlier `from_step`; chronological retraining takes an explicit output
directory string. `fixed` still rejects a just-trained in-period artifact.

## Reports and viewers

Current reports identify `stockguidance-training-1`,
`stockguidance-recommendation-1` and `stockguidance-simulation-1`.
Choose explicit files; no directory, checkpoint or saved-score fallback exists.

```powershell
& $stockguidancePython -m stockguidance.cli.reports summary output/simulation.json
& $stockguidancePython -m stockguidance.cli.reports compare output/alternate.json output/staggered.json
& $stockguidancePython -m stockguidance.cli.reports artifact --artifact output/model.sgmodel --artifact-id '<artifact identity>'
& $stockguidancePython -m streamlit run src/stockguidance/viewers/reports.py
& $stockguidancePython -m streamlit run src/stockguidance/viewers/artifacts.py
```

The optional `viewers` dependency supplies Streamlit. UI imports are lazy and
viewers require explicit file paths. Comparisons require identical source,
feature/target/calendar, evaluation period, capital, execution, eligibility and
selection specifications; model policy and allocation policy are shown for each
run. Total return and drawdown use total marked equity, including cash and
resting proceeds. Incomplete/stale marks remain explicit. Training MAE retains
its in-sample label and is not presented as unbiased evaluation.

Exit 0 means successful completion; 1 means required execution/storage/reporting
failure; 2 means invalid arguments/configuration or an incomplete simulation.
Update/validation additionally use 2 for incomplete/unverified coverage.
Report readers use 1 for invalid/unavailable/incompatible report inputs.
External exception details are excluded from CLI errors.

## Checks and scope

```powershell
& $stockguidancePython -m pytest tests -q --tb=short
& $stockguidancePython -m mypy
git diff --check
git diff --cached --check
```

Fixtures use fake providers, temporary SQLite/fake MySQL adapters and tiny CPU
models. They do not mutate a live database, download production history, submit
trades or send mail. Native MySQL/Polygon deployment, CUDA and production-scale
performance are outside fixture verification. Java sources are unchanged. Their
legacy saved-score/CSV consumers need explicit format coordination before using
the new Python artifacts/reports; Python does not continue emitting the old
intraday formats for them. The completed [Stage 10 opening-price extension](entry_open.md)
adds full-universe first-regular-minute opening inputs, actual-receipt readiness,
distinct artifacts, read-only validation and matched chronological comparisons.
It does not implement full intraday ingestion or change holding/cash-rest rules.
