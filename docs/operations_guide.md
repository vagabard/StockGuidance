# Operating StockGuidance

This guide describes the current Python commands and the proposed automatic
history workflow. Implementation prompts are in
[luna_maintenance_prompts.md](luna_maintenance_prompts.md). Those prompts have
**not been implemented** by adding this guide. Start with the Python workflow;
the Java sources are unfinished legacy prototypes.

## Recommended order

For an existing database, use **validate -> update/repair -> validate -> train
-> simulate**. The first validation records existing problems before a write;
the second checks the resulting data. Save both reports. A failed validation
does not automatically mean that an update can repair every finding.

For a new empty database, first establish source/history policy and explicitly
initialize the updater schema, then **update -> validate -> train -> simulate**.
Read-only validation never creates tables. An incompatible existing schema needs
inspection and a separate migration decision; `--initialize-schema` does not
convert it.

For routine operation after a clean baseline, use **update -> validate -> use an
existing model**, or **update -> validate -> retrain -> use the new model**.
Independent validation is useful as an audit even though the updater performs
shared reconciliation checks. Training and simulation do not update data.

Before either an initial update or a repair, back up the database and retained
source files using your normal backup procedure. Keep one writer per selected
database/data root; run audits against a consistent snapshot or while it is idle.

## Environment

Run from `C:\Projects\StockGuidance`. Use the executable selected by PyCharm for
this project. On 2026-10-16 the IDE reported Python 3.14.8 at:

```powershell
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $pythonExe -m stockguidance.cli.validate_database --help
& $pythonExe -m stockguidance.cli.update_market_data --help
& $pythonExe -m stockguidance.cli.train_model --help
& $pythonExe -m stockguidance.cli.simulate --help
```

Rediscover the interpreter if the SDK changes. The explicit `PYTHONPATH` supports
running this checkout without an editable installation. The repaired interpreter
passed these four help commands and module-presence checks for NumPy,
exchange-calendars, Torch and pytest. That is not a Torch training, CUDA, MySQL or
live-provider acceptance test. The IDE reported an unknown package manager, so
no package installation was attempted. Old Python-base workarounds in historical
refactoring handoffs should not be copied into the repaired environment.

## Three different date ranges

| Range | Meaning | How it should be used |
|---|---|---|
| Retained local history | Dates in local responses/manifests and database rows; these may start before current download access | Audit and preserve it, recording which observations are actually verified |
| Currently downloadable history | Account/request-specific provider coverage as of a particular check | Bound network requests and scheduled refreshes |
| Requested operation range | Dates selected for validation, updating, training or simulation | Default to appropriate discovered coverage; allow explicit date subsets |

The latest database row is an **observation**, not proof of completeness. The
update target is the latest completed exchange session whose daily data should
be published at the current clock, using the configured publication delay.
Weekends, holidays, early closes and provider delays matter. Scan for interior
gaps even if a later date is present.

Example: retained data starts in 2010, while today's provider entitlement starts
in 2021. Validation should still inspect the retained 2010-2020 archive. Updating
should preserve those years, use intact local evidence to repair imports where
possible, and fetch only within today's supported download range. An archive
gap that cannot be fetched must remain an explicit unresolved finding. It must
not trigger deletion, a fabricated replacement, or a claim of complete history.
Raw database rows alone do not prove source identity or original availability.

**Current limitation:** `cli/update_setup.py` and `cli/validation_setup.py` still
require either persisted source history or `--history-start` with
`--history-evidence`. They share a single lower boundary. They do not yet discover
the three ranges above. Prompts 1-3 replace this limitation while retaining the
history arguments as optional overrides. Omitting the arguments works today
only when matching history metadata already exists.

Provider access may roll forward or change with an account. A first successful
download, an empty response, a filename or `MIN(date)` cannot establish an
authoritative provider limit. Automatic discovery needs evidence; an unresolved
limit must be reported honestly. Offline validation must not contact the provider.

## Validate and update with the current commands

