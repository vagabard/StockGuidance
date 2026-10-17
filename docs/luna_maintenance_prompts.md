# Luna 5.6 maintenance prompts

These are implementation tasks for the model you select as Luna 5.6. Use one
numbered prompt per conversation, in order, with the shared preamble below.
Prompts 4 and 7-9 are conditional as described. Stages 1-10 are marked complete
in the handoffs below. Read [operations_guide.md](operations_guide.md) for current commands
and [database_validation.md](database_validation.md) for the existing audit contract.

This brief updates the history requirement in `REFACTORING_PROMPTS.md`: retained
local history may predate current provider access. They must have separate bounds.
The history arguments stay optional overrides; users should normally get automatic
local/current discovery. Preserve the earlier brief's trading, timing, evidence,
cash-rest and modularity requirements except for changes explicitly specified here.

## Shared preamble: include with every prompt

```text
Work in C:\Projects\StockGuidance. Read applicable AGENTS.md instructions,
docs/luna_maintenance_prompts.md, docs/operations_guide.md, and the files named
in this stage. Implement only the selected stage and its necessary tests/docs.
Inspect git status first and preserve unrelated user edits. Do not commit.
Use the configured PyCharm interpreter; consult the python-tools skill and
get_python_environment before Python commands. Do not revive historical
PYTHONHOME/base-installation workarounds. Do not install packages blindly.

Use small typed modules under src/stockguidance, thin CLI frontends, explicit
source/adjustment/interval identity, and injected clocks/providers/repositories.
Inspect existing stage tests before adding focused regression cases. Use fake
providers and disposable fixture storage; no live provider calls, production
database writes, schema conversions, or destructive cleanup for acceptance tests.
Keep credentials out of output, fixtures, metadata and subprocess arguments.

Do not infer provider coverage, market closures, original receipt times or
completeness from a successful row, an empty response, maximum ID or filename.
Preserve earlier local history even when it cannot be downloaded again. Do not
zero-fill missing prices or silently change targets. Features use previous
session closes; targets and fills use entry close to next exchange-session
close. Keep mandatory cash rest and anchored independent allocations.

Run focused tests and required checks for affected modules. Report actual
commands/results and remaining limitations. End with a handoff in this brief:
status, files changed, accepted semantics, tests, and next-stage prerequisites.
Do not mark an incomplete stage done or implement later stages opportunistically.
```

## 1. Separate coverage concepts and implement shared discovery

```text
Implement Stage 1: source-scoped automatic coverage discovery.

Inspect market_data/history.py, history_store.py, coverage.py, publication.py,
providers/contracts.py, providers/polygon.py, providers/configured_polygon.py,
database/repositories/sessions.py, the validation repositories, calendar/, and
tests/stage2 through stage4. Implement discovery and its typed contracts first;
CLI consumers are the next two stages.

Represent separately: (a) observed local bounds with evidence quality, (b)
verified retained coverage/holes, (c) currently fetchable provider bounds with
capability check time/provenance/freshness, and (d) requested/effective bounds
and latest expected published session at an injected current clock. Min/max
dates describe observations, never continuous completeness. Use real dates and
exchange sessions, not ID ordering. Keep full source/request identity.

Inventory existing source-bound response manifests and real date mappings with
bounded read-only queries. Earlier DB-only/unbound legacy data remains visible
as unverified; do not promote filenames or rows to source/receipt evidence.
Scan metadata once per run and avoid one DB query per ticker/session.

For provider history, prefer authoritative account/request capability metadata.
If the current adapter has no such facility, add a typed optional capability
adapter and an honest unknown result. Any supported probing must have explicit
rate/request budgets, use exchange sessions, and distinguish entitlement limits
from auth errors, 429, transient errors, delayed/empty data and interior gaps.
Do not assume response success is monotonic or claim the exact earliest date
from the first success. Return earliest observed success separately if useful.
No guessed URL, fixed lookback or endless scan is an acceptable implementation.

Version metadata; load existing history JSON without deleting it. Existing
single-boundary metadata records what it actually established, not today's
account entitlement. Preserve earlier retained coverage when provider limits
advance. Keep explicit history start/evidence as optional overrides with clear
source binding, precedence and conflict diagnostics; never silently rewrite a
conflicting boundary. Do not encode unknown availability as an invented date.
Document the migration and the treatment of stale capability evidence.

Tests: earlier local data than provider access; sparse local coverage; raw rows
without manifests; no local data; corrupt/source-mismatched metadata; rolling
access; stale evidence; override conflicts; auth/rate/transient/empty responses;
weekends, holidays, early closes and a publication cutoff around session end.
Offline discovery must make zero HTTP calls or writes. Keep existing consumers
working until the next stages migrate them.
```

## 2. Make validation discover retained history without writing

