# Saved-run foundation

Stage 1 adds the versioned `stockguidance-run-1` saved-run contract. It is a
filesystem-only service: reading, listing, validating, and inspecting a run do
not open a database, contact a provider, import Torch, or execute a model.

## Directory layout

The default root is `<project>/runs`. Pass `runs_root` to
`RunDirectoryService` or `--runs-root` to the inspection CLI to use another
root. Runtime runs are ignored by Git.

Each run has a collision-free directory containing:

```text
<runs-root>/<run-id>/
  manifest.json
  artifacts/
  checkpoints/
  reports/
```

`manifest.json` is written and replaced atomically. Its original settings are
sanitized at creation; credential-shaped fields are replaced with
`[REDACTED]`. Resolved settings are a separate field and may be added while a
run is pending or running. No unrelated dirty-tree files or credential file
contents are copied into a manifest.

The manifest reserves identity, resolution, universe, specifications,
provenance, artifact, prediction, eligibility, exclusion, metric, report,
trade, equity, and benchmark sections for later stages. Empty sections mean
that work has not populated them; they are not successful placeholder results.

## Recommendation runs

Stage 9 recommendation attempts use the same lifecycle and save both success
and failure. A completed recommendation stores its external model reference,
artifact-owned specifications, refresh/data provenance, all ordered scores
before top-n filtering, all eligibility evidence and exclusions, aggregate
score metrics, and `reports/recommendation.json`. A failed attempt stores a
generic stage/code failure record without raw provider, database, or
credential text. The saved run is inspectable with the database-free commands
below and can be validated without loading a model or opening a database.

`validate-only` recommendation runs record that mode and the verified data
revision. They use read-only readiness checks: no provider request, update
operation, schema migration, or database write is part of the validate-only
path.

## Lifecycle

The process state transitions are:

```text
pending -> running -> completed
pending ----------------> failed
running -----------------> failed
```

An incomplete simulation report is represented separately by
`report_state="incomplete"` and `incomplete_report` while the process itself
can still be `completed`. A process failure uses `status="failed"` and a
sanitized failure record. Parent runs are immutable; replay, rerun, or rescore
children store `parent_run_id` and never rewrite the parent.

Local artifacts and checkpoints are outside the manifest and are recorded by a
SHA256 digest plus a relative path. External artifacts require an explicit
external reference and digest. `validate` checks local file existence and
content identity.

## Python API

```python
from stockguidance.runs import RunConfig, RunDirectoryService, RunOperation

service = RunDirectoryService(runs_root="runs")
handle = service.create(
    RunConfig(RunOperation.TRAINING, {"epochs": 3, "database_config": "fixture-dbinfo.id"})
)
service.start(handle.run_id)
service.update_sections(
    handle.run_id,
    resolved_configuration={"resolved": "stored separately"},
    sections={"identity": {"source_revision": "fixture-1"}},
)
service.complete(handle.run_id)

manifest = service.validate(handle.run_id)
summaries = service.list_runs()
```

`RunOperation` currently names `training`, `recommendation`, `simulation`,
`replay`, `rerun`, and `rescore`. Later workflows are responsible for filling
their typed sections and saving their reports, predictions, or execution
evidence through this foundation.

## Database-free inspection commands

These commands only inspect the selected filesystem root:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs list
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs read <run-id>
.venv\Scripts\python.exe -m stockguidance.cli.runs --runs-root runs validate <run-id>
```

The CLI emits a generic failure response for corrupt, incomplete, unavailable,
or incompatible manifests so raw exception text is not exposed. It does not
repair a run, migrate a schema, refresh data, or rerun a workflow.