Use one data root, database configuration, source identity and adjustment policy
throughout. The examples use `adjusted`; select what matches your existing
source. `dbinfo.id` is an explicit JSON configuration; do not paste its contents
into prompts or reports. Set `POLYGON_API_KEY` through your existing configuration
if needed. No credential values belong in these examples.

Set policy values from your provider/account evidence. No live delay or
correction interval is inferred here:

```powershell
$publicationDelay = Read-Host 'Verified publication delay in minutes'
$publicationEvidence = Read-Host 'Publication policy provenance'
$recentWindowDays = Read-Host 'Recent correction window in days'
$recentRefreshHours = Read-Host 'Recent refresh interval in hours'
$archiveRefreshDays = Read-Host 'Archive refresh interval in days'
$correctionEvidence = Read-Host 'Correction policy provenance'

$commonArgs = @('--data-root', 'data', '--dbinfo-file', 'dbinfo.id', '--adjustment', 'adjusted')
$publicationArgs = @('--publication-delay-minutes', $publicationDelay, '--publication-evidence', $publicationEvidence)
$correctionArgs = @('--recent-window-days', $recentWindowDays, '--recent-refresh-hours', $recentRefreshHours,
    '--archive-refresh-days', $archiveRefreshDays, '--correction-evidence', $correctionEvidence)
$historyArgs = @()
New-Item -ItemType Directory -Force -Path 'output' | Out-Null
```

For a current checkout without history metadata, set `$historyArgs` once from
inspected source evidence. This is a temporary prerequisite, not automatic
discovery, and the current single boundary cannot fully represent the retained
archive/provider distinction. Avoid a broad update if that distinction applies;
use explicit fetchable bounds until prompts 1-3 are complete.

```powershell
$historyArgs = @('--history-start', '<evidenced-first-supported-session>',
    '--history-evidence', '<non-secret provenance>')
```

Replace the bracketed strings before use. If history is already persisted, leave
`$historyArgs` empty. A conflicting explicit boundary fails today. Validation
uses an explicit boundary only in memory; the updater persists it.

```powershell
& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs --format json > output/validation-before.json
$validationExit = $LASTEXITCODE

& $pythonExe -m stockguidance.cli.update_market_data @commonArgs @historyArgs @publicationArgs @correctionArgs
$updateExit = $LASTEXITCODE

& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs --format json > output/validation-after.json
$validationExit = $LASTEXITCODE
```

These are separate steps: inspect the report and exit code between commands.
An existing database with integrity/schema findings needs review before writes.
For a new empty database only, add `--initialize-schema` to its first update.
Add `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` to narrow the operation;
bounds are inclusive. Omitted end dates use the current clock and calendar/
publication planning. A validator without a publication policy reports unverified
availability and is not a readiness pass.

Validation exits: `0` verified required coverage, `2` missing/invalid/incomplete/
unverified coverage or no available session, `1` setup/inspection failure.
Argument errors also use `2`. Check the findings and feature scope as well as
the exit code: a calendar-only range can pass without any training samples.

To check the exact model input layout, add both arguments using **real IDs in
the intended order**:

```powershell
& $pythonExe -m stockguidance.cli.validate_database @commonArgs @historyArgs @publicationArgs `
  --start-date '<first-entry-date>' --end-date '<last-entry-date>' `
  --ticker-ids '<ordered,real,ticker,ids>' --lookback-sessions 20 --format json > output/model-readiness.json
```

Feature checks also enforce historical receipt availability. Ordinary raw/derived
coverage success does not guarantee model readiness. `entry_open` requires its
own feature specification and original opening receipts; start with
`previous_close` unless those receipts exist.

For an automatic repair plan that has no manual-review items, substitute this
for the ordinary update command:

```powershell
& $pythonExe -m stockguidance.cli.update_market_data @commonArgs @historyArgs @publicationArgs @correctionArgs `
  --repair-plan output/validation-before.json
