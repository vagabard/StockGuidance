# Luna 5.6 maintenance prompts

These are implementation tasks for the model you select as Luna 5.6. Use one
numbered prompt per conversation, in order, with the shared preamble below.
Prompts 4 and 7-9 are conditional as described. Stage 1 is marked complete in
the handoff below; later stages remain pending. Read [operations_guide.md](operations_guide.md) for current commands
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

Initial status (2026-10-16): prompts prepared; Stage 1 is complete and Stages 2-10
remain pending. Documentation review confirmed the repaired configured Python can
run the four main CLI help commands. No production database/provider operations,
training, Java build or performance measurements were performed for this brief.

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