```text
Implement Stage 2 using Stage 1's contracts. Inspect cli/validate_database.py,
cli/validation_setup.py, workflows/validate_database.py, validation/, database/
repositories/mysql_validation.py, reporting, and tests/stage4 and stage5.

With history/date arguments omitted, inventory and audit all observed retained
history through the latest expected published session at the current clock.
An explicitly bounded request remains supported. The validator must be entirely
offline/read-only: no fetch, schema setup, history save, receipt rewrite or
repair execution. It may use valid cached provider capability evidence; an
unknown live entitlement must not prevent structural inspection of local rows.

Distinguish integrity/source verification from market coverage and model-input
readiness. Earlier archive dates remain inspectable if current provider access
starts later. Missing source evidence, gaps or unavailable live coverage remain
typed unverified/missing findings, not proof of correctness. An unknown earliest
source boundary must not turn the observed minimum into a claim that no earlier
data is expected. Show the auditable scope and any unresolved boundary explicitly.

Retain history overrides and as-of cutoffs with evidence validation. Report
local observed bounds, verified coverage, provider capability/freshness, effective
scope, latest expected session and unresolved limits separately in text/JSON.
Update repair plan versioning/decoding so out-of-access gaps get explicit blocked
or manual-review reasons, never automatic impossible refreshes. Preserve existing
finding semantics, numerical checks, feature scope and safe exit codes.

Tests must prove no history arguments are needed for usable local evidence,
older retained archives are audited, an empty/unbound/corrupt database does not
pass coverage, interior holes are found, and provider access is never contacted.
Assert zero writes including history/manifest files and DB rollback behavior.
Test overridden subsets, source mismatch, future cutoffs and immature final labels.
Update docs/database_validation.md to describe only implemented behavior.
```

## 3. Make updating discover fetch coverage and preserve older archives

```text
Implement Stage 3 using Stages 1-2. Inspect cli/update_arguments.py,
cli/update_setup.py, workflows/update_market_data.py and update helpers,
market_data/manifests.py, corrections.py, providers/, transactional repositories,
repair-plan application, and tests/stage3, stage4 and stage7.

Default to discovered retained/fetchable history and latest expected published
session. Network requests must stay inside current evidenced provider access;
local audit/reconciliation may extend earlier. Retain optional history overrides
and explicit start/end subsets. When no trustworthy fetch boundary is known,
report the specific unresolved capability instead of guessing; still expose
local audit coverage. Do not make routine users supply history flags when
source/capability evidence can establish the scope automatically.

Process old local evidence without requiring a provider recheck outside access:
intact matching response/manifests may restore a missing import or rebuild
derived data. Missing/corrupt source content outside access remains blocked.
Never delete or truncate inaccessible archives, manufacture receipts, or mark
an unrecoverable gap complete. A denied refresh preserves prior content and
records a separate failed/stale refresh result. A source row deletion is allowed
only by existing complete-response reconciliation for that exact session, never
because provider history moved forward.

Preserve gap repair throughout the requested scope, atomic persistence,
transactional imports/receipts, correction schedules, dependency rebuilds and
revision invalidation. Distinguish reconciled local archive from currently
refreshable coverage in completion and exit reporting. Validate repair plans
before side effects, including inaccessible proposals and stale capability.
Schema creation stays explicit. Update recommendation/update assembly consumers
so they use the shared resolver rather than restoring a mandatory history flag.

Tests: local 2010/provider 2021 fixture; no fetch before 2021 and no archive loss;
cached-only reimport/rebuild; inaccessible corrupt archive; rolling entitlement;
interior holes despite a later maximum; correction invalidation; interrupted
transaction/restart; newest unpublished bar; repeated unchanged run; safe subset
and repair-plan handling. Update updater docs and run affected integration tests.
```

## 4. Optional: support explicitly retrospective archive research

Use this stage if the desired experiments use backfilled history without original
decision-time receipts. Strict replay remains the default. Skip it if original
contemporaneous evidence already supports the selected training/simulation dates.

```text
Implement Stage 4: an explicit retrospective research availability policy.
First reproduce, with a small fixture, why recently downloaded old daily bars
are excluded by original feature-decision cutoffs. Inspect datasets/building.py,
features/construction.py, market_data/availability.py, close_projection.py,
training jobs/artifact metadata, simulation jobs/signals/prices, and tests/stage5
through stage8. Do not solve exclusion by changing real receipt timestamps.

Add a typed policy with strict point-in-time behavior as the default and a
separately opted-in retrospective daily previous_close research mode. The latter
may assume verified backfilled bars became available at the calendar close plus
the evidenced publication delay, solely for research signal/label eligibility.
Store actual receipts and an explicit assumption separately. Source/content
verification still applies. Training labels must mature at each fitting cutoff;
features and liquidity must never use the entry session's final daily values.
Execution audit time remains separate. Reject entry_open under this policy.

Carry the policy and data snapshot/revision through job/config decoding, sample
and cache identities, artifacts, evaluation, fixed/walk-forward simulation and
reports. Prevent strict/research artifact or cache mixing. Current production
recommendation commands must reject research-mode artifacts pending a separate
explicit deployment design. Preserve existing strict artifacts as strict where
their schema permits an unambiguous migration; otherwise fail with rebuild guidance.

Reports must state that current corrected/adjusted history and chosen universe
may contain hindsight and do not establish an original point-in-time replay.
No synthetic opening evidence or fabricated revision history is allowed.

Tests compare strict/research on identical late-receipt data, prove actual
receipts are unchanged, reject future/unpublished input and immature labels,
reject policy mixing and entry_open, and verify fixed-model chronology and
walk-forward maturity remain enforced. Document the new actual field/CLI names,
limitations and rebuilding needs. Do not silently relax existing timing tests.
```