```

Use a recent report from the same source and scope. The updater rejects plans
requiring schema/integrity/policy review; do not remove those findings merely to
make the plan run. Explicit date bounds must match the plan. Always validate
again afterward. See [database_validation.md](database_validation.md) for the
finding taxonomy and [market_data_updating.md](market_data_updating.md) for recovery.

## Train on a date subset

Keep the full archive; choose training entry dates in the job JSON. Do not delete
rows or renumber database IDs. Start with the complete configuration example in
[model_training.md](model_training.md), save it as `training-job.json`, and edit:

| Field | Choice |
|---|---|
| `source`, `data_root`, `database_config` | Your verified source and storage |
| `features.ticker_ids` | Explicit ordered real IDs, fixed without future survival information |
| `features.lookback_sessions` | Number of preceding exchange sessions per sample |
| `features.signal_mode` | `previous_close` for the initial workflow |
| `start_date`, `end_date` | Inclusive **entry-session** training subset |
| `training_cutoff` | Aware timestamp when fitting is allowed to know its mature labels |
| Publication fields | Your evidenced delay and provenance |
| `evaluation` | A strictly later holdout, or `null` initially |

The following dates are an illustrative split, not evidence that this checkout
has usable observations for them:

```json
{
  "start_date": "2022-01-03",
  "end_date": "2023-12-28",
  "training_cutoff": "2023-12-30T12:00:00+00:00",
  "evaluation": null
}
```

This is a **fragment to merge into the full example**, not a standalone job.
The last training entry needs its next-session close and publication before the
cutoff. With a 20-session lookback, retain 20 sessions before the first entry,
and at least one exit session after the last entry. Each selected ticker must
have the required observations; missing values are not zero-filled. The first
usable entry can be later than the local archive minimum.

```powershell
& $pythonExe -m stockguidance.cli.train_model --config training-job.json `
  --artifact output/subset.sgmodel --checkpoint output/subset-training.pt > output/training-result.json
$trainingExit = $LASTEXITCODE
```

Inspect `training_samples`, `excluded_training`, the artifact reference and
metrics. A small CPU trial should establish readiness before a long fit. The
checkpoint supports a compatible continuation; use a new fit for an independent
experiment or changed split. Training MAE is an in-sample statistic.

### Historical downloads and original decision times

**A successful download today does not currently make old dates trainable.**
`datasets/building.py` preserves each sample's original feature-decision cutoff,
and the verified repository checks actual receipt evidence. Prices fetched in
2026 for a 2022 sample cannot prove that those exact observations were known in
2022. Moving `training_cutoff` to today only matures labels; it does not move the
feature decision to today. Fabricating earlier receipt times would hide this issue.

For strict historical evaluation, use original contemporaneous evidence and
preserved revisions. Where it is missing, samples remain excluded. If you want
research on a backfilled archive, prompt 4 specifies a separate, explicitly
labeled retrospective mode that assumes historical publication availability.
It is proposed functionality: **there is no such CLI switch today**. It must
retain real receipt times, reject future-session inputs, and label correction,
adjustment and universe hindsight limitations. It cannot claim a point-in-time
replay. `entry_open` needs separate evidence and is excluded from that proposal.

## Simulate the resulting model

For a fixed model, use a **later** period: for example, train on the illustrative
2022-2023 subset, then simulate entries from 2024-01-02 through 2024-06-28. The
model's information cutoff must precede the first simulation open and its final
training exit must precede the first simulated entry date. Save a complete
configuration from [simulation.md](simulation.md) as `simulation.json` and set:

- The same source, data paths, ordered tickers, lookback, feature order and signal
  mode as the artifact.
- `start_date` and `end_date` to the later entry range, with an explicit allocation
  `anchor_date` and either `alternate_sessions` or `staggered_halves` policy.
- `initial_capital` and `fee_per_fill` as decimal strings, plus `top_k` and the
  evidenced publication policy.
- `execution_audit_cutoff` after the receipt of the prices used for fills/marks;
  it never authorizes future signal inputs. Set `retraining` to `null`.

Use the **reported artifact identity**, not a file hash from `Get-FileHash`.
The ID hashes the canonical manifest payload and weights identity, not the
entire `.sgmodel` archive bytes.

```powershell
$trainingResult = Get-Content -Raw output/training-result.json | ConvertFrom-Json
if ($trainingResult.successful -ne $true) { throw 'Training did not produce a successful artifact' }
& $pythonExe -m stockguidance.cli.simulate --config simulation.json --model-policy fixed `
  --artifact $trainingResult.artifact.path --artifact-id $trainingResult.artifact.artifact_id > output/simulation-result.json
$simulationExit = $LASTEXITCODE
```

Simulation exits `0` for a successful complete run, `2` for invalid configuration
or an incomplete completed run, and `1` for a required-stage failure. Inspect
skips, missing fills, missing/stale marks, fees, open positions and benchmark
completeness alongside returns. Expected end-period open positions are retained.
Extend the simulated session range if you want later exits, accounting for any
new entries scheduled in that range; there is no automatic forced liquidation.

If you mean "train on a subset and simulate **those same dates**," a model fitted
using those dates would have hindsight. The fixed-model command rejects that
chronology. Use a later test range, or use `--model-policy chronological_retrain`
with the documented `retraining` configuration and `--artifacts-directory`.
That workflow fits only mature history available at each scheduled decision.
It starts with pre-period history; the global training range does not authorize
future outcomes. Repeatedly tuning against the same test range makes it a
development holdout; reserve another later period for final evaluation.

Both allocation policies buy at T's close and sell at the next trading close.
Sale proceeds rest until the next exchange session. Staggered halves have
independent balances; missed decisions never shift the anchored schedule.

## What belongs in Java?

No function has yet been shown to require a Java port. Database latency,
round trips, parsing, materialization and Python loops need separate measurement.
NumPy and Torch already execute substantial work in native code. Compare a
correct batched/vectorized Python implementation with Java before choosing.

| Area | Recommendation |
|---|---|
| SQL range scans and raw-row integrity | First improve query bounds, batching and query plans; consider a streaming JDBC reader if measured CPU/allocation remains significant |
| Hashing, response parsing, row reconciliation | Candidate for a Java worker after Python/Java canonicalization and report parity tests |
| Bulk import and deterministic legacy-derived rebuild | Later candidate, requiring identical transactions, receipts, dependency invalidation and recovery |
| Repeated simulation over frozen scores | Possible later candidate if the event loop dominates; Python still owns inference/training and the semantic reference |
| Torch models, fitting and feature specifications | Keep in Python initially; do not rebuild the ML stack to optimize database work |
| Provider/history policy and exchange chronology | Keep one authoritative contract; export explicit session/policy data rather than maintaining divergent rules |

The current Java files need correctness work before any speed comparison:

- `database/DatabaseRowsToTensors.java` reads legacy derived targets, orders by
  numeric IDs, builds the full dataset in memory, uses a global `missing_dates`
  filter, and reads nullable SQL numbers without `wasNull()`. Its shape branches
  currently produce the same flattened layout.
- `simulator/SimulationEngine.java` loads an unbounded upper date range and
  calculates `close / open` ratios. It lacks the current evidence/portfolio
  contract needed for close-to-next-close evaluation.
- `simulator/StockRecommenderFromSavedScores.java` returns `index + 1` rather
  than the actual ticker ID and does not enforce the requested top-K limit in
  its result loop.
- `database/DbInfo.java` and `util/JsonUtils.java` parse JSON with regular
  expressions. `DbConnect.java` searches parent directories for configuration,
  whereas current Python uses an explicit selected JSON file.
- The inspected Java tree has a JDBC jar and a small load test, but no discovered
  Maven/Gradle build or equivalent current updater/validator CLI.

These observations are source review findings, not Java runtime test results.
Prompts 6-9 establish measurements, a build, parity fixtures and conditional
ports. A Java read-only validator is the first sensible implementation candidate;
keep it opt-in until it matches the Python reports and improves measured results.

## Completion checklist

After the prompts are implemented, a normal run should discover retained history
and current published coverage without history arguments, preserve older local
data outside current provider access, report unrecoverable gaps explicitly, and
support explicit subsets. Record the resolved bounds, provenance, data revision,
training exclusions, artifact ID and simulation configuration with each experiment.
Never describe a stage as implemented solely because its prompt or guide exists.