## 5. Provide usable subset-training and simulation examples

```text
Implement Stage 5: reproducible user configurations and a small end-to-end
acceptance fixture. Read training/jobs.py, workflows/train_model.py, simulation/
jobs.py, workflows/simulation_runtime.py, artifacts/loading.py, reporting/training.py,
docs/model_training.md, docs/simulation.md and docs/operations_guide.md.

Add complete parser-valid example JSON files for a small previous_close training
subset, a strictly later fixed simulation and chronological retraining. Use
clearly marked fixture/non-secret storage and policies. Distinguish entry-date
subsets from lookback/next-session dependencies. Document selecting ordered real
ticker IDs without future-survival filtering. Keep historical availability
requirements explicit; use Stage 4 only if it was actually implemented and opted in.

Demonstrate reading artifact.path and artifact.artifact_id from training JSON.
The identity is the canonical payload content ID, not the archive file SHA256.
Show a fixed later-period run and explain rejection of a model fitted on the
same simulation period. Show walk-forward bootstrap history and mature-label
filtering. Include fees, independent cash-rest allocations, skips and end positions.

Use existing in-memory repositories and deterministic CPU fixtures to train a
tiny model, export/reload it and simulate a later range without MySQL/network.
Assert sample boundaries, exact ticker order, no future features/labels, stable
allocation parity, artifact identity and event/accounting correctness. Run the
current parsers against every new JSON example and relevant stage tests. Do not
introduce a second feature or simulation implementation just for this example.
Update the guide with the exact commands now supported and remaining blockers.
```

## 6. Measure before choosing Java work

```text
Implement Stage 6: a bounded performance study, not a port. Read Python database
inspection, manifest/response decoding, hashing, raw/derived reconciliation,
datasets and simulation loops, plus StockGuidance/java/src/stockguidance/.
Record the Java correctness findings in operations_guide.md against current code.

Create a reproducible offline benchmark on fixed synthetic/exported non-secret
fixtures at several date/ticker sizes. Separate SQL/query count and transfer,
JSON parsing/hashing, reconciliation/derived computation, tensor construction,
inference and portfolio event processing. Measure elapsed time, throughput and
peak memory with versions, hardware, fixture identity and cold/warm conditions.
Do not benchmark an incomplete Java implementation as semantically equivalent.

Try the smallest appropriate Python improvements: bounded/batched reads,
streaming, avoiding repeated scans and vectorized numerical work. Evaluate SQL
plans on disposable storage if available; otherwise mark DB measurements absent.
No unsolicited production schema/index changes. Include startup, serialization
and process-boundary costs in any Java comparison; never make timing a flaky CI test.

Write docs/java_performance.md with actual results and ranked named hotspots.
Propose Java only for remaining measured bottlenecks. Default candidate order is
streaming read-only reconciliation, then bulk imports, then frozen-score
simulation, but evidence may change or eliminate that order. Keep Torch fitting,
feature contracts and provider/calendar policy in Python initially. State clearly
when no performance justification for a port has been established.
```

## 7. Conditional: establish a Java build and shared contracts

Run this before any Java backend work if the performance review supports a port,
or if you independently want the existing prototypes made maintainable.

```text
Implement Stage 7: a reproducible Java foundation under StockGuidance/java.
Inspect all existing Java sources, bundled JDBC dependencies and available JDK.
Establish one Maven or Gradle build with an explicit supported Java version,
pinned dependencies, a real JSON parser, unit-test framework and documented
PowerShell build/test commands. Reuse local tooling when suitable; accurately
report dependencies unavailable under network restrictions. Do not claim a build
passed if it could not be run.

Replace regex JSON parsing in the active configuration path with typed validated
decoding. Match Python's explicit JSON database path, types, database/schema
conflict rules, and credential-safe failures; remove parent-directory discovery
from that active path. Specify JDBC read-only transaction/isolation and rollback
behavior. Use prepared parameters and allowlisted identifiers. Keep historical
legacy model readers separate from current contracts; don't imply old formats work.

Define a versioned Python/Java interchange contract for explicit exchange dates,
ordered ticker IDs, source identity, coverage/provenance, actual receipt times,
availability policy if present, null values, errors, revisions and findings.
Decide canonical hash bytes with golden fixtures (sorted keys, number formatting,
Unicode, nulls and timezone serialization); do not compare ad hoc JSON strings.
Use integer minor units or BigDecimal for money with matching rounding rules.

Generate small neutral fixtures from current Python reference objects and consume
them in Java tests. Test escaped JSON, negative/exponent numbers, nulls, missing
fields, ID gaps/reordering, invalid config and secret-free errors. Do not add a
provider downloader, live writer or model-training port in this stage.
```

## 8. Conditional: implement a streaming read-only Java validator

```text
Implement Stage 8 only after Stage 7, and use Stage 6 measurements to choose a
bounded validation hotspot. Python remains the semantic reference. Inspect the
current validator/report/repair contracts and Java JDBC infrastructure.

Implement an opt-in Java backend for the selected streaming inspection/
reconciliation operation, behind an explicit Python integration boundary. Pass
the selected config path without credentials on the command line. Keep one
long-lived/batched invocation per workload, bounded date queries and deterministic
output. Distinguish SQL NULL using wasNull or nullable getters; never turn it
into zero. Detect duplicates/orphans independently of decoded row counts. Do not
sort chronology by mapping IDs or filter a universe by global missing_dates.

Either implement the full selected contract or report unsupported checks
explicitly; never return complete for checks that were skipped. Preserve local
archive/provider bounds, source/content verification, actual receipt/as-of rules,
finding taxonomy, numerical tolerances, revisions and repair proposals. Derive
sessions from the authoritative exported calendar rather than a second calendar
implementation. Validation must never execute SQL writes, schema initialization,
repairs, manifest updates or provider requests.

Compare Python/Java normalized results on golden clean/corrupt/gap/old-archive/
late-receipt/null/duplicate/orphan fixtures, with randomized row and ID order.
Check zero writes with disposable DB fixtures and credentials absent from errors.
Benchmark equal results including JVM startup/transport and peak memory. Keep
Python default unless a documented improvement is demonstrated. A Java backend
failure must surface clearly, not silently skip validation or claim readiness.
```

## 9. Conditional: port one additional measured hotspot

Choose **one** variant per conversation. Skip both if Stage 6/8 measurements do
not justify further work. A simulator correctness rewrite is substantial work,
not a prerequisite for database maintenance.

### 9A. Bulk import and derived rebuild

```text
Implement Stage 9A only if profiling still identifies import/rebuild CPU or
allocation as a bottleneck after batching improvements. Use Stage 7 contracts.
Keep provider access, discovery/planning and explicit repair authorization in
Python; Java consumes already validated source batches and an explicit plan.

Port only the selected database operation. Preserve per-session atomic raw rows,
mappings and import receipts; separately atomic derived rows/dependency receipts;
idempotent retry; rollback; source binding; archive preservation; content/revision
hashes; and predecessor/successor correction invalidation. Legacy derived columns
remain legacy-open-to-close-v1, never newly relabeled training targets. Enforce
one writer ownership contract and fail safely on partial cross-process failure.

On disposable MySQL fixtures compare Python/Java row contents, receipt bytes or
canonical identities, revisions and post-write validation. Inject failure before
commit, after raw commit and during derived rebuild; repeat to prove recovery.
Run the read-only validator after each scenario. Benchmark with transaction,
transport and JVM costs included. Keep this backend opt-in unless parity and
measured benefit pass; never run acceptance writes against the user's live DB.
```

### 9B. Simulation over verified frozen scores

```text
Implement Stage 9B only if repeated simulation event processing is a measured
remaining bottleneck. Use Stage 7 contracts and the Python simulation/portfolio
implementation as the reference. Existing Java open-to-close simulation is not
an acceptable starting semantic baseline.

Consume versioned frozen score/eligibility and audited execution-price fixtures
with exact ticker IDs, decision cutoffs, artifact IDs, source/revision, sessions
and availability policy. Reject old unversioned score CSVs. Python owns feature
construction, inference and chronological retraining; do not port Torch.

Replace index+1 recommendation IDs with the supplied ordered ticker IDs, enforce
top-K and identical finite-score/tie/eligibility behavior. Use close(T+1)/close(T)-1,
bounded real-date queries and original feature cutoffs. Implement identical
anchored alternate_sessions/staggered_halves, independent balances, overnight
cash rest, cents/quantity rounding, fees, skipped entries, missing/late exits,
open end positions, stale marks and complete-universe benchmarks. No future
price or same-session final liquidity may enter decisions.

Compare every event, selected ticker, cash category, quantity, fee, mark,
benchmark and final portfolio with Python golden fixtures including holidays,
early closes, ID gaps, ties, nonfinite scores, missing observations and diverging
A/B balances. Prove conservation and chronology. Benchmark equal results and
interchange costs; retain the Python reference and opt-in selection until justified.
```

## 10. Reconcile documentation and run acceptance checks

```text
Implement Stage 10 after the chosen stages. Read their handoffs and inspect the
actual code, not just claimed completion. Update README.md, docs/operations_guide.md,
database_validation.md, market_data_updating.md, model_training.md, simulation.md
and java_performance.md where present so all commands, fields and limitations agree.

Document existing DB validate->update/repair->validate, new DB explicit setup->
update->validate, and routine update->validate->train/use-model. Explain automatic
local/current discovery, optional history/date overrides, old inaccessible
archive preservation, unresolved capability/gaps, strict versus research mode
only if implemented, subset lookback/label context, later fixed evaluation and
walk-forward use. Include exact artifact ID retrieval and supported Java status.

Run relevant Python tests and type checks, Java build/parity tests if applicable,
CLI help smoke checks and parser validation of complete examples. An offline
acceptance fixture must cover retained 2010 data with provider access from 2021,
no earlier fetch/deletion, a gap repair, a blocked old gap, subset training and
a later simulation without leakage. Do not run live downloads or production
repairs as acceptance checks. Remove claims of completed functionality for
skipped stages; report any blockers and checks that could not run.
```

## Stage handoffs

Initial status (2026-10-16): prompts prepared; Stages 1-5 are complete and Stages
6-10 remain pending. Documentation review confirmed the repaired configured
Python can run the four main CLI help commands. No production database/provider
operations, live training, Java build or performance measurements were performed
for this brief.

### Stage 1 handoff (2026-10-16)

Status: complete. Implemented source-scoped, read-only discovery contracts in
`src/stockguidance/market_data/history.py` and
`src/stockguidance/market_data/discovery.py`. The result keeps observed local
bounds/evidence quality, verified retained sessions and holes, fresh provider
capability bounds, requested/effective bounds, and the latest finalized/expected
published sessions separate. It uses real calendar sessions and an injected clock;
it never treats a maximum ID, filename, successful response, or local minimum as
continuous completeness.

Source-bound manifest inventory is in
`src/stockguidance/market_data/manifests.py`. Legacy database date mappings use
one bounded read-only query in `src/stockguidance/database/repositories/mysql_sessions.py`
and remain unverified. Provider capability metadata is optional and typed in
`src/stockguidance/market_data/providers/contracts.py`; the current Polygon
adapters return an honest unknown result without probing. Existing v1 boundary
JSON remains loadable; v2 discovery metadata is written only to a separate
`*.discovery.json` sidecar. Explicit overrides are source-bound, in-memory, and
report conflicts rather than silently rewriting persisted history.

Accepted checks: `python -m pytest tests/stage1` passed 73 tests; `python -m pytest tests/stage2 tests/stage3/test_provider.py` passed 90 tests; the complete `python -m pytest` suite passed 722 tests; and `python -m mypy src/stockguidance/market_data src/stockguidance/database/repositories/mysql_sessions.py` passed with no issues in 32 source files. The commands used the configured PyCharm Python 3.14.8 executable with `PYTHONPATH=src`; no live HTTP, production database, or metadata writes were used. Stage 2 prerequisites are migrating validation to these contracts, including offline inventory/audit, explicit unresolved provider capability, and zero-write guarantees.

### Stage 2 handoff (2026-10-16)

Status: complete. Validation now uses Stage 1 discovery when history and date
arguments are omitted. `cli/validation_setup.py` performs one bounded local
inventory of source-bound manifests and legacy database observations, loads only
valid cached provider capability metadata, and never constructs a live provider.
The validator audits the discovered retained scope through the latest expected
published session, while a synthetic read-only query floor keeps an unknown
earliest source boundary from being reported as a historical claim.

Reports expose local observed bounds, verified retained sessions and holes,
provider capability/freshness, effective scope, latest expected publication, and
unresolved limits independently from integrity findings and model-input
readiness. Explicit start/end, as-of, history overrides, source validation,
immature labels, and existing numerical/feature checks remain supported. Repair
plans retain version 1 decoding and use version 2 only when a
`blocked_out_of_access` item with a reason prevents an impossible automatic
refresh.

Accepted checks: `python -m pytest tests/stage2 tests/stage4 tests/stage5/test_repository_and_validation.py` passed 142 tests; the complete `python -m pytest` suite passed 725 tests with 3 existing model warnings; and the affected mypy check over 17 source files passed with no issues. The new Stage 2 tests cover omitted history bounds, local/provider separation, interior gaps, zero provider calls, zero validation writes, rollback-preserving inspection, cached capability and blocked old-gap repair. Commands used the configured PyCharm Python 3.14.8 executable with `PYTHONPATH=src`; no live HTTP or production writes were used. Stage 3 migrates updater setup and planning to the shared resolver without changing this validator's offline contract.

### Stage 3 handoff (2026-10-16)

Status: complete. Updating now resolves omitted bounds through the shared
source-scoped discovery result. `cli/update_setup.py` inventories source-bound
manifests once, optionally inventories legacy database observations with one
bounded query, loads only valid cached capability evidence, preserves the
existing v1 history boundary, and uses an explicit non-persisted local query
floor when retained data predates that boundary. Explicit start/end and
source-bound history overrides remain supported; a conflicting override is
rejected without rewriting metadata.

`coverage_planning.py` and `update_planning.py` separate local reconciliation
from fetch authorization. Missing or inaccessible sessions cannot trigger a
network request without fresh verified capability bounds. Intact matching
manifests can reimport and rebuild older local data without provider access.
Due refreshes outside access preserve prior content and are reported as
`stale_refreshes`; corrupt source content remains unresolved and is never
marked complete. Repair plans reject blocked or stale out-of-access refreshes
before schema/provider/database writes. Update JSON and run metadata expose
local bounds, provider state/freshness, fetchable bounds, unresolved limits,
history provenance, stale refreshes, and data revision.

Files changed: `src/stockguidance/cli/update_setup.py`,
`src/stockguidance/cli/update_market_data.py`,
`src/stockguidance/market_data/coverage.py`,
`src/stockguidance/market_data/coverage_planning.py`,
`src/stockguidance/market_data/manifests.py`,
`src/stockguidance/market_data/update_planning.py`,
`src/stockguidance/validation/repair.py`,
`src/stockguidance/workflows/update_market_data.py`,
`src/stockguidance/workflows/update_metadata.py`,
`src/stockguidance/workflows/update_result.py`, and updater documentation in
`docs/operations_guide.md`, `docs/database_validation.md`,
`docs/market_data_updating.md`, and `docs/frontends.md`.

Accepted checks: `python -m pytest tests/stage3 -q` passed 90 tests;
`python -m pytest tests/stage3 tests/stage4 tests/stage7 -q` passed 204 tests;
and `python -m pytest -q` passed 728 tests with 3 existing model warnings. The
focused updater test includes provider-scope fetch blocking, cached-only
reimport, stale refresh/archive preservation, atomic recovery, correction
invalidation, and repair-plan handling. The affected mypy command over the 10
changed Python modules passed with no issues; the command was
`python -m mypy src/stockguidance/cli/update_setup.py src/stockguidance/cli/update_market_data.py src/stockguidance/market_data/coverage.py src/stockguidance/market_data/coverage_planning.py src/stockguidance/market_data/manifests.py src/stockguidance/market_data/update_planning.py src/stockguidance/validation/repair.py src/stockguidance/workflows/update_market_data.py src/stockguidance/workflows/update_metadata.py src/stockguidance/workflows/update_result.py`.
The four main CLI `--help` smoke checks also passed. Commands used the
configured PyCharm Python 3.14.8 executable with `PYTHONPATH=src`. No live
provider calls, production database writes, schema setup, or destructive cleanup were used.
Stage 4 prerequisites were the explicit retrospective policy, strict default,
and unchanged actual receipt evidence; they are recorded in the Stage 4 handoff
below. Strict point-in-time behavior remains the default.

### Stage 4 handoff (2026-10-16)

Status: complete. Added the typed `AvailabilityPolicy` with `strict` as the
default and the explicitly opted-in `retrospective_daily_previous_close` mode.
Research mode accepts only verified daily previous-close observations and uses
an explicit aware `data_snapshot` for read scope; it treats calendar close plus
the evidenced publication delay as an eligibility assumption. Actual
`ObservationTiming.available_at` receipts, source verification, revisions and
execution audit timestamps remain separate and unchanged. Entry-open signals
are rejected in research mode, and labels still mature at each fitting cutoff.

Policy and snapshot identity now flow through samples/cache keys, training and
evaluation datasets, model semantics/provenance/artifacts, fixed and
chronological simulation, eligibility, training/recommendation/simulation
reports, and JSON job/config decoding. Research artifacts cannot mix with a
different policy or snapshot in simulation; production recommendation jobs and
artifacts reject research mode. Existing strict artifact metadata remains
loadable with an unambiguous strict migration. Reports identify the assumption,
actual receipt preservation, point-in-time status, and the hindsight limitation
of corrected/adjusted history and the selected universe.

Files changed: `src/stockguidance/market_data/availability.py`,
`src/stockguidance/features/specification.py`,
`src/stockguidance/features/construction.py`,
`src/stockguidance/datasets/building.py`,
`src/stockguidance/datasets/identity.py`,
`src/stockguidance/datasets/sample.py`,
`src/stockguidance/datasets/training.py`,
`src/stockguidance/artifacts/contracts.py`,
`src/stockguidance/artifacts/decoding.py`,
`src/stockguidance/training/`, `src/stockguidance/recommendations/`,
`src/stockguidance/simulation/`, `src/stockguidance/workflows/`, and
`src/stockguidance/reporting/`, plus `docs/model_training.md`,
`docs/simulation.md`, `docs/operations_guide.md`, and the focused Stage 4
fixture in `tests/stage4/test_retrospective_availability.py`.

Accepted checks: `python -m pytest tests/stage4 tests/stage5 tests/stage6
tests/stage7 tests/stage8 -q` passed 368 tests; the complete `python -m pytest
-q` suite passed 731 tests with 3 existing model warnings; the affected mypy
check over 59 source files passed with no issues; and the four main CLI
`--help` smoke checks passed. The fixture proves strict exclusion versus
retrospective acceptance for a late backfilled close, unchanged actual receipt
time, entry-open rejection, mature-label enforcement, and no live provider or
production database operations. Commands used the configured PyCharm Python
3.14.8 executable with `PYTHONPATH=src`.

### Stage 5 handoff (2026-10-16)

Status: complete. Added three complete parser-valid, non-secret fixture
configurations under `examples/stage5/`: a previous-close entry-date training
subset, a strictly later fixed-model simulation, and a chronological-retraining
simulation with explicit bootstrap history and anchored cadence. All examples
use strict availability, explicit ordered ticker IDs, decimal capital/fees,
staggered independent allocations, and disposable fixture storage paths. The
guides now identify the exact files and commands, distinguish entry-date scope
from lookback/next-session dependencies, explain why same-period fixed models
are rejected, and show how to read `artifact.path` and the canonical
`artifact.artifact_id` from training JSON.

The new offline acceptance fixture in
`tests/stage5/test_examples_acceptance.py` trains a deterministic tiny CPU
model through the existing in-memory close repository, reads the reported
artifact reference, exports/reloads it, and simulates a later range through the
existing simulation engine. It checks exact ticker order, no future features or
labels, canonical artifact identity, fees, independent cash-rest balances,
anchored allocation parity, a missing-entry skip, an open end position and
event/accounting semantics. No MySQL, provider, schema setup or production
write is used.

Accepted checks: the configured PyCharm Python 3.14.8 executable ran
`python -m pytest tests/stage5 -q` with 78 passed and
`python -m pytest tests/stage5 tests/stage6 tests/stage7 tests/stage8 -q` with
313 passed and 3 existing Torch warnings. The same executable, with
`PYTHONPATH=src`, parsed all three example JSON files through
`training.jobs.load_job`/`simulation.jobs.load_configuration`. No live provider,
MySQL, schema setup or production write was used. Stage 6 was limited to
performance measurement; no Java or production performance work was introduced
here. Stages 7-10 remain pending.

### Stage 6 handoff (2026-10-16)

Status: complete. Added the deterministic offline benchmark in
`src/stockguidance/performance/stage6.py`, the thin launcher
`tools/stage6_benchmark.py`, and the non-secret fixed fixture specification
`benchmarks/fixtures/stage6.json`. It measures JSON parsing/hashing,
raw/derived decoding and reconciliation, tensor construction, single-sample
and candidate batched inference, and typed portfolio event processing at small,
medium, and large date/ticker sizes. It records cold/warm elapsed time,
throughput, tracked peak Python allocations, optional resident deltas,
versions, host details, and a canonical fixture identity. SQL/query and
transfer measurements are explicitly absent because no disposable SQL service
was available; the run made no provider, database, schema, or production data
calls.

The study found raw/derived reconciliation, manifest JSON decode/hash, and
typed portfolio events as the ranked measured hotspots. The batched inference
candidate is faster but is not adopted because cross-decision batching would
violate chronological simulation cutoffs. The Java review is recorded in
`docs/java_performance.md` and `docs/operations_guide.md`: regex JSON,
SQL-NULL handling, parent-path discovery, legacy IDs, missing source/receipt
contracts, and the absent build/parity foundation prevent a valid Java timing
or port recommendation. No production Python optimization, schema/index
change, Java implementation, or Java benchmark was introduced.

Accepted checks: the configured PyCharm Python 3.14.8 executable ran
`PYTHONPATH=src python tools/stage6_benchmark.py --fixture
benchmarks/fixtures/stage6.json --output output/stage6-performance.json` to
completion with all semantic guards passing across three fixture sizes. The
run recorded Python 3.14.8, NumPy 2.5.4, Torch 2.14.1+cu132, one Torch CPU
thread, Windows 11, and a 24-CPU AMD64 host. No disposable SQL service was
available, so database query/transfer acceptance is explicitly not claimed.
Stage 7 prerequisites were the reproducible Java build, typed JSON and database
contracts, and Python/Java golden parity fixtures recorded in the next handoff.
The current Python implementation remains the semantic reference.

### Stage 7 handoff (2026-10-17)

Status: complete. Established a Maven Java 21 foundation under
`StockGuidance/java/` with current sources in `src/main/java`; historical files
under `src/stockguidance` remain outside the build as unsupported legacy readers.
The active configuration path now uses Jackson typed tree decoding from an
explicit JSON path, rejects unknown fields, wrong types, missing required values,
and conflicting `database`/`schema` names, and keeps passwords out of object and
error text. JDBC access has an explicit MySQL factory, allowlisted identifiers,
prepared date parameters, read-only repeatable-read transactions, and rollback
on both successful and failed inspections.

The versioned interchange v1 contract covers source identity, calendar and
exchange dates, ordered ticker IDs, observed/verified/hole coverage with
provenance, actual receipt times, UTC `Z` timestamp serialization, availability
policy, nullable values, revisions, errors, findings, canonical SHA-256 bytes,
and BigDecimal money with scale-four `HALF_UP` rounding. The fixture
`StockGuidance/java/fixtures/interchange-v1.json` was generated from current
Python reference values by `tools/stage7_generate_fixtures.py`; Java tests
consume its golden hash and verify escaped Unicode, negative/exponent numbers,
nulls, missing fields, ID gaps/reordering, invalid config, secret-free errors,
prepared parameters, identifier allowlists, and rollback behavior.

Files changed: `StockGuidance/java/pom.xml`,
`StockGuidance/java/README.md`, the current Java source/test trees and fixture,
`src/stockguidance/interchange.py`, `tools/stage7_generate_fixtures.py`, and
the Java status section in `docs/operations_guide.md`.

Accepted checks: the configured PyCharm Python 3.14.8 executable ran
`tools/stage7_generate_fixtures.py` and produced fixture hash
`5e1fa46e012effbb02606d065b410e61a045f7bfddb3b7489f59205e6854ca05`.
With Maven 3.9.9 and OpenJDK 26.0.2.1 compiling Java 21 bytecode, offline
`mvn -o test` passed 12 tests and offline `mvn -o -DskipTests package` passed.
The build uses cached Jackson 2.15.2, JUnit Jupiter 5.13.4, compiler 3.13.0,
Surefire 3.5.3, and the bundled MySQL Connector/J 9.6.0 system jar. Maven
reports the expected system-path warning; no network dependency download, live
provider call, database connection, schema setup, production write, or model
port was used.

### Stage 8 handoff (2026-10-17)

Status: complete. Selected the Stage 6 `raw_derived_reconciliation` hotspot and
added the opt-in `stockguidance.validation.ValidatorMain` backend under
`StockGuidance/java/src/main/java/stockguidance/validation/`. The typed request
contract carries source identity, an authoritative exported calendar, bounded
scope/cutoff, source evidence, and history diagnostics. The validator uses one
read-only repeatable-read transaction, bounded prepared date queries, streaming
result sets, explicit `wasNull()` handling, independent duplicate/orphan
detection, source/content and receipt revisions, chronological legacy-derived
reconciliation, Python-compatible tolerances, deterministic findings, and
repair proposals. It never initializes schema, writes SQL, repairs rows,
updates manifests, or contacts a provider.

The Python boundary in
`src/stockguidance/validation/java_backend.py` passes only explicit config and
request paths, normalizes the selected report shape, and keeps Python as the
default. Java fixture-backed JDBC tests cover clean, old-archive context,
nullable values, corrupt derived data, late receipts, gaps, duplicates,
orphans, randomized row/ID order, rollback, zero commits, and bounded SQL.

Files changed: the Stage 8 Java validation/request/content/finding classes and
tests, `src/stockguidance/validation/java_backend.py`,
`tests/stage8/test_java_backend.py`, `StockGuidance/java/README.md`,
`docs/java_performance.md`, and the Java/performance sections of
`docs/operations_guide.md`. No existing Python
validator default or production integration was changed.

Accepted checks: offline `mvn -o test` passed 17 Java tests, including 5 Stage 8
validator tests. The configured PyCharm Python 3.14.8 executable ran
`python -m pytest tests/stage8 -q` with 85 passed, including 4 Python boundary
and normalized-parity tests. No disposable MySQL service was available, so
an end-to-end JVM startup/transport/peak-memory comparison against a live SQL
fixture is explicitly not claimed; the Python backend remains the default.

Next-stage prerequisites for a conditional Java hotspot port remain a
disposable MySQL parity fixture and a measured end-to-end benefit after JVM
startup and transport costs. The approved source-retirement/frontend migration
is recorded in the Stage 9 handoff below; no Java hotspot port was selected.

### Stage 9 handoff (2026-10-17)

Status: complete. Applied the approved source retirement and frontend migration
scope from `docs/stage9_retirement.md`. The 85 listed legacy Python source/test
paths are absent, all 21 retained root/script/database/verification/viewer
frontends delegate once to their typed package entrypoints, and
`polygon_fetch.py` retains only the original `DEFAULT_API_KEY` plus the shared
updater delegation. The package includes that credential module for installed
use while imports remain side-effect free. User data, Java sources, datasets,
models, NPZ/CSV files, databases, caches and configuration were not cleanup
targets; the recovery archive and Git history remain available.

Files changed by the Stage 9 source cleanup are enumerated in
`docs/stage9_retirement.md`; the current checkout contains the cleanup from
commit `103f8f5`. No additional Java 9A/9B hotspot port was introduced because
Stage 8 did not establish a measured end-to-end benefit after JVM and transport
costs.

Accepted checks: the configured PyCharm Python 3.14.8 executable ran
`python -m pytest tests/stage9 -q` with 75 passed in 81.82 seconds. This suite
verified the exact retired-path count, one-call frontend delegation, wheel
packaging and import safety outside the checkout, no legacy imports, explicit
database configuration and credential-safe failures, diagnostics, reports,
batch/incremental workflows, repair round-trip behavior and chronology. No
live provider, production database, schema setup, production write or
credential-bearing subprocess was used.

### Stage 10 handoff (2026-10-17)

Status: complete. Reconciled the user-facing README and operations, validation,
updating, training, simulation, and Java-performance guides with the implemented
Stages 1-9 contracts. The documentation now describes automatic local/current
discovery, optional source-bound overrides, old-archive preservation, unresolved
capability and gap findings, strict versus explicitly opted-in research mode,
subset lookback/label context, later fixed evaluation, walk-forward training,
canonical artifact ID retrieval, and the supported Java foundation/opt-in
read-only validator status.

Added `tests/stage10/test_cross_stage_acceptance.py`, an offline disposable
fixture covering retained 2010 data with provider access beginning in 2021,
capability-bounded fetches, in-access gap repair, a blocked old gap, subset
training, artifact identity, and a later fixed simulation without future reads.
The fixture uses fake provider/database/close repositories and performs no live
provider calls, production database writes, schema setup, or repairs. The
post-Stage-9 import regression fix in commit `f90a67c` remains intact.

Accepted checks: the configured PyCharm Python 3.14.8 executable ran
`python -m pytest tests/stage10 -q` with 57 passed in 39.45 seconds, and the
complete `python -m pytest tests -q --tb=short` suite passed 738 tests with the
three existing Torch nested-tensor warnings. The four main CLI `--help` checks
passed, and parser validation of all three Stage 5 JSON examples passed. The
post-Stage-9 import regression remains covered by the full suite and the Stage
6/9 tests.

From `StockGuidance/java`, offline `mvn -o test` passed 17 Java tests and
`mvn -o -DskipTests package` passed. Maven emitted its existing system-scoped
MySQL connector warning; no network dependency resolution was used. The full
`python -m mypy` check remains limited by the pre-existing
`src/stockguidance/validation/java_backend.py:70` `Any | None` iteration error;
no Stage 10 production Python source changed, so that unrelated Stage 8 issue
was not broadened here. `git diff --check` passed.

No live provider calls, production database writes, schema setup, production
repairs, or credential-bearing subprocesses were used. Stage 10 has no remaining
prerequisites; Python remains the default operational and semantic backend.
