# StockGuidance staged refactoring prompts

## How to use this file

Run the numbered stages in order, starting each stage in a fresh conversation. Copy its entire prompt block, or use this shorter prompt with the appropriate stage number:

```text
Read REFACTORING_PROMPTS.md in the repository root. Follow its confirmed requirements and shared working rules, inspect the current implementation and stage handoffs, and implement Stage 1 only. Complete its acceptance checks and update the stage status and handoff before stopping.
```

Replace `Stage 1` with the stage you want to execute. Stages 1-9 implement the main refactor. Stage 10 is an optional enhancement for selecting stocks using the entry session's opening prices. Each prompt obtains its context from this file and the checkout; no previous conversation is needed.

This document is the implementation brief. Consult the stage status and handoffs for completed work, and verify the checkout before starting a dependent stage. Updating this file alone is not completion of a stage.

## Confirmed requirements

### Trading and information timing

- Buy at the closing price of entry trading session T. Sell at the closing price of the next trading session, T+1. Sessions are exchange trading sessions, not calendar days or consecutive database IDs.
- The untransformed training target is `close(T+1) / close(T) - 1`. Any ranking or normalization applied to targets must be explicit, fitted appropriately, and recorded. Targets, evaluation, benchmarks, and simulated fills must agree on this holding period.
- Generate recommendations early enough to place the closing-price purchase. Represent the decision cutoff separately from the eventual closing fill. A same-close fill is a stated simulation assumption; do not assume access to the completed daily bar before placing that order.
- The required initial signal mode uses historical closing prices through the previous completed session. Do not silently retain the old open-to-close price features as inputs to this mode. Stage 5 chooses and records the precise close-based feature transformations.
- The optional signal mode supplements that same historical information with the opening prices of all stocks in the model's ticker universe on the entry session. It is not limited to the stocks eventually selected. This mode can be deferred to Stage 10.
- Same-session final close, high, low, full-day volume, VWAP, and full-day transaction counts are unavailable at a decision made before that session closes. Enforce information availability for model inputs, eligibility filters, normalization, and benchmarks alike.
- Both signal modes still buy at the entry session's close and sell at the next session's close. Opening prices change the information used to select stocks, not the execution prices.
- A training sample is eligible only when its exit price was available by the training cutoff. For a decision before session T closes, the outcome for an entry at T-1's close is not yet complete. Do not train on it by querying a database that also contains future history.

### Cash rest and allocation

- Money from a sale must rest overnight before being traded again. Sale proceeds at session T's close cannot fund another purchase at that same close; they become eligible starting the next trading session. This is the required strategy rule, not a configurable assumption of immediate reuse.
- Support `alternate_sessions`: invest the available portfolio at session 1's close, sell at session 2's close, buy again at session 3's close, and continue.
- Support `staggered_halves`: split the initial capital equally into A and B once. A buys at session 1's close and sells at session 2's close; B buys at session 2's close and sells at session 3's close; A buys again at session 3's close, and so on.
- A and B keep their own cash, positions, proceeds, and eligibility times. Their values will diverge. Do not rebalance them, transfer gains between them, or repeatedly split total equity in half.
- Each allocation reinvests its own available proceeds when scheduled. Initially unused capital remains cash. Total equity includes cash, resting proceeds, and marked positions, without double counting.
- Anchor schedules to an explicit starting trading session. Missing records, failed predictions, and skipped orders must not silently shift alternate-session parity.
- Recommendation and simulation must use the same timing, eligibility, and allocation planning rules. Recommendations produce plans; they do not place orders or pretend suggested trades have already filled.

### Two recommendation modes

- `current`: refresh all currently available data within the configured coverage, validate readiness, and recommend using an explicitly identified existing model.
- `retrain`: perform the same refresh and readiness checks, train on the selected history with mature outcomes, then recommend using the resulting artifact.
- Both modes use the same feature, prediction, eligibility, ranking, and reporting code. A required-stage failure prevents a successful recommendation result.
- Backtests must distinguish a fixed model trained before the simulated period from chronological retraining. Retraining and prediction may use only information available at each simulated decision cutoff. Do not describe evaluation on the training period as an unbiased performance estimate.

### History, updating, and validation

- Remember the first supported historical session for the configured data source and request. Do not request earlier data or issue data queries with earlier bounds. Distinguish this expected lower bound from the earliest row currently present in the database.
- Do not invent that first date or assume it is 2015-01-01. Establish and persist it from inspected configuration, source history, or an explicit supplied boundary, and record the provenance. If it cannot be established, report that fact rather than claiming complete coverage.
- Respect the earliest usable feature window. Boundary samples without enough history are unavailable, not zero-filled historical observations.
- Check the entire requested coverage for gaps, not only dates after the latest file or maximum database ID. Fetch missing available data, complete interrupted imports, and refresh records subject to the provider's publication or correction policy.
- Define completeness relative to the actual provider request: endpoint, market/universe, adjustment policy, interval, historical limits, and publication availability. A provider omission cannot automatically be interpreted as a valid non-trading session.
- Keep sufficient source manifests and raw response metadata to reconcile requested sessions, response records, and database records. Account for listings, delistings, and legitimate absence of trading; do not expect every ticker on every session.
- Provide a standalone, read-only database validation command. Report missing, incomplete, inconsistent, not-yet-available, and unverified data distinctly. Produce actionable repair plans without automatically deleting data or executing schema changes.
- Missing predecessor-based features at the history boundary and immature forward labels at the latest boundary are expected conditions. Missing mature outcomes or unexplained interior gaps are not.
- Cache identities must include source/request identity, data revision or snapshot, interval, and relevant feature/target/model specifications. Backfills and corrections must invalidate affected cached values.

### Architecture and scope

- Prefer many cohesive packages and small modules. Breaking up the existing large scripts is a primary acceptance criterion. Avoid replacing a large script with a single equally large service class or a general-purpose utilities module.
- Use `src/stockguidance/` as the Python package root. The existing root-level `StockGuidance/` tree contains other work, including Java; keeping the Python package under `src/` avoids a case-insensitive path collision on Windows.
- CLI modules parse and validate arguments, assemble dependencies, invoke application functions, and map results/errors to output and exit codes. Application code must not build CLI argument strings to call other application code.
- Use typed configuration and result objects, explicit enums, one configured static type checker, and runtime validation at external-data and array-layout boundaries. Preserve the project's Python formatting conventions.
- Saved inference artifacts must be independent of mutable training-controller state and contain the exact feature, target, ticker-order, and model metadata needed for inference. Portfolio allocation policy remains independent of model input/output semantics.
- The project is unreleased. No legacy API, CLI, artifact, cache, or directory compatibility is required. Update internal callers and tests, and document when models or datasets need rebuilding. Temporary adapters during migration must be removed by Stage 9.
- The hardcoded Polygon API key is acceptable for now. Document its location and that it should eventually become external configuration. Do not reproduce its value in documentation, reports, or new configuration files, or make credential migration a prerequisite.
- Keep credentials out of new model artifacts, run metadata, and logs. Record non-secret source identity and a credential reference when needed.
- Prepare modest extension points for hourly or five-minute observations: interval identity, timezone-aware observation/availability timestamps, and separation of observation frequency from holding period and allocation cadence. Implement daily behavior first; do not build a full intraday system.

## Shared working rules for every stage

1. Read this entire file, applicable `AGENTS.md` instructions, the current Git status, and preceding stage handoffs. Inspect actual files and prerequisite behavior; a status row alone is not evidence that a dependency is complete.
2. This checkout already contains substantial user changes, including Python and Java work. Preserve them. Do not reset, stash, overwrite unrelated changes, create branches, or commit unless separately asked.
3. Implement only the selected stage and the prerequisite fixes strictly necessary for it. Choose routine details autonomously and record material decisions. If a prerequisite is substantially absent, explain the concrete dependency instead of silently implementing multiple stages.
4. Treat file names below as proposed destinations, not evidence that they already exist. Build on the previous stages' actual packages and record any justified naming adjustments.
5. Use the available Python-environment skill and IDE environment discovery before any Python toolchain commands. Use the configured interpreter and package manager. Do not replace the user's environment or use an arbitrary system Python.
6. Use `apply_patch` for edits. Keep modules focused, imports directional, and numerical layout contracts explicit. Do not add blanket type suppressions to make checks pass.
7. Use deterministic fixtures, temporary files, and fake provider/database adapters for routine tests. Do not run long production training jobs, download the entire market history, mutate the user's live database, send email, or submit trades as routine verification.
8. Run focused tests and static checks for the stage. Financial timing, completeness, and portfolio behavior require meaningful behavioral tests. Record unrelated pre-existing failures without repairing unrelated code.
9. During migration, do not silently connect an old feature/target convention to a new artifact or simulator. Validate specifications and reject mismatches. Do not claim a command is migrated until its full path works.
10. Before stopping, update the stage status and append or revise its handoff in this file. Include actual changed paths, public entry points, decisions, checks and results, outstanding issues, and the exact next stage. Keep secrets and large generated outputs out of the handoff.

## Proposed package boundaries

```text
src/stockguidance/
    cli/
    workflows/
    config/
    calendar/
    market_data/
    database/repositories/
    validation/
    features/
    targets/
    models/
    artifacts/
    training/
    inference/
    recommendations/
    portfolio/
    simulation/
    reporting/
    diagnostics/
```

Create subpackages as they acquire real responsibilities. Prefer focused files within these boundaries. The training engine, experiment orchestration, checkpointing, and diagnostics should also be separated rather than moved together into a large file.

## Stage status

| Stage | Deliverable | Prerequisites | Status |
|---|---|---|---|
| 1 | Package foundation and typed strategy contracts | None | Complete (see handoff) |
| 2 | Session calendar, history boundaries, and coverage planning | 1 | Complete (see handoff) |
| 3 | Complete and recoverable market-data updating | 2 | Complete (see handoff) |
| 4 | Standalone database validation | 3 | Complete (see handoff) |
| 5 | Shared close-based features and close-to-close targets | 4 | Complete (see handoff) |
| 6 | Independent model artifacts and typed training API | 5 | Complete (see handoff) |
| 7 | Current-model and retraining recommendation workflows | 6 | Complete (see handoff) |
| 8 | Cash-rest portfolio planning and chronological simulation | 7 | Complete (see handoff) |
| 9 | Frontend integration, remaining module extraction, and cleanup | 8 | Complete; main refactoring complete (see handoff) |
| 10 | Optional entry-session opening-price signal | 9 | Complete (see handoff) |

## Stage 1 - Package foundation and typed strategy contracts

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, and implement Stage 1 only.

Establish the src/stockguidance Python package, focused contract modules, and the minimum packaging/test configuration needed to import it with the configured environment. Inspect existing tests and record the relevant baseline before changing behavior.

Define typed contracts for request/source configuration, interval identity, session and decision timing, feature specifications, close-to-next-session-close target specifications, model refresh policy, allocation policy, recommendation results, coverage results, and stage failures. Keep these contracts in the packages that own them rather than one large shared types file. Use immutable configuration where appropriate.

The two model policies are current and retrain. The two allocation policies are alternate_sessions and staggered_halves. The required signal mode is previous_close; reserve entry_open for the optional enhancement and reject it clearly until implemented. The cash-rest rule is mandatory: sale proceeds cannot fund a purchase at the same close.

Make availability time distinct from observation time, entry time, and exit time. Separate data interval, holding horizon, and allocation cadence. Do not implement a full intraday framework or prematurely move all existing modules.

Configure one static type checker with strict checking for the new typed modules. Prefer the existing checker if one exists; otherwise choose one, document it, and avoid unnecessary tooling. Validate external values at runtime; type hints alone do not validate array shapes or CLI input.

Acceptance: package import works without command side effects; invalid policy/timing combinations are rejected; contracts represent the confirmed strategy unambiguously; focused tests and configured type checks pass. Record the package setup, interpreter/check commands, decisions, baseline failures, and Stage 2 handoff in this file. Do not begin Stage 2.
```

## Stage 2 - Session calendar, history boundaries, and coverage planning

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stage 1, and implement Stage 2 only.

Extract session/calendar and date-query responsibilities from choose_next_day_stocks.py, trainer/util/date_bounds.py, database/database_date_id_queries.py, and related loaders into small calendar, coverage, and repository modules. Normalize driver-specific dates in the database boundary. Inject the clock and configured database source; do not hardcode dbinfo.id inside reusable date resolution.

Persist expected history_start separately from observed database/file minima, with source/request identity and provenance. Do not guess the date. Represent the latest finalized session, currently available observations, first usable feature window, entry session, and label maturity separately.

Plan requests and queries only within supported history and the decision cutoff. Resolve previous/next sessions by the calendar and stored session chronology, never by adding/subtracting database IDs. Cover DST, holidays, early closes, and nonconsecutive IDs. Use an established calendar source suitable for the market rather than hand-maintained UTC close constants.

Produce a typed coverage/update plan identifying expected sessions, missing or incomplete sessions, and availability states. Historical target windows must be anchored to their requested session rather than the database's current latest dates. Date-ID lookup for newly fetched sessions belongs after ingestion has made those sessions available.

Acceptance: deterministic tests cover the first supported date, insufficient lookback, a missing earliest database row, empty storage with known history metadata, historical targets, nonconsecutive IDs, holidays/DST/early closes, and a session not yet published. No plan crosses the history boundary or decision cutoff. Update stage status and the Stage 3 handoff with the authoritative boundary behavior and calendar choice. Do not begin Stage 3.
```

## Stage 3 - Complete and recoverable market-data updating

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-2, and implement Stage 3 only.

Extract the update pipeline from choose_next_day_stocks.py, polygon_fetch.py, database/populate_db_raw_stock_info.py, and database/populate_db_daily_delta_stock_data.py into focused provider, manifest, planning, ingestion, repository, and update-workflow modules. Reusable code receives typed configuration and returns results instead of calling CLI main functions.

Reconcile the complete requested historical coverage with source manifests and database coverage. Download missing available sessions, complete interrupted imports, and refresh records according to an explicit publication/correction policy. Do not use MAX(date_id), the newest filename, or file existence alone as proof that earlier data is complete. Use persisted verification information to avoid needless full-history downloads each run.

Record request scope, response status, available provider count/continuation metadata, source record keys/counts, content identity, and import/derived-data completion. Validate the provider's response shape. Distinguish valid market closure, no trading, not-yet-published data, provider errors, and unverified results. Never convert an error response or unexpected empty payload into a successful complete session.

Write downloads atomically after validation. Make imports and derived updates idempotent and recoverable, with explicit transaction boundaries. Recalculate affected dependent rows after corrections, including next-session outcomes and transformations; do not only fill NULL columns. Tie cache invalidation to the resulting data revision.

Propagate every required-stage failure. Carry the selected data root, date limits, adjustment settings, interval, and database source through every layer. Add a thin update_market_data CLI. Document the existing hardcoded API key's location and future externalization without copying its value or making its removal part of this stage.

Acceptance: fake-source/database tests demonstrate recovery of an interior missing session after a later success, partial or corrupt files, interrupted imports, late corrections, repeated runs without duplicate rows, an initially empty database, custom roots/sources, and provider failures. A successful result means all required available data in the declared scope has been reconciled; unverified coverage is visible. Update this file and hand off Stage 4. Do not begin Stage 4 or mutate the live database for verification.
```

## Stage 4 - Standalone database validation

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-3, and implement Stage 4 only.

Create a thin validate_database CLI backed by small reusable validation modules. Reuse the coverage/request semantics and repository APIs from earlier stages. Run appropriate readiness checks from the updater too, so the two commands cannot disagree about completeness.

Check expected sessions and source manifests against raw database record keys/counts; interrupted imports; duplicate and orphaned records; invalid prices/values; required derived features; and mature outcomes. Account for ticker lifetimes and source omissions without demanding every ticker on every date. Keep expected history_start independent of current stored minima so leading gaps are discoverable.

Distinguish missing data, invalid data, expected boundary absence, not-yet-available outcomes, and coverage that cannot be verified from available source evidence. Report exactly which source/sessions/tickers/checks were assessed. Internal consistency alone must not be advertised as proof of provider completeness. Do not claim new close-to-close target checks before Stage 5 supplies their implementation.

Return typed findings and a structured repair plan. Provide readable output, machine-readable JSON, and documented exit codes for complete, incomplete, and failed validation. Default operation is read-only and does not fetch an entire history or repair/delete records. Repair plans should be consumable by the updater through explicit invocation.

Acceptance: fixtures cover leading/interior gaps, missing source keys despite plausible counts, legitimate ticker absence, duplicate/orphaned data, invalid prices, missing derived rows, immature final labels, and provider-unverified coverage. Prove the default command performs no writes. Update this file with command syntax, finding categories, results, and Stage 5 handoff. Do not begin Stage 5.
```

## Stage 5 - Shared close-based features and close-to-close targets

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-4, and implement Stage 5 only.

Refactor feature_vector_builder.py, feature_windowing.py, training_instance_provider.py, load_database_rows_into_tensors.py, and the relevant label/transform logic into small repositories, feature-specification, feature-window, dataset, and target modules. Keep data access separate from transformations and NumPy/Torch device handling.

Implement the required previous_close mode: the input for an entry at session T's close is constructed from historical closing prices available through the prior completed session. Choose explicit close-based normalization/return features and record the decision; do not silently substitute old intraday close/open ratios. Keep transformations identical between training and inference, fitted only on information available to the training process.

Associate every sample with entry_session T, decision cutoff, and exit_session T+1. Calculate raw outcome close(T+1)/close(T)-1 and any configured transformed targets from the same target specification. Preserve immature labels as unavailable and exclude them from training; never sanitize them into successful zero-return observations. Keep the entry close as a fill/target input, not an available signal input before that close.

Use an explicit ordered ticker universe, feature order, lookback, missing-data policy, price-adjustment policy, and data revision. Validate layouts and exact feature windows; remove size-based guessing about whether to include the current day. Missing required prices must produce explicit readiness or sample-validity outcomes. Leave entry_open unsupported with a clear message, while keeping its planned extension point.

Expose one shared sample-building API for training, recommendation, and simulation. Extend database validation for the new feature and target specifications. Keep unconverted legacy consumers isolated rather than feeding new semantics into them implicitly.

Acceptance: hand-calculated tests prove input availability and close-to-close targets, training/inference feature equality, exact ticker alignment, first-history and final-label boundaries, historical as-of behavior, and invalid/missing prices. Include a case where next-day open-to-close return differs materially from close-to-close return. Verify cache identity/invalidation for the new specs. Update this file and hand off Stage 6. Do not begin Stage 6.
```

## Stage 6 - Independent model artifacts and typed training API

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-5, and implement Stage 6 only.

Separate model definitions, inference artifacts, training configuration, training execution, experiment/sweep orchestration, checkpoints, and reporting into small modules/packages. Refactor train_pytorch_network.py, train_pytorch_network_cli.py, trainer/state, trainer/model_loader.py, trainer/train_single_run.py, trainer/io_run.py, models, and related code incrementally, preserving the user's actual model implementations.

Provide a typed train_model/training-service API returning an explicit artifact reference and training result. Programmatic callers must not construct argument lists or depend on a mutable training_directory property. Keep the train CLI thin. Use Stage 5's common samples and mature close-to-close outcomes, with chronology-aware fitting and evaluation.

Export a self-contained inference artifact with architecture, weights, ordered tickers/features, lookback, transformations, signal mode, interval, close-to-close target/execution semantics, adjustment policy, data revision, and training/information cutoffs. It must load without reconstructing SingleRun, FamilyRun, an optimizer, or an experiment directory layout. Do not serialize credentials or require directory-discovery heuristics.

Keep resumable training/checkpoint state separate from the inference contract. Record explicit ensemble members and validate feature/target/ticker semantics across members, not just input/output dimensions. Reject incompatible or incomplete artifacts; old-artifact compatibility is unnecessary. Record that old open-to-close models require retraining.

Propagate training failure and export artifacts atomically after successful validation. Keep existing execution/resource choices unless necessary for correctness; do not invent a new distributed trainer. Continue splitting large training responsibilities rather than moving their code wholesale into one file.

Acceptance: use tiny deterministic CPU fixtures to train/export/load a model, compare predictions before/after loading, verify ensemble semantic mismatches are rejected, exercise failure propagation, and prove exported inference metadata has no training-controller or credential dependency. Run relevant type checks. Update this file with APIs, artifact schema and rebuild requirements, and Stage 7 handoff. Do not begin Stage 7.
```

## Stage 7 - Current-model and retraining recommendation workflows

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-6, and implement Stage 7 only.

Replace the large choose_next_day_stocks.py application logic and duplicated run_recommender_from_model_dir.py logic with a thin recommend CLI and focused workflow, artifact-loading, prediction, eligibility, ranking, and report modules. Extract remaining private helpers into their owning packages; consumers must not import helpers from a CLI script.

Implement exactly the current and retrain model policies. Both reconcile available data and validate readiness. Validate the requested model/specification before avoidable side effects, and resolve newly ingested session IDs after ingestion. Current requires a compatible explicit artifact; retrain calls the typed training API with mature outcomes and uses the returned artifact.

Use previous_close inputs and shared Stage 5 construction. Decision timing, price availability, eligibility data, and training-label maturity must respect the same cutoff. Do not query same-session finalized transaction counts for a pre-close decision. If no available eligibility observation can satisfy a required rule, report that explicitly rather than bypassing it.

Separate pure prediction from database access, eligibility policy, ranking, and output. Return scores tied explicitly to ticker IDs and produce a structured recommendation result containing entry/exit sessions, decision cutoff, artifact identity, source revision, and strategy specifications. Keep email and diagnostic output outside prediction; do not send messages during verification.

Expose allocation policy in the request/result contracts, but leave cash-ledger-backed order planning to Stage 8. A ranked list alone must not claim to enforce the cash-rest rule. Make unsupported entry_open requests fail explicitly.

Acceptance: fake-source/trainer tests cover both complete paths, no training in current mode, one training invocation in retrain mode, custom source configuration, failure at each required stage, incompatible artifacts, unavailable current-session inputs, and exact training/inference feature parity. CLI tests check parsing and exit codes. Update this file with actual commands and Stage 8 handoff. Do not begin Stage 8.
```

## Stage 8 - Cash-rest portfolio planning and chronological simulation

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-7, and implement Stage 8 only.

Split simulate_trading.py into small simulation engine, score-source, execution, portfolio ledger, allocation schedule, event, and reporting modules. Use the same target specification, feature construction, eligibility/ranking, and recommendation workflow as the other paths. The new economics are entry close T to exit close T+1 everywhere, including percentile/ranking benchmarks.

Implement alternate_sessions and staggered_halves exactly as described in the confirmed requirements. Track positions, available cash, overnight-resting proceeds, and independent allocation balances. Sale proceeds cannot fund a same-close purchase. Split initial capital once; never rebalance A/B or transfer capital between them. Preserve the calendar anchor when inputs are missing. Make precision, rounding, per-ticker sizing, and any fees explicit; avoid expanding this into a brokerage integration.

Create a shared deterministic order planner used by simulation and capital-aware recommendation output. Ranking may operate without portfolio state, but sized plans require actual supplied capital/state. Recommendation output must not book hypothetical fills. Simulation applies the historical fills to the ledger and records each transition.

Drive simulation chronologically by decision, order, fill, sale, and next-session availability events. Use only information available at each event. Support an explicitly fixed model trained before the evaluation period and chronological retraining with mature labels. Honor retraining cadence consistently with scheduled buy decisions, record it, and prevent full-history fitting from masquerading as walk-forward results.

Handle missing entry/exit prices, suspended trading, unfilled orders, and end-of-period open positions explicitly. Do not drop losing/unpriced positions after seeing outcomes or silently redistribute their capital. Report total equity, cash categories, allocation balances, positions, realized returns, and marked equity. Cache predictions only with compatible model/data/spec/cutoff identities.

Acceptance: hand-calculated multi-session fixtures verify both schedules, A/B divergence without rebalancing, overnight restrictions, initial idle cash, holidays/nonconsecutive IDs, missing-data schedule stability, no double counting, end-period positions, and chronological label eligibility. Include a case that would incorrectly profit from same-close proceeds reuse and prove it is prevented. Demonstrate consistent model/spec use for both portfolio schedules. Update this file and hand off Stage 9. Do not begin Stage 9.
```

## Stage 9 - Frontend integration, remaining module extraction, and cleanup

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-8, and implement Stage 9 only.

Finish migrating Python callers, batch/incremental workflows, viewers, utilities, and tests to the new typed packages and CLI frontends. Inspect actual imports and entry points rather than assuming the original review's dependency list is complete. Keep the existing Java work unchanged unless a concrete shared-format change requires coordination; document such dependencies.

Remove temporary adapters, private-helper imports from command scripts, duplicate model/directory discovery, obsolete configuration parsing, old cache/artifact fallbacks, and superseded open-to-close target paths. Legacy compatibility is not required. Make every supported recommendation and simulation command state and enforce the new close-to-close semantics.

Complete the small-file decomposition: move feature inspection, random-date diagnostics, recommendation reports, email adapters, model loading, run filesystem handling, experiment orchestration, and simulation report generation into focused modules. Review the remaining large Python files and split responsibilities that still obstruct clarity; avoid a single replacement orchestrator or catch-all utils module. Large model-definition code may remain cohesive where splitting would obscure it.

Document command examples for update, read-only validation, typed training, current-model recommendations, retraining recommendations, and both portfolio schedules. Include how to identify/set the first historical session, rebuild datasets/models, inspect validation findings, and run static checks. Explicitly document the retained hardcoded API key location without its value and the optional entry_open enhancement.

Run appropriate integrated fixture-based workflows and the relevant broader test/type-check suite. Verify import-time safety, consistent errors/exit codes, absence of credential data in new artifacts, and no stale callers of removed APIs. Do not delete user model/data directories or run live production jobs as cleanup.

Acceptance: both recommender modes and both allocation schedules work through documented frontends with a shared spec; update plus validation detect and repair planned fixture gaps; fixed and retrained evaluations respect chronology; migrated code satisfies configured checks; supported internal callers use the new packages. Update all stage handoffs with final commands and known limitations. Mark main refactoring complete only if these criteria are actually met. Leave Stage 10 optional.
```

## Stage 10 - Optional entry-session opening-price signal

```text
Read REFACTORING_PROMPTS.md completely, follow its shared working rules, verify Stages 1-9, and implement optional Stage 10 only.

Implement entry_open as an alternative feature specification to previous_close. Keep the same completed-session closing-price history and add entry-session opening information across the model's full ordered ticker universe. Purchases still execute at that session's close; sales still execute at the next session's close.

Inspect provider capabilities and implement a focused way to acquire the entry session's opening observations before the recommendation cutoff. Do not wait for or read the completed daily bar, synthesize unavailable openings, or require a full intraday ingestion redesign. Record availability/provenance and make missing or not-yet-opened ticker handling explicit without changing the artifact's ticker layout opportunistically.

Persist the exact opening-price transformation, feature order, availability requirements, and model input mode. Train distinct compatible artifacts for previous_close and entry_open; never toggle input semantics on an existing model. Extend coverage, validation, cache identity, and artifact compatibility checks for these observations.

Use identical target/execution rules and cash-rest schedules in both modes. Provide a reproducible comparison using the same evaluation sessions, initial capital, ticker-universe policy, training budget/cadence, and recorded seeds where applicable. Fit preprocessing within each training cutoff, and report any sessions excluded for missing opening data so the comparison is not biased by different hidden samples.

Acceptance: deterministic fixtures prove openings can affect selection but not fill timing, future daily data is inaccessible, missing opening observations follow the declared policy, model/spec mismatches are rejected, and both allocation schedules work with both signal modes. Run a small chronological comparison and record commands/results; do not launch a full production experiment. Update stage status and the final handoff in this file.
```

## Stage handoffs

Maintain an entry for each executed stage using this template:

```text
Stage:
Status:
Implemented paths and public APIs/commands:
Decisions and contract/schema changes:
Checks run and results:
Pre-existing failures or remaining limitations:
Data/model rebuild or migration requirements:
Next stage and concrete prerequisites:
```

### Stage 10 integration addendum - 2026-10-15

Stage 10 is now complete, following verification of the committed Stage 9 checkout. The Stage 1-9 handoffs and their integration addendum remain historical records: their statements that `entry_open` is unsupported, optional-but-deferred or not implemented are superseded by this addendum and the final Stage 10 handoff. Both `previous_close` and `entry_open` are supported with distinct explicit feature/artifact contracts; execution remains close T to next-session close T+1 with mandatory overnight cash rest. Current opening acquisition, availability requirements, commands and the recorded matched-cohort comparison are documented in `docs/entry_open.md`. Final verification is **716 tests passed**, including **56 Stage 10 tests**, and strict mypy passes on **204 source files**. Stages 1-9 remain complete; no further numbered stage is required.

### Integration addendum for Stage 1-8 handoffs - 2026-10-15

The numbered handoffs below remain historical implementation records. Current package commands, installation options, the exact source-history/publication requirements, rebuild instructions, report schemas, checks and limitations are consolidated in `docs/frontends.md`. Stage 9's handoff records final completion: **660 tests pass**, strict mypy passes on **191 source files**, all supported Python callers are migrated and the reviewed source cleanup is complete. Main refactoring (Stages 1-9) is **complete**; Stage 10 remains optional and deferred. Earlier commands referencing retired files and statements about missing Torch, legacy adapters, baseline failures or the next stage not having started describe their historical stage boundary, not the current checkout.

- Stages 1-2: strict mypy still checks all `src/stockguidance`; source-history provenance, XNYS chronology, explicit timestamps and daily-only semantics are unchanged. The configured Python base-runtime workaround still applies. `database/connections.py` now supplies `MySQLConnectionFactory` directly; the package's temporary legacy connector adapters are removed. Explicit database JSON replaces parent-directory/default configuration discovery.
- Stages 3-4: `python -m stockguidance.cli.update_market_data` and `python -m stockguidance.cli.validate_database` remain the supported frontends, with the exact evidenced policy flags in `docs/frontends.md`. Read-only validation and explicit automatic-repair plans retain their 0/1/2 semantics. The updater now shares lazy `ConfiguredPolygonProvider` assembly with recommendations. Existing `legacy-open-to-close-v1` derived-table maintenance/auditing remains explicitly a storage-consistency responsibility, independent of the new model target; no table/Java semantics are silently changed. Numerical close-to-close targets are computed from verified raw closes. The live history/publication boundary is still unknown until operator evidence is supplied.
- Stages 5-6: shared sample/artifact/checkpoint contracts are unchanged. Training JSON/configuration code moves from `cli/training_configuration.py` to `config/training.py`, and jobs/history assembly moves from `cli/training_job.py` to `training/jobs.py`. `python -m stockguidance.cli.train_model` remains the training command. New read-only `python -m stockguidance.cli.inspect_features --config ...` and `--kind correlations` inspect those exact samples, retain unavailable/mature-label boundaries and record identities. Old datasets/models/caches still require rebuilding. Package import safety and independent artifact credential/controller isolation remain checked.
- Stages 7-8: recommendation/simulation JSON loaders move to `recommendations/jobs.py` and `simulation/jobs.py`; shared dependency assembly moves to `workflows/recommendation_runtime.py` and `workflows/simulation_runtime.py`, with typed options in their owning packages. Their CLI modules construct those options. Both recommendation modes and both portfolio schedules retain the same specs/cutoffs, independent balances, overnight proceeds restriction and missing-exit behavior. `python -m stockguidance.cli.incremental` enforces chronological retraining. Reports add schema IDs `stockguidance-training-1` and `stockguidance-recommendation-1`; simulation remains `stockguidance-simulation-1`. `python -m stockguidance.cli.reports` reads explicit reports/artifacts and rejects legacy/incompatible economics. `python -m stockguidance.cli.batch` executes typed steps directly, propagates actual artifact references and stops on failure/incomplete simulations.
- Final check command forms for every preceding stage: use the IDE-discovered configured executable and the documented runtime workaround; run `python -m pytest tests -q --tb=short --maxfail=5`, `python -m mypy`, `git diff --check`, and `git diff --cached --check`. Full results: **660 passed**, three known Torch optimization warnings, **129.37 seconds**; strict mypy: **191 files**, no issues. `tests/stage9/test_retired_frontends.py` adds 24 passing delegation/retirement/isolated-wheel checks. No legacy tests are hidden by pytest exclusions. The requested grouped deletion report is `docs/stage9_retirement.md`: 85 source/test deletions, 21 retained thin frontends and one retained credential module. Native provider/MySQL/CUDA/production-scale performance remain outside fixture verification; no live database/history/model/data, Java code, email or trades are changed. Historical revisions overwritten by current manifests remain unrecoverable, and `entry_open` stays optional Stage 10.

### Stage 1 handoff - 2026-10-15

**Final integration:** Stage 1's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 2 has not started. No existing trading, updating, training, inference, or Java implementation was migrated.

**Implemented paths and public APIs/commands:**

- `pyproject.toml`: setuptools `src` packaging for distribution `StockGuidance`, Python >=3.13, no runtime dependencies, optional `dev` dependencies (`pytest>=8.3`, `mypy>=1.15`), pytest source-path configuration, and strict mypy checking of `src/stockguidance` only. No pre-existing packaging or configured static checker was found. `.gitignore` now excludes generated `*.egg-info/` metadata.
- `src/stockguidance/__init__.py`, `src/stockguidance/py.typed`, and focused subpackage initializers: safe package imports and an installed typing marker. The new package imports no legacy application, database, provider, NumPy, or Torch modules.
- `src/stockguidance/config/source.py`: `SourceIdentity`, `PriceAdjustment`, `SourceConfig`; `src/stockguidance/config/validation.py`: runtime scalar configuration checks. Source identity records provider, endpoint, market, universe, adjustment, and interval. Runtime paths and a credential reference are separate; no credential value was copied or introduced.
- `src/stockguidance/calendar/sessions.py`: `TradingSession`, `require_next_session`; `src/stockguidance/calendar/decisions.py`: `DecisionTiming`.
- `src/stockguidance/market_data/intervals.py`: `ObservationInterval`; `src/stockguidance/market_data/requests.py`: `MarketDataRequest`; `src/stockguidance/market_data/availability.py`: `ObservationTiming`.
- `src/stockguidance/features/specification.py`: `FeatureSpec`, `SignalMode`, `MissingHistoryPolicy`; `src/stockguidance/targets/specification.py`: `TargetSpec`, `HoldingHorizon`, `ExecutionPrice`, `TargetTransform`; `src/stockguidance/targets/availability.py`: `LabelTiming`.
- `src/stockguidance/models/policy.py`: `ModelRefreshPolicy`, `ModelSelection`; `src/stockguidance/portfolio/policy.py`: `AllocationPolicy`, `AllocationSpec`, `CashRestRule`; `src/stockguidance/portfolio/availability.py`: `SaleProceedsTiming`.
- `src/stockguidance/recommendations/request.py`: `RecommendationRequest`; `src/stockguidance/recommendations/result.py`: `RankedTicker`, `RecommendationResult`; `src/stockguidance/validation/coverage.py`: `CoverageState`, `SessionCoverage`, `CoverageResult`; `src/stockguidance/workflows/failures.py`: `WorkflowStage`, `FailureCode`, `StageFailure`, `StageExecutionError`.
- `tests/stage1/conftest.py`, `tests/stage1/test_policies.py`, `tests/stage1/test_timing.py`, `tests/stage1/test_results.py`, `tests/stage1/test_imports.py`: deterministic contract, availability, policy, provenance, and import-safety fixtures without live services.
- PyCharm's automatic pyproject import registered the new source root in `.idea/stockguidance.iml` and updated `.idea/modules.xml`; the IDE also staged the case-only rename from `.idea/StockGuidance.iml`. These are IDE-generated packaging integration changes. The pre-existing `.idea/encodings.xml` change and all existing Python/Java edits were preserved. No commit or branch was created.

**Decisions and contract/schema changes:**

- Configuration/result dataclasses are frozen and collection fields use validated tuples. External enum strings must be explicitly parsed with their owning enum, such as `ModelRefreshPolicy("current")`; constructors reject unparsed strings, invalid values, naive timestamps, mutable ticker/ranking layouts, duplicate tickers, and non-finite scores. No numerical array boundary has been introduced; Stage 5 must validate its actual arrays and transformations.
- `TradingSession.sequence` is a calendar-supplied chronological ordinal with a stable origin for each `calendar_id`, never a database ID. Session contracts validate adjacent ordinals and increasing dates/timestamps. They do not supply an authoritative exchange calendar. `DecisionTiming` requires consecutive previous/entry/exit sessions, a decision at or after the prior close and strictly before the entry close, entry at T's close, and exit at T+1's close. Early decisions before the entry session opens are permitted.
- A completed daily observation is observed at its session close and cannot be published before that close. `available_at` remains separate from observation and fill times. `FeatureSpec.validate_observation` requires publication by the decision cutoff and a session inside the specified prior-session lookback. `LabelTiming.is_mature_at` requires both entry and exit prices to have become available by the training cutoff; a T-1 entry is immature before T's close/publication.
- `previous_close` means historical closing-price inputs only. Missing history is unavailable. Stage 5 still owns precise transformations, fitted preprocessing, feature order, array layout, and sample construction. `entry_open` is an enum reservation and raises an explicit Stage 10 unsupported error. Hourly/five-minute interval identities are also reserved and rejected by executable daily contracts.
- The target is explicitly raw `close(T+1) / close(T) - 1`, with close fills and a next-trading-session horizon. Ranked/normalized targets and legacy open-to-close semantics are not accepted. Numerical target calculation remains Stage 5 work.
- `current` requires an explicit existing artifact ID. `retrain` takes no existing artifact ID and must report the resulting ID later. Requests validate source identity, interval, cutoff, and allocation-anchor consistency. Results preserve the request/specifications, actual artifact ID, data revision, and descending scores tied to ticker IDs. Current results cannot silently substitute another artifact.
- Allocation policy is independent of model/target semantics. Both allocations have a two-session buy cadence per independent balance, anchored to an explicit session. `staggered_halves` means a single initial A/B split with independent proceeds and no rebalancing or transfers. `CashRestRule` has only `next_session_after_sale`; same-close reuse is not configurable. `SaleProceedsTiming` rejects same-session eligibility. These are contracts, not a portfolio ledger or an order planner; ranked recommendations do not claim fills or cash-rest enforcement.
- Coverage states distinguish complete, missing, incomplete, inconsistent, not-yet-available, and unverified assessments. `assessed_sessions_complete` intentionally describes only supplied assessments; empty assessments do not establish readiness, and future daily closes cannot be marked complete. It is not proof of full requested/provider coverage. Stage 2 must establish expected sessions; Stages 3-4 supply reconciliation and validation evidence. No history start was guessed or persisted in Stage 1.
- The retained hardcoded provider key is `DEFAULT_API_KEY` in `polygon_fetch.py` (line 27 at inspection). It should eventually become external configuration; its value is absent from new code, documentation, artifacts, and logs.

**Interpreter, setup, and check commands:**

IDE discovery selected Python 3.13.9 at `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The venv's `pyvenv.cfg` refers to a removed `C:\Users\formm\AppData\Local\Programs\Python\Python313` base installation. Unmodified startup fails with `ModuleNotFoundError: No module named 'encodings'`, both inside and outside the sandbox. No interpreter was switched and no venv configuration was replaced.

To complete checks with that exact configured executable, matching Python 3.13.9 standard-library/extension files were downloaded from the official Python distribution into temporary storage. This process-only workaround was used for all successful commands below. It does not permanently repair the missing base installation. Recreate the temporary files if they have been cleaned up:

```powershell
Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.9/python-3.13.9-embed-amd64.zip' -OutFile (Join-Path $env:TEMP 'stockguidance-python3139-stdlib.zip')
Expand-Archive -LiteralPath (Join-Path $env:TEMP 'stockguidance-python3139-stdlib.zip') -DestinationPath (Join-Path $env:TEMP 'stockguidance-python3139-stdlib') -Force
$stage1Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage1Runtime
$env:PYTHONPATH = "$stage1Runtime\python313.zip;$stage1Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

The downloaded archive's SHA256 was `91d828c2da3a029b41699e918674a0cb379c02cf20dab9c501306885f837402a`.

After IDE environment discovery before each invocation, setup/check commands run from the repository root were:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pip install 'setuptools>=68' 'mypy>=1.15'
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pip install --no-build-isolation --no-deps -e .
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/test_simulate_trading_utils.py tests/test_feature_vector_builder.py -q
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 -q
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/test_simulate_trading_utils.py -q
git diff --check
```

With a healthy configured interpreter, the normal development setup is `& '<configured-python>' -m pip install -e '.[dev]'`; no temporary runtime overrides should be needed. Continue following Python-environment discovery before toolchain commands in subsequent stages.

**Checks run and results:**

- Legacy behavioral baseline on the unchanged implementation: **4 passed, 7 failed**. All four feature-window utility tests pass. All seven `tests/test_feature_vector_builder.py` tests fail because callers unpack two values from `FeatureVectorBuilder.build_for_date`, which currently returns more values. These failures predate the new contracts and were not repaired.
- New Stage 1 suite: **67 passed**. Tests cover explicit enum parsing, runtime value validation, immutable specifications, both model/allocation policies, unsupported modes, pre-close decisions, publication delays, immature labels, next-session/cash-rest constraints, request/result mismatches, coverage distinctions, and structured failures. Import checks use an empty temporary working directory, guard application file/network/subprocess access, and import every new module without output or legacy/heavy dependencies.
- Final focused suite including the four passing legacy utility tests: **71 passed**.
- Strict mypy **2.4.0**: **no issues in 28 source files**. No blanket suppressions or checker exceptions were added.
- Editable installation with setuptools **84.0.0** succeeded. A separate smoke command executed from the system temporary directory, without `src` in `PYTHONPATH`, imported the installed package from `src/stockguidance/__init__.py` and confirmed its `py.typed` marker.
- `git diff --check` passed; Git also reported existing LF/CRLF notices for unrelated user-modified files. No live database, production training job, market-history download, email, or trade was used for verification.

**Pre-existing failures or remaining limitations:**

The missing base Python installation remains an environment issue; the temporary workaround must be repeated or the configured environment repaired before ordinary commands can run. The environment also lacks Torch. Installing the necessary build/type-check dependencies reported pre-existing incomplete Streamlit/Scalene dependencies and Streamlit's incompatible pandas constraint; these unrelated packages were not repaired. A full legacy test run was not attempted because the inspected suite includes database/model-dependent paths and the focused baseline already records relevant failures. Authoritative calendars, supported-history provenance, persistence, coverage plans, full request readiness, feature transformations, artifacts, workflows, and portfolio execution remain assigned to later stages.

**Data/model rebuild or migration requirements:**

Stage 1 creates no datasets, caches, or model artifacts and changes no legacy inputs or outputs. Nothing needs rebuilding to use these contracts. Later close-based dataset and inference-artifact stages must rebuild legacy open-to-close data/models rather than silently treating them as compatible.

**Next stage and concrete prerequisites:**

Implement **Stage 2 only** next. Verify these contract tests/imports/type checks using the configured environment and account for the documented base-runtime problem. Select an authoritative exchange calendar and provide stable session ordinals plus timezone-aware actual opens/closes, including holidays, DST, and early closes. Inspect `choose_next_day_stocks.py`, `trainer/util/date_bounds.py`, `database/database_date_id_queries.py`, and related loaders before extracting their behavior. Establish the source/request-specific first supported session from real evidence, persist it with provenance separately from observed storage minima, and extend the coverage contracts into bounded calendar-driven plans. No authoritative historical boundary or calendar implementation is supplied by Stage 1.

### Stage 2 handoff - 2026-10-15

**Final integration:** Stage 2's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 3 has not started. Calendar, bounded read-only coverage planning, persisted history metadata, and temporal feature/label window APIs are implemented. The legacy updater, trainer, and recommendation CLI are not fully migrated and must not be described as enforcing the new strategy.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/calendar/clock.py`: injectable `Clock`, `SystemClock`, and `decision_cutoff`; `calendar/protocols.py`: `SessionCalendar`; `calendar/exchange.py`: lazy `ExchangeCalendar`; `calendar/resolution.py`: `resolve_decision` and `latest_finalized_session`. All calendar paths in this entry are under `src/stockguidance/`.
- `src/stockguidance/calendar/date_bounds.py`: pure `DateBoundResolver` and `DateBoundType` operate on supplied, bounded, chronological `StoredSession` rows. Relative bounds and offsets use row chronology, not numeric ID arithmetic or a global database latest date. Stored neighbors do not prove exchange adjacency across missing records.
- `src/stockguidance/market_data/history.py`: `HistoryBoundary`, `HistoryProvenance`, `UnknownHistoryBoundary`, and `establish_history_boundary`; `src/stockguidance/market_data/history_store.py`: `JsonHistoryStore.save/load/path_for` with atomic, schema-versioned JSON metadata keyed by the full non-secret source/request identity.
- `src/stockguidance/market_data/publication.py`: `DailyPublicationPolicy`; `src/stockguidance/market_data/coverage.py`: `UpdateAction`, `SessionUpdate`, `CoveragePlan`; `src/stockguidance/market_data/coverage_planning.py`: `plan_coverage(source, request, boundary, calendar, publication, repository, clock)`. Plans expose expected sessions, fetch/repair/verification actions, finalized versus expected-published sessions, actual available observation timings, in-scope observed storage bounds, and boundary clipping separately. No update execution or new CLI is introduced.
- `src/stockguidance/database/dates.py`: `normalize_session_date` and `normalize_date_id`; `src/stockguidance/database/repositories/sessions.py`: validated `SessionQuery`, `StoredSession`, and `SessionRepository`; `src/stockguidance/database/repositories/mysql_sessions.py`: `MySQLSessionRepository` and typed connection/cursor protocols. New database package initializers are import-safe. Queries are parameterized, read-only, use exact authorized calendar dates, and close resources on failure.
- `src/stockguidance/database/legacy_connections.py`: temporary, lazy `LegacyMySQLConnectionFactory` forwards the explicit configured database path to the existing connector without importing its drivers until invoked. Remove this migration adapter by Stage 9.
- `src/stockguidance/features/history_window.py`: `plan_feature_history`, `FeatureHistoryWindow`, `WindowState`; `src/stockguidance/targets/training_window.py`: `plan_training_history`, `TrainingHistoryWindow`. These plan temporal eligibility only; numerical feature construction, price validation, and close-to-close target calculation remain Stage 5.
- Surgical legacy integration: `choose_next_day_stocks.py` imports the new driver-date normalizer and resolves an explicit target's date ID after raw ingestion. `trainer/util/date_bounds.py` now requires and forwards `dbinfo_file` for every query. Its callers in `trainer/state/single_run.py`, `calculate_ticker_correlations.py`, and `database/query_tickers_by_date.py` supply their configured source. Other existing edits in these files were preserved.
- `pyproject.toml` adds runtime dependencies `exchange-calendars>=4.11,<5` and `tzdata>=2024.1`. `tests/stage2/conftest.py`, `test_calendar.py`, `test_history.py`, `test_coverage_planning.py`, `test_database_sessions.py`, and `test_history_windows.py` supply deterministic fake-source/database checks.

**Decisions and contract/schema changes:**

- Calendar choice is `exchange_calendars` **XNYS** for the US-equities daily session schedule, with `America/New_York` exchange dates and aware UTC opens/closes. Actual installed versions are **exchange-calendars 4.13.2** and **tzdata 2026.5**. Caller-supplied loaded calendar bounds must include required lookback/entry/exit sessions; out-of-range access fails rather than silently widening a request. Calendar creation is explicit and potentially loads library data; module import does not load it. The calendar version is recorded in each coverage plan.
- Session ordinals count exchange sessions relative to **1970-01-01** and remain stable across different loaded ranges for the same calendar version. This is only an ordinal origin, not a provider-supported history date. Calendar schedule generation for ordinal calculation is not a provider or database data request. Holiday/early-close/DST handling comes from the established calendar, not a fixed UTC close constant.
- **No authoritative first supported session for the live source was found.** The inspected provider's `years_back=2` request default and old diagnostic date literals are not evidence of supported history. No production boundary was invented, inferred from current database minima, or persisted. Callers must establish a boundary from inspected configuration, source-history evidence, or an explicit supplied limit, with `HistoryProvenance` and nonempty evidence. `plan_coverage(..., boundary=None, ...)` raises `UnknownHistoryBoundary` before database/provider work rather than claiming readiness.
- `JsonHistoryStore(directory)` persists schema version 1, full source identity, calendar ID, `history_start`, provenance, evidence, and aware establishment timestamp. Its SHA256 filename includes provider, endpoint, market, universe, adjustment, and interval, excluding credential/configuration paths. Save is atomic and interrupted replacement preserves the prior file. Missing metadata returns `None`; corrupt, unsupported, or mismatched metadata fails. Validate a loaded boundary against the chosen source/calendar before use. Observed first/last rows are separate, explicitly in-scope diagnostics, not evidence of the supported lower bound.
- Publication delay must be explicitly supplied with provenance. An unknown schedule yields unverified coverage and authorizes no provider fetch or database query. A known expected publication is not proof of actual receipt, data validity, or provider completeness. Tests use an explicit **fixture-only** 15-minute post-close policy and **fixture-only** history start of 2024-07-01; neither is a live provider assertion. Stage 3 must establish the real publication/correction policy and persist actual availability evidence.
- `plan_coverage` checks source identity and the injected clock, enumerates the entire requested supported calendar coverage, clips future dates, and defers unfinished/unpublished daily sessions. Only expected-published completed sessions enter `SessionQuery`. Missing available sessions are fetched by calendar date without requiring existing database IDs; incomplete/inconsistent sessions are repair proposals and legacy-unverified rows require verification. Re-read after ingestion to obtain new IDs. A previously stored bar with actual availability later than the historical cutoff remains unavailable.
- The legacy MySQL schema alone cannot prove provider scope or publication. Its adapter reports present raw rows as **unverified** and a date with zero raw rows as **missing**, never complete merely from counts. `StoredSession(COMPLETE, ...)` requires an actual availability timestamp; subsequent source-manifest/validation stages own completeness evidence. Exact date predicates avoid assuming IDs are ordered or consecutive and detect leading/interior gaps even after later successes. Repository results outside the bounded query or with duplicate dates/IDs are rejected.
- `plan_feature_history` exposes the first usable entry after the full prior-session lookback. Earlier entries have an empty unavailable window, never earlier provider requests or zero-filled history. Expected publication readiness remains distinct from actual feature-data readiness. `plan_training_history` anchors to the requested historical decision, separates expected mature entries from actual mature price timings, excludes T-1 entries before T's exit close/publication, and exposes missing mature prices without silently shifting the selected history farther backwards. Future observations in storage do not move a historical window.

**Interpreter, setup, and check commands:**

IDE discovery still selects Python **3.13.9** at `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The removed base installation and missing Torch remain unchanged. All successful Stage 2 commands used the same process-local matching-runtime workaround documented in Stage 1, with no venv replacement or interpreter switch:

```powershell
$stage2Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage2Runtime
$env:PYTHONPATH = "$stage2Runtime\python313.zip;$stage2Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

After IDE environment discovery before each Python invocation, Stage 2 setup/check commands were:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pip install 'exchange-calendars>=4.11,<5' tzdata
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/test_simulate_trading_utils.py -q
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=3
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
git diff --check
```

**Checks run and results:**

- Verified Stage 1 prerequisites before implementation: **71 tests passed**, strict mypy passed on **28 source files**.
- Final focused suite: **136 passed** (**67 Stage 1**, **65 Stage 2**, **4 legacy window utilities**). The all-package import-safety check also covers every new module from an empty working directory without application file, network, subprocess, or heavy-library import side effects.
- Calendar fixtures cover July 4 closure, July 3's 17:00 UTC early close, regular closes, spring DST, post-fall-DST early closing, stable ordinals, and next-session resolution. History/coverage fixtures cover supported starts, leading/interior gaps, partial imports, empty storage with persisted metadata, deferred ID resolution, historical cutoffs, unknown boundaries/publication policy, actual late availability, invalid metadata, interrupted metadata saves, and requests wholly before supported history.
- Window fixtures demonstrate insufficient lookback, exact prior-session windows, historical targets despite future stored observations, missing mature outcomes without window substitution, and immature/late-published exits. Repository fixtures use nonmonotonic IDs (901, 11, 450), driver-specific dates, custom config paths, invalid/duplicate rows, cleanup on failure, and legacy config forwarding without a connection.
- Strict mypy **2.4.0**: **no issues in 46 source files**, without new suppressions or checker exceptions. A separate `ast.parse` check passed on all five touched legacy Python modules without importing their model/database dependencies.
- `git diff --check` passed; new Stage 2 files also passed per-file `git diff --no-index --check` comparisons. Git reported LF/CRLF normalization notices, not whitespace errors. Existing user Python, Java, data/model, and IDE changes were preserved. Verification used no live database, production training, market-history download, email, or trade.

**Pre-existing failures or remaining limitations:**

The seven Stage 1 baseline failures in `tests/test_feature_vector_builder.py` remain unrelated (two-value callers versus the current three-value builder return); they were not repaired or rerun as Stage 2 acceptance. Dependency installation again reported existing incomplete plotting/Streamlit dependencies and Streamlit's pandas constraint; those packages were not changed. The configured Python base-runtime problem still requires the temporary workaround or user repair. Full legacy CLI execution was not used as verification.

`database/database_date_id_queries.py`, old loaders, the legacy trainer date resolver's numeric APIs, and remaining date logic in `choose_next_day_stocks.py` are isolated legacy paths, not calendar-driven new workflows. Their fixed UTC cutoff/global-latest assumptions are not inherited by the new APIs. Complete updater extraction belongs to Stage 3, numerical datasets/targets to Stage 5, training consumers to Stage 6, recommendation orchestration to Stage 7, and remaining legacy cleanup to Stage 9. No old feature/target convention is connected to the new timing planners or represented as a new-compatible artifact. This stage proposes work; it does not claim source reconciliation, full readiness, or correction handling is implemented.

**Data/model rebuild or migration requirements:**

No production metadata, raw data, schema, dataset, cache, or model artifact was changed. No rebuild is needed for the new calendar/planning APIs themselves. Later close-based datasets/models must be rebuilt rather than silently reusing open-to-close semantics. Persist an evidenced source boundary before invoking new production coverage planning.

**Next stage and concrete prerequisites:**

Implement **Stage 3 only** next. Verify the Stage 1/2 tests and static checks using the configured environment. Establish or load an evidenced source/request-specific history boundary and select an explicit provider publication/correction policy; report unknowns instead of using database minima or request defaults. Assemble `ExchangeCalendar`, the injected clock, configured repository, and `plan_coverage` into the complete recoverable update workflow. Add source manifests/raw metadata that can prove request-scoped completeness and actual availability, atomic validated downloads, idempotent transactional ingestion, correction/dependent-data refresh, and failure propagation. Re-read date IDs after successful ingestion, carry custom roots/database configuration throughout, and do not interpret provider omissions as exchange closures. Retain the hardcoded key location already documented in Stage 1 without copying its value. Do not begin validation CLI, numerical feature construction, training, or recommendation migration prematurely.

### Stage 3 handoff - 2026-10-15

**Final integration:** Stage 3's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 4 has not started. The supported updater reconciles complete declared request coverage, persists source evidence, recovers interrupted imports, and refreshes corrections. The recommendation frontend delegates only its refresh step; its remaining model/training/recommendation semantics are still legacy and are not represented as the completed new strategy.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/market_data/providers/contracts.py`: typed `MarketDataProvider`, `ProviderResponse`, `ResponseState`, and `DownloadedSession`; `providers/polygon.py`: `PolygonGroupedProvider`, `HttpTransport`, `HttpResponse`, `GROUPED_ENDPOINT`, and `GROUPED_UNIVERSE`; `providers/http.py`: credential-redacting `PolygonHttpTransport` with bounded transient retries and throttling; `providers/__init__.py` is import-safe. All shortened provider paths in this entry are relative to `src/stockguidance/market_data/`.
- `src/stockguidance/market_data/records.py`: validated `DailyBar`, provider/serialized record decoding, fixed database price precision, source timestamp/session validation, and `records_id`; `identity.py`: canonical non-secret source/content identities and JSON decoding boundaries; `atomic.py`: validated-content atomic replacement with flush/fsync and temporary-file cleanup.
- `src/stockguidance/market_data/manifests.py`: `ManifestStore`, `SourceManifest`, `InvalidManifest`, normalized response encoding/decoding, and independent import/derived completion markers. `save_download`, `load`, `mark_imported`, and `mark_complete` are public persistence operations.
- `src/stockguidance/market_data/corrections.py`: explicit `CorrectionPolicy`; `update_planning.py`: `plan_updates`, `UpdateWork`, `SnapshotCoverageReader`, `SnapshotIndex`, and shared `reconciled` readiness assessment; `dependencies.py`: `dependency_scope`/`DependencyScope` for bounded neighboring-session context; `ingestion.py`: `import_if_needed` and `derived_repairs`.
- `src/stockguidance/market_data/legacy_derived.py`: explicit `LEGACY_DERIVED_SPEC = "legacy-open-to-close-v1"`, `DERIVED_COLUMNS`, `LegacyDerivedRow`, `legacy_rows`, and `derived_id`. This extracts existing derived-table calculations and transformations without introducing Stage 5's numerical feature/target implementation.
- `src/stockguidance/database/repositories/market_data.py`: typed `MarketDataRepository`, `SessionSnapshot`, `ImportReceipt`, and `DerivedUpdate`; `mysql_market_read.py`: bounded, driver-normalized snapshot reads; `mysql_market_data.py`: `MySQLMarketDataRepository.inspect/import_session/replace_derived` with explicit transactions, exact key replacement, batched inserts, receipts, and revision checks. Cached per-snapshot hashes and indexed chronology avoid repeatedly hashing full history for each dependency; each derived write reads only its immediate session context.
- `src/stockguidance/database/write_connections.py`: typed write connection/cursor protocols and temporary lazy `LegacyWriteConnectionFactory`; `database/update_schema.py`: explicit `initialize_update_schema`, source binding, and read-only InnoDB checks. No existing table is altered or automatically converted. Shortened database paths in this entry are under `src/stockguidance/`.
- `src/stockguidance/workflows/update_market_data.py`: typed `UpdateSettings` and `update_market_data(source, settings, boundary, calendar, provider, repository, clock)`; `update_result.py`: immutable `UpdateResult`; `update_metadata.py`: atomic, credential-free run/policy metadata. The initial coverage plan remains a work proposal; `result.coverage`, `result.failures`, and `result.successful` describe the reconciled result.
- `src/stockguidance/cli/update_arguments.py`, `cli/update_setup.py`, `cli/update_market_data.py`, and import-safe `cli/__init__.py`: argument validation, explicit dependency assembly, JSON output, and exit mapping. Run `& '<configured-python>' -m stockguidance.cli.update_market_data ...` or `& '<configured-python>' update_market_data.py ...`. Root `update_market_data.py` is a thin wrapper. `pyproject.toml` declares the `update_market_data` console entry point, available after refreshing the editable installation; no dependency installation was required during Stage 3.
- `choose_next_day_stocks.py`: removes the old refresh block, filename/latest-row range helpers, incremental raw CLI invocation, and numeric-ID-based derived refresh. It delegates refresh dependency assembly to the shared CLI setup, forwards the selected data root/source/bounds/policies, stops on incomplete or failed refresh, and resolves requested date IDs only after successful ingestion. The unrelated recommendation/training/user edits are preserved. Standalone old provider/population entry points remain isolated for Stage 9 cleanup and are not the supported reconciler.
- `tests/stage3/conftest.py`, `test_update_workflow.py`, `test_provider.py`, `test_http_transport.py`, `test_persistence_and_boundaries.py`, `test_update_cli.py`, and `test_legacy_update_delegation.py`: deterministic fixture coverage. `docs/market_data_updating.md` documents setup, exact scope, policy arguments, evidence/revision schemas, exit codes, recovery, migration limits, and commands.

**Decisions and contract/schema changes:**

- The live first supported session and publication schedule remain **unknown**, as documented by Stage 2. No date or publication delay was invented and no production boundary was persisted. The command requires an existing source-specific `JsonHistoryStore` boundary or explicit `--history-start` plus `--history-evidence`; conflicting established boundaries fail. Publication delay/provenance and recent/archive correction intervals/provenance are required arguments. Test dates and the 15-minute test delay remain fixture-only values. See `docs/market_data_updating.md` for the command template and required evidence.
- The concrete Polygon request is `/v2/aggs/grouped/locale/us/market/stocks/{date}`, daily interval, market `us-stocks`, explicit universe **`us-stocks-excluding-otc`**, and explicit HTTP `include_otc=false`. This avoids claiming OTC coverage from the grouped endpoint's default behavior. Adjustment is configurable and validated against the response. Full source identity, including adjustment/interval/universe, keys manifests and boundaries; generic Stage 2 fixture identities do not constitute this live source's history evidence.
- Coverage enumerates all supported requested exchange sessions rather than relying on file maxima or ID arithmetic. Download eligibility uses XNYS actual closes, an evidenced publication expectation, and the injected clock. Actual receipt time is recorded separately; downloading historical dates today does not establish historical availability. This is a live updater API with no historical `as_of` materialization option. A final coverage pass prevents newly due sessions from being silently omitted when publication occurs during a run.
- Manifest schema **1** records source/request scope, calendar/session, observed close and actual receipt timestamps, response content identity, normalized financial-content identity, exact record keys/count, and import/derived completion. Content-addressed normalized response envelopes retain HTTP/provider status, available count fields, continuation presence, per-key provider timestamps, and approved financial fields. Unrecognized payload fields, error bodies, and continuation URLs are deliberately excluded. `load` validates scope, hash, shape, counts, keys, and flags; corrupt/missing evidence is repaired by fetching rather than trusting a filename. Raw response content and manifests are replaced atomically only after validation.
- A positive, validated grouped response requires exact `resultsCount`, valid unique records, matching adjustment, successful status, and no continuation. Missing count evidence, unexpected emptiness, and continuation remain **unverified**, with no import. Pagination is not implemented for this endpoint; its presence prevents completeness. Explicit delayed empty responses retain **not_yet_published** metadata. Only a trusted adapter with authoritative evidence can return **no_trading**; Polygon never infers it from emptiness. Exchange closures come only from the calendar. Provider/HTTP/malformed-response failures remain structured failures even if later sessions succeed.
- Each database is bound to one full source identity by `stockguidance_source`; a different source/adjustment requires a separate database. `stockguidance_imports` stores per-session raw revision/actual availability plus derived dependency/content identities. Existing legacy rows have no such evidence and are unverified until reconciled. Explicit `--initialize-schema` creates missing InnoDB tables without altering existing tables; it is never invoked by read-only inspection. Every updater transaction checks the expected six InnoDB tables and source binding. The retained connector's `autocommit=True` is countered by explicit `START TRANSACTION`, commit on full success, and rollback/cleanup on failure. Nontransactional storage is rejected before downloads or writes.
- A raw transaction replaces exactly one source session, including withdrawn keys, ticker/date mappings, and its receipt. Derived data is repaired separately, with explicit completion markers and recovery from already validated downloads. Database keys and actual financial content are compared, so plausible counts or stale completion flags cannot hide missing or incorrect records. Price fields are normalized to the existing four-decimal storage contract; source counts must be nonnegative integral values rather than silently rounded or clamped.
- Derived updates overwrite stale non-null values and recompute all existing next-session outcome transformations. A correction affects its own rows, its chronological predecessor's next-session outcome/transforms, and its successor's prior ratios. Two-session context allows narrow correction requests to repair verified adjacent sessions without clearing their other dependencies. Context queries never precede supported history or include unpublished/future sessions. Missing history/ticker/dependency observations remain null, never synthetic zero returns. The existing open-to-close columns remain explicitly **legacy**; no old model or dataset is relabeled close-to-close.
- `data_revision` fingerprints source/interval identity, normalized raw content, actual stored derived content, adjacent dependency identities, and the explicit derived spec. Corrections invalidate this identity; unchanged rechecks retain it. New caches must include this revision plus their relevant feature/target/model/cutoff specifications. Existing legacy caches have no compatibility proof and are not claimed to participate in the new staged contract. Stage 5 and later consumers must incorporate the revision rather than falling back to those caches.
- JSON output exposes assessed sessions, source-response states/counts, source scope, history bounds, closures, deferred/unverified coverage, revision, and sanitized failures. Exit **0** means all required currently available data in scope is reconciled (explicitly deferred sessions remain visible); an entirely closed calendar range can succeed without claiming data exists. Exit **2** indicates unverified/incomplete coverage or no available trading data; argparse also uses 2. Required provider/storage/setup/persistence failures use exit **1**. Persisting completion/run metadata is required and cannot fail while returning success.
- The existing key remains `DEFAULT_API_KEY` in `polygon_fetch.py`; the legacy recommendation parser retains its pre-existing inline default. No value was copied into new code/docs/metadata/logs. The new updater uses `POLYGON_API_KEY` when set, otherwise the retained constant. Externalization is future work, not a prerequisite. Lazy legacy connection/credential adapters currently require running from the checkout; remove those migration dependencies by Stage 9.

**Interpreter and check commands:**

IDE discovery still selects Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The missing base installation remains unchanged. All successful Python commands used the same process-local standard-library workaround already documented in Stages 1-2; no interpreter, venv configuration, dependency set, live database, or production data was replaced:

```powershell
$stage3Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage3Runtime
$env:PYTHONPATH = "$stage3Runtime\python313.zip;$stage3Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

After IDE environment discovery before each Python invocation, checks included:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/test_simulate_trading_utils.py -q --tb=short
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage3 -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.update_market_data --help
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage3/test_update_cli.py tests/stage1/test_imports.py -q --tb=short
git diff --check
```

New untracked Stage 3 files also receive per-file `git -c core.autocrlf=false diff --no-index --check -- NUL <path>` whitespace checks. No formatter was added. An EOF blank-line finding was corrected without semantic changes.

**Checks run and results:**

- Verified the Stage 1/2 prerequisite baseline: **136 passed**, strict mypy passed on **46 source files**.
- Final integrated focused suite: **207 passed** in **10.24 seconds** (**67 Stage 1**, **65 Stage 2**, **71 Stage 3**, **4 legacy window utilities**). Strict mypy passes on **71 source files**, without suppressions/checker exceptions. CLI help exits 0. The separate CLI/import-safety follow-up passes all **7 tests**.
- Tests exercise an initially empty schema/database through the production schema/repository code using a fake MySQL adapter over temporary SQLite storage, real transaction rollback, and an injected InnoDB metadata response. They cover leading/interior gaps after later successes, corruption/truncation/missing source files, atomic write interruptions, interrupted raw and derived imports, partial batched insertion rollback, repeated runs without duplicate rows/downloads, exact-key mismatches despite plausible counts, late corrections, withdrawn keys, narrow-range neighboring repairs, and stable/nonconsecutive/nonmonotonic IDs.
- Provider/HTTP fixtures cover malformed/error/empty/delayed/paginated responses, count/adjustment/layout/price/date errors, legitimate zero transaction counts, explicit no-trading evidence, universe mismatch, explicit OTC/adjustment query parameters, transient retry versus nonretryable HTTP failure, and credential redaction. Availability fixtures cover supported lower bounds, unpublished sessions, unknown publication/boundary, future receipt times, archive recheck policy, and publication during a run. CLI tests verify custom roots/sources, exit codes, no work with unknown history, and failure propagation. Legacy frontend tests execute its extracted AST function without Torch and prove that refresh failure stops before training and date-ID lookup follows successful ingestion.
- The package import-safety fixture still imports every new module from an empty working directory without application-file/network/subprocess activity, output, or legacy/NumPy/Torch imports. Run metadata records actual policy provenance and revision and excludes credentials/configuration secrets. Tests use fake providers, temporary SQLite databases, and temporary files only. No live MySQL/schema/data mutation, production download/training, email, or trade occurred.
- Whitespace checks pass after the EOF fix; pre-existing CRLF normalization notices are informational. All unrelated Python, Java, model/data, Git, and IDE changes are preserved. No branch or commit was created.

**Pre-existing failures or remaining limitations:**

The missing Python base runtime and absent Torch remain unchanged; use the documented temporary workaround or repair the configured environment before ordinary Python commands. The seven previously recorded `tests/test_feature_vector_builder.py` unpacking failures and existing dependency inconsistencies are unrelated and were not repaired or rerun. Real provider publication/history entitlements still require operator evidence. Native MySQL/provider calls were not used for routine verification; fake driver tests validate transactional SQL behavior without claiming a live deployment check. Schemas must be InnoDB and compatible with the existing raw/derived column contract; automatic conversion is deliberately absent.

Source manifests prove completeness of the declared, validated grouped request, not every security that might exist outside that endpoint/universe or independent truth beyond provider evidence. Missing count/continuation evidence remains visible and fails readiness. Standalone old population/provider scripts and remaining recommendation/training date/model logic are isolated legacy paths; they do not inherit all new workflow guarantees. The new updater uses legacy derived semantics explicitly until Stage 5 supplies the new close-based dataset/target implementation. No Stage 4 validation CLI, Stage 5 features, Stage 6 model artifact, or Stage 7 recommendation workflow was implemented.

**Data/model rebuild or migration requirements:**

New source evidence uses `<data-root>/market_data/<source-hash>/responses/`, session manifests, and `runs/`; supported-history metadata remains under `<data-root>/history/`. Old filenames alone are unverified and may require a request-scoped initial refresh. Explicit database initialization adds updater metadata/binding and creates missing compatible tables but does not convert existing schema or mutate data during verification. Existing standalone caches are not compatible evidence for new data revisions. Legacy model/dataset target meanings remain unchanged; close-based datasets/models must still be rebuilt in Stages 5-6. No user cache/model directory was deleted.

**Next stage and concrete prerequisites:**

Implement **Stage 4 only** next. Recheck the fixture suite and strict mypy with the configured interpreter/workaround. Build a standalone, default-read-only validation command using the shared coverage/history/source semantics, `ManifestStore.load`, `MySQLMarketDataRepository.inspect`, typed snapshots/receipts, `SnapshotIndex`, and `reconciled`. These inspection/assessment operations are read-only; do not call `update_market_data`, `initialize_update_schema`, import, repair, or manifest completion methods during default validation. Report leading/interior gaps, exact source-key/content discrepancies, duplicates/orphans, invalid values, interrupted derived work, unavailable data, and unverified evidence with an actionable typed repair plan. Inspection currently requires the declared compatible updater schema; validation should report missing/incompatible metadata/schema without automatically creating or converting it. Be explicit about legacy derived semantics and do not claim close-to-close target validation until Stage 5 exists. Keep the actual supported-history date and provider schedule unknown unless new evidence is supplied. Do not begin Stage 5.

### Stage 4 handoff - 2026-10-15

**Final integration:** Stage 4's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 5 has not started. Standalone read-only validation, shared updater readiness checks, typed findings, and explicit repair-plan consumption are implemented. No production database, provider data, dataset, model, recommendation, or Java work was migrated by this stage.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/cli/validate_database.py`: `main`/`parser`; `cli/validation_setup.py`: `assemble_and_validate`; root `validate_database.py`: thin wrapper. Run `& '<configured-python>' -m stockguidance.cli.validate_database ...` or `& '<configured-python>' validate_database.py ...`. `pyproject.toml` declares the `validate_database` console command, available after refreshing the editable installation. No dependency or environment installation was performed in Stage 4.
- `src/stockguidance/workflows/validate_database.py`: immutable `ValidationSettings` and `validate_database(source, settings, boundary, calendar, repository, clock)`. Uses the existing bounded coverage and dependency planners, manifest loading, snapshots, and availability semantics without invoking updating or CLI argument strings.
- `src/stockguidance/validation/findings.py`: `FindingCategory`, `ValidationCheck`, `ValidationFinding`; `assessment.py`: shared `assess_session`/`coverage_state`; `derived.py`: `differing_columns`/`derived_values_match`; `boundaries.py`: `boundary_findings`; `result.py`: `ValidationResult` with `complete`, `failed`, and `exit_code`; `reporting.py`: `report_payload` and `render_text`. All shortened paths here are under `src/stockguidance/`.
- `src/stockguidance/database/repositories/validation.py`: `ValidationRepository`, `DatabaseInspection`, and physical `SessionRecordCounts`; `mysql_validation.py`: `MySQLValidationRepository`; `validation_rows.py`: tolerant `inspect_rows`. The diagnostic adapter reports malformed rows, duplicate mappings/keys, and raw/derived/receipt orphans without silently dropping them through inner joins or aborting all healthy sessions. Missing/incompatible schema metadata is reported without initialization/conversion. `mysql_market_read.py` exposes shared `decode_raw_row`, `decode_derived_row`, and `decode_receipt` boundaries for strict and diagnostic readers.
- `src/stockguidance/validation/repair.py`: `RepairAction`, `RepairItem`, `RepairPlan`, `plan_repairs`, and `load_repair_plan`; `cli/update_market_data.py`/`cli/update_setup.py`: explicit `--repair-plan <validation.json>` support. `UpdateSettings.repair_plan` is optional. Source, bounds, cutoff, and manual-review requirements are checked before provider/database side effects. Plan bounds supply omitted update bounds.
- `src/stockguidance/database/repositories/market_data.py` and `mysql_market_data.py`: add read-only `MarketDataRepository.inspect_integrity(query)` using the same diagnostic reader as validation. `market_data/update_planning.py`, `market_data/ingestion.py`, and `workflows/update_market_data.py` use numerical legacy-derived checks as well as revision receipts. The updater's final readiness pass uses `assess_session` and integrity findings, so orphaned rows and forged derived receipts cannot produce success.
- `tests/stage4/conftest.py`, `test_validation_workflow.py`, `test_database_integrity.py`, and `test_validation_cli_and_repairs.py`: **54** deterministic tests reusing the fake MySQL-over-temporary-SQLite harness from Stage 3. `docs/database_validation.md` documents syntax, findings, scope, exit codes, repair invocation, and limitations; `docs/market_data_updating.md` links the shared checks and explicit repair option.

**Decisions and contract/schema changes:**

- Validation is read-only by default and by API design: database inspection uses SELECT statements in a transaction that is always rolled back, with no commit, initialization, fetch, import, derived replacement, history persistence, or manifest-completion calls. Reports go to stdout; saving JSON requires explicit shell redirection. The validator has no schema/repair/delete switch. An explicit history boundary with evidence is permitted in memory but never saved by validation.
- The live supported-history start and publication schedule remain **unknown**. Missing supported-history metadata fails before database access. Unknown publication yields unverified sessions and no data queries. A known delay requires provenance. Persisted/request boundaries are validated independently of observed database minima, preserving leading-gap detection. Optional `--as-of` must be timezone-aware and no later than the injected/current clock; actual receipt times remain distinct from session closes.
- Categories are `missing`, `invalid`, `incomplete`, `unverified`, `expected_absence`, `not_yet_available`, `not_assessed`, and `failed`. Checks identify schema, sessions, source manifests, raw keys/content, receipts, duplicates/orphans, legacy derived features/history/outcomes, and the explicitly unimplemented close-to-close target check. Findings carry exact dates, ticker keys, and mapping IDs where available. JSON separates physical row counts from successfully decoded unique keys, records source/provider evidence, and identifies which checks and sessions were assessed, including context. Unavailable observations do not claim completed value checks or reveal future record keys.
- Completeness remains relative to the declared provider endpoint/universe/adjustment/interval. Equal counts do not establish matching keys/content. Missing/corrupt manifests, incomplete provider evidence, invalid prices/counts, stale/absent receipts, missing derived rows, and incorrect mature legacy values prevent readiness. Internal consistency alone does not prove provider completeness. A delayed response after expected publication reports both unavailable data and unverified required evidence; it cannot make the updater succeed.
- No Cartesian ticker/session requirement is introduced. Ticker absence in adjacent responses leaves history/outcomes unavailable with an explicit expected-absence finding; it does not invent listing/delisting dates or zero returns. The supported first session has expected missing predecessor features. Immature exit prices leave final labels unavailable. Missing mature dependencies inside requested coverage fail; mature dependencies beyond its end without verified context are explicitly not assessed rather than claimed valid or requested implicitly.
- Derived validation still uses **`legacy-open-to-close-v1`** only. Recomputed values are compared with relative tolerance `1e-10` and absolute tolerance `1e-12`, including null availability, rather than trusting a content receipt alone. Numerical close-to-close targets and previous-close feature construction are explicitly `not_assessed` until Stage 5. No legacy dataset/model is relabeled compatible.
- Existing dependency planning supplies at most two available context sessions on each side, never before supported history or beyond the publication/cutoff bounds. Orphans without any date mapping have no inferable historical date, so the read-only integrity query checks those unassigned records across storage and reports their IDs explicitly. Damaged context is visible and requires review when outside the requested repair scope.
- Repair-plan schema **1** contains non-secret source identity, exact request bounds/cutoff, typed actions, dates/checks/tickers, and no executable SQL. Automatic actions are `refresh_source`, `reimport`, `rebuild_legacy_derived`, and `complete_manifest`. Refresh actions can override an unexpired correction interval. Manual schema/integrity/policy/context proposals are rejected by automatic update invocation until reviewed; no automatic schema conversion/deletion is added. Recovery still reconciles the whole declared scope and is idempotent.
- Exit **0** means all required available request coverage is verified, with expected/deferred/not-assessed conditions visible; a calendar-closure-only range can succeed. Exit **2** means incomplete/invalid/unverified coverage or no available session; argparse also uses 2. Exit **1** means failed setup/inspection/configuration. Error text is sanitized; credentials are absent from reports/plans. The hardcoded key location remains documented without its value, and validation never loads it.

**Interpreter and check commands:**

IDE discovery still selects Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The missing base installation remains unchanged. Every Python invocation followed IDE environment discovery and used the same process-local matching-runtime workaround from previous stages:

```powershell
$stage4Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage4Runtime
$env:PYTHONPATH = "$stage4Runtime\python313.zip;$stage4Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

Commands from the repository root:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage4 -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage4 tests/stage1/test_imports.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.validate_database --help
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' validate_database.py --help
git diff --check
```

**Checks run and results:**

- Prerequisite baseline: **207 passed**; strict mypy passed on **71 source files**.
- Final integrated fixture suite: **261 passed** in **44.52 seconds** (**67 Stage 1**, **65 Stage 2**, **71 Stage 3**, **54 Stage 4**, **4 legacy utility tests**). Final Stage 4 plus package-import checks: **55 passed**. Strict mypy: **no issues in 84 source files**, without suppressions/checker exceptions. Module and root-wrapper help commands exit 0.
- Fixtures cover leading/interior gaps after later successes, plausible counts with wrong/missing source keys, legitimate ticker absence/no-trading evidence, duplicate raw/derived/date rows, raw/derived/receipt and unassigned orphans, invalid mapping IDs, bad OHLC/counts, content drift, missing derived rows, interrupted receipts/manifests, mature missing/incorrect outcomes, immature final labels, historical receipt cutoffs, corrupt/missing/unverified source evidence, unknown publication/history, and missing/incompatible schema/source binding.
- The full default CLI path is exercised with fake storage, custom roots/config, and explicit in-memory history. Write/fetch methods are guarded, all SQL is SELECT/transaction start, commit counts and data/manifest files remain unchanged, and no history directory is created. CLI tests cover readable/JSON output, actual assessed-check scope, exit codes, and sanitized errors.
- Explicit JSON repair round trips recover fixture gaps without unnecessary downloads. Tests reject mismatched/manual/future plans before side effects, force a source refresh before the correction interval expires, complete interrupted markers only during updating, and prove both commands reject unassigned orphans and incorrect mature values hidden behind a forged receipt. Package imports remain safe from an empty working directory with no heavy/legacy imports or application-file/network/subprocess activity.
- Tracked and new-file whitespace checks pass; existing CRLF normalization notices are informational. No live MySQL mutation, production download/training, email, or trade was performed. Existing user Python/Java/data/model changes remain intact; no branch or commit was created.

**Pre-existing failures or remaining limitations:**

The missing Python base runtime, absent Torch, seven recorded legacy `test_feature_vector_builder.py` unpacking failures, and existing dependency inconsistencies remain unrelated and unchanged. Native MySQL/provider operations were not used as verification; fake driver tests exercise the production SQL/transaction adapters without claiming deployment validation. Live history/publication entitlement evidence still requires operator input. Validation requires the declared compatible schema and reports/requires review when it cannot inspect it; it never migrates the schema. Historical cutoffs honor available evidence but cannot reconstruct historical data revisions. Numerical new feature/target validation, dataset construction, and model compatibility remain Stage 5 and later work. Console entry points require refreshing the editable installation; module/wrapper commands work directly.

**Data/model rebuild or migration requirements:**

No production schema/data/history/model/cache was changed and no rebuild is needed for validation itself. Existing source manifests and receipts retain schema 1. Repository implementations of `MarketDataRepository` now need the read-only `inspect_integrity` method. Existing legacy derived datasets/models still require rebuilding under the explicit close-based specifications introduced by Stages 5-6; validation does not grant them compatibility. The temporary lazy database/credential adapters remain for Stage 9 removal.

**Next stage and concrete prerequisites:**

Implement **Stage 5 only** next. Verify the Stage 1-4 fixture suite and strict mypy using the configured interpreter/workaround. Inspect the actual feature builders, windowing, tensor loaders, training providers, caches, and label transforms. Build one shared numerical sample API for historical closing-price inputs through T-1 and raw `close(T+1) / close(T) - 1` targets, with explicit ordered tickers/features, transformations, missing-data/availability policy, adjustment, and data revision. Keep immature labels unavailable and exclude them from training. Extend `ValidationCheck.TARGET` and the validation workflow for those exact specifications; keep the legacy-derived validation path explicit and isolated until its consumers migrate. Carry source-history/publication unknowns forward rather than inventing production values. Do not begin Stage 6.

### Stage 5 handoff - 2026-10-15

**Final integration:** Stage 5's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 6 has not started. Shared close-only numerical samples, mature close-to-close targets, chronological dataset selection, revision identities, and read-only numerical database validation are implemented. Existing training/model/recommendation/simulation consumers remain explicitly isolated legacy paths until their assigned stages; matching vector dimensions do not establish compatibility.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/features/specification.py`: extends immutable `FeatureSpec` with ordered `CloseFeature` transformations, explicit `PriceAdjustment`, numerical version, exact shape/input size, and layout. Existing previous-close, daily interval, missing-history, and unsupported entry-open contracts remain enforced. `features/construction.py`: validated immutable `FeatureValues` and pure `construct_features`.
- `src/stockguidance/market_data/closes.py`: `CloseObservation`, `CloseIssue`, `PriceState`, and `valid_close`; `database/repositories/closes.py`: `CloseQuery`, `CloseData`, and `CloseRepository`. Every exact queried session/ticker key has one valid available observation or a typed issue, with no missing/dense-slot guessing. The query carries the evidenced history boundary, non-secret source, actual sessions, ordered ticker IDs, and aware cutoff.
- `src/stockguidance/database/repositories/close_projection.py`: pure `project_closes`; `verified_closes.py`: `VerifiedCloseRepository(source, validation_repository)`, a read-only adapter using diagnostic raw snapshots, source manifests, actual receipt times, and exact raw revision/content reconciliation. `validation.py`/`validation_rows.py` add normalized `DatabaseInspection.ticker_symbols` and ambiguous-mapping findings; mappings use dictionaries rather than per-row scans. The existing compatible updater schema is inspected without creation or conversion.
- `src/stockguidance/targets/construction.py`: pure `close_to_close_return`, immutable `TargetValues`, and `construct_targets`, preserving per-ticker unavailable outcomes and the exact output order. These calculate new targets from raw closes, never from legacy derived target columns.
- `src/stockguidance/datasets/building.py`: shared public `build_sample(specification, target, timing, boundary, calendar, publication, repository, *, label_cutoff=None)`; `sample.py`: validated immutable `Sample`/`TargetState`; `training.py`: `build_training_dataset` and `TrainingDataset.eligible`/`excluded`; `identity.py`: `feature_identity`/`sample_identity`; `numpy_arrays.py`: lazy `training_arrays`/`validate_arrays`; import-safe `datasets/__init__.py`. Shortened paths in this entry are under `src/stockguidance/`.
- `src/stockguidance/validation/close_samples.py`: `validate_close_samples` and read-only `InspectedCloseRepository`; `feature_scope.py`: bounded `feature_query`; `sample_reporting.py`: numerical `sample_payload`. `workflows/validate_database.py` extends `ValidationSettings` with optional exact features and an explicit target specification, and assesses the shared numerical samples. `validation/findings.py`, `result.py`, `reporting.py`, and `repair.py` expose separate feature/target checks, numerical sample results, dependency dates, actual assessed scope, and cause-aware repair proposals.
- `src/stockguidance/cli/validate_database.py`/`cli/validation_setup.py`: add paired `--ticker-ids '17,3' --lookback-sessions 3` for exact default-feature validation. Existing module/root commands, source/bounds/history/publication arguments, read-only behavior, and exit codes remain. Without the paired arguments, targets are computed for verified raw source keys and model feature readiness is explicitly not assessed. Typed callers can supply any supported ordered feature tuple through `ValidationSettings.features`.
- `feature_vector_builder.py`, `feature_windowing.py`, `training_instance_provider.py`, `load_database_rows_into_tensors.py`, and `trainer/label/transform/transformers.py`: add explicit legacy module documentation pointing new consumers to the extracted packages. Their old intraday data, sanitization, size-based window guessing, target transforms, and cache paths are not connected to new specifications. The actual legacy callers and model architecture/controller migration belong to Stages 6-9.
- `pyproject.toml`: declares the now-direct numerical-adapter dependency `numpy>=2.1,<3`; installed NumPy **2.4.3** already satisfies it. No dependency installation, environment replacement, or editable-install refresh was performed. `docs/close_samples.md` documents the numerical contract, APIs, cache identities, availability limits, and migration; `docs/database_validation.md` documents the new checks/options/context.
- `tests/stage5/conftest.py`, `test_samples.py`, `test_training_and_identity.py`, and `test_repository_and_validation.py`: **76** deterministic behavioral checks using hand calculations, fake close repositories, and the existing temporary-SQLite/fake-MySQL harness. One Stage 4 expectation is updated from unimplemented target validation to actual numerical target results and explicitly unassessed model features.

**Decisions and contract/schema changes:**

- Numerical version **`previous-close-v1`** uses the exact L exchange-session closes immediately before entry T, ordered oldest first. Default feature: for each ticker, `close(s) / close(first session in this exact window) - 1`. Optional explicit `log_close` is supported as another stateless close-only feature, alone or in a specified order. No scaler is fitted, so there is no fitted-state leakage or global/full-history normalization. The anchor's zero is an actual observed-price ratio, not fabricated history; a one-session relative window is necessarily all zero and a longer lookback is needed to describe movement.
- Tensor layout is exactly `(L, ticker_count, feature_count)`, flattened in C order: session, ticker, feature. Ticker IDs may be sparse and reversed; they are keys, never offsets. Missing history invalidates the whole fixed input vector. Feature order, signal mode, interval, lookback, missing-history policy, source adjustment, and numerical version are explicit. Shape equality cannot substitute for a specification match. `entry_open` still fails with the explicit Stage 10 message.
- Every sample carries authoritative entry/exit sessions and its original aware pre-close decision cutoff. Inference omits `label_cutoff`, reads only prior-session history, and returns `not_requested` targets. Training/evaluation explicitly supplies its later cutoff; this never changes the historical feature cutoff. Entry/exit prices are requested only after both expected publications and the exit close have matured. Actual source/import availability must additionally prove maturity. A T-1 entry remains immature before T's close/publication, even when the database contains its future outcome.
- Targets are raw **`close(T+1) / close(T) - 1`** only; `TargetSpec.transform` remains `raw_return`, so the separately exposed raw/transformed tuples are identical. Unsupported legacy ranking/normalization is rejected rather than reinterpreted. Missing or immature targets stay `None`, including partial ticker outcomes; training excludes the whole dense sample until all required inputs and labels are valid. Excluded decisions keep their dates rather than replacing them with older samples.
- `WindowState` continues to express calendar/expected-publication readiness separately from actual `FeatureValues.values`/`issues`. Numerical target states distinguish ready, not requested, not yet available, unverified, and unavailable. Price issues distinguish missing, invalid, unverified, actual late availability, and verified provider ticker absence. No Cartesian ticker/session completeness requirement, inferred listing/delisting date, fallback price, or successful zero-return replacement is added.
- Data access is separate from feature/target mathematics and numerical framework handling. The production close adapter proves matching provider evidence and raw content/receipts before emitting a price; a receipt or plausible count alone is insufficient. Runtime boundaries validate every queried key, finite positive prices, exact windows, numerical feature/label layouts, and immutable specifications. The optional NumPy adapter produces contiguous float64 matrices and rejects NaN/infinity/dtype/shape errors without sanitization. No Torch import or device operation is introduced into sample construction or package imports.
- New numerical values are computed on demand; **no database schema, source manifest/receipt schema, persisted target column, or production data is changed**. Legacy derived checks remain explicitly `legacy-open-to-close-v1` and continue independently. Standalone validation now computes new close targets using the shared API. Exact feature checks require an explicit model universe/lookback and use a documented historical decision one microsecond before each entry close, with the requested validation cutoff for label assessment.
- Numerical validation extends read-only historical context to the entire explicit lookback, beyond the legacy two-session context when needed, without crossing supported history or publication/cutoff bounds. Reports show exact sample dates, ticker order, windows, numerical values or unavailable states, specification/version/layout, revision identities, and dependency dates. Mature in-scope missing dependencies block validation. Insufficient boundary history, verified ticker absence, immature exits, and beyond-scope unverified dependencies remain distinct. Required source/import repairs cover numerical consequences of the same dependency; numerical findings never request rebuilding a legacy column as if it stored the new target. Existing explicit updater repair round trips remain automatic where appropriate; integrity/schema review still requires manual review.
- Feature identities include full non-secret source/request/interval/adjustment, numerical feature specification/version/layout, ticker order, entry/exit/decision, calendar identity/version, and raw content/availability revision. Sample identities add target semantics, label cutoff, and outcome dependencies. Corrections/backfills invalidate affected identities; label-only corrections do not change a feature identity. Row order and database date IDs do not affect identity. Stage 5 introduces **no persistent cache** and reads no legacy NPZ cache; later prediction caches must additionally include model/artifact identity.
- Existing latest manifests/receipts cannot reconstruct a prior revision or original publication evidence after a correction/recheck. Such historical inputs remain unavailable rather than being assigned fabricated historical availability. The actual live supported-history start and provider publication policy remain **unknown** until evidenced operator configuration is supplied. No production date, delay, or entitlement was invented.

**Interpreter and check commands:**

IDE discovery continues to select Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The missing base installation is unchanged. Every Python invocation followed IDE environment discovery and used the existing process-only matching-runtime workaround:

```powershell
$stage5Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage5Runtime
$env:PYTHONPATH = "$stage5Runtime\python313.zip;$stage5Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

Commands from the repository root included:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage5 tests/stage4 -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage4 tests/stage5 tests/stage1/test_imports.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.validate_database --help
git diff --check
```

AST-only syntax checks also parse the five documented legacy modules without importing their model/database dependencies. New/untracked Python and documentation files receive individual `git diff --no-index --check -- NUL <path>` whitespace checks with CR-at-EOL handling; ordinary tracked diff checks use the repository's normal newline settings. A temporary check that disabled tracked CRLF normalization produced newline-only false positives; the normal tracked check and explicit new-file checks pass without file-wide reformatting.

**Checks run and results:**

- Verified Stage 1-4 baseline before implementation: **261 passed** in **43.67 seconds**; strict mypy passed on **84 source files**.
- Integrated suite: **337 passed** in **54.58 seconds** (**67 Stage 1**, **65 Stage 2**, **71 Stage 3**, **54 Stage 4**, **76 Stage 5**, **4 legacy utility tests**). Final affected Stage 4/5 plus package-import checks after availability/mapping refinements: **131 passed** in **33.20 seconds**. Strict mypy: **no issues in 99 source files**, with no suppressions or checker exceptions.
- Hand-calculated fixtures prove exact prior-session features, feature equality between training/inference, sparse/reversed ticker alignment, explicit feature ordering, first-history unavailability, mature/immature/late-published labels, historical cutoffs despite future storage, missing/invalid prices, unsupported entry-open, and rejection of equal-sized entry-session windows. A next-session close of 210 after entry close 200 yields **5%**, whereas that exit session's open of 10 would produce the materially different legacy intraday return.
- Training fixtures retain excluded boundary/immature/missing decisions and produce only valid mature arrays. Runtime tests reject malformed numerical layouts, NaN/infinity, wrong dtype, noncontiguous arrays, incomplete/duplicate repository keys, source-adjustment mismatches, incompatible equal-width dataset semantics, and nonchronological decisions. Revision tests cover corrections, backfills, source/adjustment/spec/ticker/window/cutoff/calendar changes, stable row ordering, and separate label-versus-feature invalidation.
- Database fixtures exercise the real diagnostic SQL adapter plus manifests with chronological actual receipt times, custom source roots/configuration, nonconsecutive/nonchronological date IDs, corrupt/missing evidence, invalid/duplicate raw data, late corrections, verified provider ticker absence, missing mature dependencies, and explicit updater repair recovery. Numerical validation checks full lookback outside the old context limit and never queries before supported history. Tests prove read-only API/CLI SQL and unchanged commit counts/source files; old source-refresh repair behavior remains covered.
- Package imports still succeed from an empty working directory without output, application file/network/subprocess activity, legacy imports, NumPy, or Torch imports. The module help command exits 0 and exposes the paired numerical feature arguments. The five touched legacy modules pass AST syntax checks. PyCharm's selected-file build reports success but limited build diagnostics; pytest/mypy provide the substantive Python verification.
- Tracked/new-file whitespace checks pass; repository CRLF normalization notices are informational. No live MySQL mutation, production provider download, production training, dataset/model rewrite, email, or trade was performed. Existing user Python/Java/data/model changes remain intact; no branch or commit was created.

**Pre-existing failures or remaining limitations:**

The missing Python base runtime, absent Torch, seven recorded legacy feature-builder unpacking failures, and unrelated dependency inconsistencies remain unchanged. Legacy model-dependent commands were not used as new-strategy verification. Native MySQL/provider deployment was not exercised; fake driver tests validate the production read-only adapter and existing update transactions. Historical data revisions cannot be reconstructed after evidence replacement. Live history/publication evidence still requires operator input. CLI feature arguments select the default relative-close transform; other supported explicit feature orders are available through the typed API. New training/artifacts, end-to-end recommendation, and portfolio/simulation migration remain the later numbered stages.

**Data/model rebuild or migration requirements:**

Existing open-to-close datasets, NPZ caches, and model artifacts are incompatible and must be rebuilt using the new exact specifications; matching tensor sizes are insufficient. No old files or models are deleted or relabeled. Persisted source manifests/receipts and compatible updater schema remain version 1. `DatabaseInspection` now optionally exposes ticker mappings, required for numerical stock-ID alignment. No installation refresh is required for module commands in this checkout; declared package dependency metadata takes effect on the next normal install. The retained hardcoded key location remains `DEFAULT_API_KEY` in `polygon_fetch.py`, without its value in new code, metadata, logs, or documentation.

**Next stage and concrete prerequisites:**

Implement **Stage 6 only** next. Verify the Stage 1-5 fixture suite and strict mypy using IDE environment discovery and the configured-runtime workaround. Inspect actual model definitions, training controllers/providers, label assumptions, output activations, checkpoints, run filesystem handling, ensembles, and CLI callers. Use `build_sample`/`build_training_dataset` and their explicit eligible/excluded outcomes; supply evidenced history/publication/source configuration and actual historical receipt cutoffs. Resolve the configured environment's absent Torch as necessary for tiny deterministic CPU training/export/load checks; do not substitute a different interpreter or launch production training. Export independent inference artifacts with exact ordered tickers/features, layout/version/transforms, target semantics, source adjustment/interval, data revisions, training/information cutoffs, architecture, and weights. Keep resume/optimizer/controller state separate and validate semantic compatibility rather than dimensions. Old models require retraining. Do not implement Stage 7 recommendation workflows or Stage 8 portfolios/simulation, and do not remove remaining legacy paths before their assigned migration/cleanup stages.

### Stage 6 handoff - 2026-10-15

**Final integration:** Stage 6's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 7 has not started. Typed chronological training, independent inference artifacts, explicit ensembles, separate resumable checkpoints, reporting and thin training frontends are implemented. Recommendation, capital allocation and simulation workflows remain assigned to Stages 7-9; their isolated legacy paths do not enforce the new strategy.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/models/specification.py`: immutable `ModelSpec`, `Architecture`, `Activation`, `LayerType` and `BiasMode`. `models/factory.py`: `build_model`. `models/activations.py`, `sparse.py`, `transformer.py`, `mlp.py` and `convolution.py`: lazy shared implementations of the user's existing MLP, activation-dependent initialization, structured sparse connections, random masked linear layers, transformer layers and convolution/transformer network. All shortened package paths in this handoff are under `src/stockguidance/`.
- `artifacts/contracts.py`: `ModelSemantics`, `TrainingProvenance`, `ArtifactMetadata` and explicit `ArtifactReference(path, artifact_id)`. `artifacts/decoding.py`, `archive.py` and `weights.py`: exact allowlisted metadata decoding, archive/content identities, and validated weights-only serialization. `artifacts/exporting.py`: atomic `export_artifact`. `artifacts/loading.py`: `load_artifact`/`LoadedModel`. These inference modules do not import training controllers, optimizers or the training package.
- `artifacts/ensembles.py`: `export_ensemble`, `load_ensemble`, `LoadedEnsemble` and semantic member validation. Ensemble members and weights are explicit, embedded and independent of their original paths. `inference/prediction.py`: low-level `predict_sample` with exact sample compatibility, finite numerical boundaries and historical model-cutoff enforcement; ranking, eligibility, recommendation orchestration and portfolio planning are not implemented here.
- `training/configuration.py`: immutable `TrainingConfig`, `OptimizerChoice`, `DeviceChoice` and `Precision`. `training/data.py`: chronological dataset validation and revision identities. `training/batches.py`, `resources.py` and `engine.py`: numerical batching, explicit device/precision/optimizer assembly, L1 fitting and MAE evaluation. `training/checkpoints.py`: atomic checkpoint save/resume, configuration/data/resource/framework compatibility and random-state restoration. `training/result.py`: immutable training, epoch, exclusion and evaluation results. `training/service.py`: `train_model(dataset, config, artifact_path, *, evaluation=None, checkpoint_path=None, resume_from=None) -> TrainingResult`.
- `training/experiments.py`: `ExperimentMember` and fail-fast `run_experiment` using explicit typed configurations and artifact destinations, without CLI arguments or controller-directory discovery. `reporting/training.py`: allowlisted `training_payload`. `workflows/train_model.py`: validated `HistoryTrainingRequest` and `train_from_history`, assembling the same Stage 5 datasets from injected history/calendar/publication/close-repository dependencies.
- `cli/training_configuration.py`, `cli/training_job.py` and `cli/train_model.py`: job parsing, read-only dependency assembly, `build_parser`/`parse_args`/`main`, JSON reporting and exit mapping. Run `& '<configured-python>' -m stockguidance.cli.train_model --config training-job.json --artifact output/model.sgmodel`; root `train_pytorch_network.py` is a thin wrapper, and `train_pytorch_network_cli.py` re-exports argument parsing. `pyproject.toml` registers `train_model` and adds optional dependency group `training = ["torch>=2.6,<3"]`; the console script appears on the next ordinary install. No editable-install refresh was needed for module/root commands.
- Root `models/multi_layer_mlp.py`, `models/sparse_linear.py` and `models/convolution_transformer.py` become small temporary re-exports of the shared package implementations. `models/__init__.py` shares `SigmoidRange` and retains its explicitly legacy manifest-loading path for unmigrated consumers. No duplicate new model implementation or old-artifact fallback is introduced into the new inference loader.
- `trainer/legacy_training.py` and `trainer/legacy_arguments.py` isolate the old root controller/frontend. `choose_next_day_stocks.py`, `incremental_training_harness.py`, `batch_run.py` and `tests/test_train_pytorch_dry_run_head_tail.py` redirect only their old training imports to that explicit legacy path. `trainer/state/single_run.py`, `family_run.py`, `trainer/model_loader.py`, `train_single_run.py` and `io_run.py` document their remaining legacy responsibilities. Their full consumers migrate in Stages 7-9; no legacy controller is reconstructed by the new service or loader.
- `tests/stage6/conftest.py`, `test_training.py`, `test_checkpoints.py`, `test_artifacts.py`, `test_ensembles.py` and `test_cli_and_workflow.py`: **78** deterministic checks using Stage 5's fake close repository, temporary source metadata/files, and tiny CPU models. `docs/model_training.md` documents exact public APIs, validated job JSON, commands, model choices, chronology, schema, resume/ensemble behavior, resource options and migration requirements.

**Decisions and contract/schema changes:**

- Stage 5's `previous-close-v1` samples are the sole new fitting/inference input. Exact feature/ticker order, stateless transformations, lookback, missing-history policy, adjustment, daily interval, raw close-to-next-session-close target and calendar identity/version are enforced. New training never reads a legacy NPZ cache or derived target column. Feature sequence length is exactly the lookback, rather than the old ambiguous `prior_days + 1` convention. Allocation policy does not alter model inputs or metadata.
- Fitting uses only `TrainingDataset.eligible`, with all required labels mature at its explicit aware cutoff. Excluded dates and readiness/price states remain visible; no zero sanitization or older-sample substitution occurs. Optional evaluation follows the entire requested training period, uses the same exact semantics, and requires the fitting cutoff to precede every evaluation decision. Evaluation labels have a separate later cutoff and never affect fitted weights or inference metadata. Training MAE is explicitly `in_sample`; separate evaluation is `chronological_holdout`, not an unconditional claim of unbiased performance.
- The existing model algorithms, sparse connection patterns, random-mask probabilities, transformer structure, bias modes, conditional LayerNorm and initialization families are shared with legacy imports. Architecture constants are bound to model implementation version **`stockguidance-models-v1`**. The new model specification records explicit hidden widths and layer types; its layer count is derived from the widths. A zero-hidden-layer MLP correctly builds one direct linear mapping: the old implementation incorrectly continued appending another hidden/output path after creating that mapping. This prerequisite model correction is tested.
- Raw-return output is unrestricted: `ModelSpec.final_activation` must be `none`. Hidden activations remain configurable. Existing fitting choices remain L1/MAE, AdamW by default (Adam/SGD supported), default learning rate `1e-7`, batch size 256, fp32, automatic CPU/CUDA selection, and no shuffling by default. Exponential decay, CUDA autocast/scaler, worker count, pinning and seed are explicit. The existing expandable-segments allocator default, explicit allocator overrides and CUDA policy excluding GPU names containing `4070` are preserved and covered with fake CUDA queries. CPU fixtures require fp32 and no CUDA device. New inference weights/inputs are explicitly float32, independent of process default dtype. No distributed trainer, automatic production sweep or old simulation callback is added.
- Inference schema **`stockguidance-inference-1`** is one `.sgmodel` ZIP containing exactly `manifest.json` and `weights.pt`. Its identity hashes exact metadata and the weight SHA256. Metadata records full non-secret source/request identity, ordered feature/ticker specification, numerical version/layout/preprocessing, target formula/horizon/close execution, explicit **`pre_close_decision_same_close_fill`** assumption, architecture/implementation/framework/dtype, training dataset revision, eligible/excluded sample identities, fitted entry/exit bounds, final exit close timestamp, seed, epochs, and training/information cutoffs. The information cutoff conservatively equals the full allowed fitting cutoff.
- Inference metadata contains no `SourceConfig`, database config/path, credential value/reference, mutable controller, optimizer, scaler, experiment directory or allocation state. Weights are tensor-only state dictionaries loaded with `weights_only=True`, exact key/shape/dtype/finite validation and strict architecture loading. Unknown, missing, corrupt or unsupported metadata/weights fail. Export reconstructs the model and compares predictions before atomic replacement; interrupted replacement preserves the previous destination. Loading a moved artifact from an empty directory requires only the installed package and Torch.
- Checkpoint schema **`stockguidance-checkpoint-1`** is separate from inference. It stores model/optimizer/scheduler/scaler state, completed epoch, CPU/CUDA random states and the loader generator state. Its compatibility identity includes configuration except total epoch count, exact dataset revision, effective device/precision and framework version. Resume requires a larger total epoch count; changed data/spec/cutoff/optimizer/resources fail. Evaluation has a separate generator, preserving the training shuffle sequence. Atomic checkpoints may remain usable if a later artifact export fails; no successful training result is returned on either required failure.
- Ensemble schema **`stockguidance-ensemble-1`** is a self-contained archive with explicit member artifact IDs and positive weights summing to one (equal by default), embedding the complete member files. Duplicate members, missing/corrupt members and any feature/target/ticker/source/calendar mismatch fail even when dimensions match. Architectures and fitting cutoffs may differ; prediction checks every member's information cutoff. Nested ensembles and legacy member artifacts are unsupported.
- The CLI reads an explicit job JSON and an existing evidenced source-specific history boundary. It does not update data, guess or persist history, create/convert schema, or discover models. `VerifiedCloseRepository` enforces manifests/raw revisions and actual historical receipt cutoffs. Exit **0** returns a real artifact/result, **2** means argument/job validation failure, and **1** means required data/training/checkpoint/export failure. External exception bodies are not logged; typed application callers receive exceptions. Unknown or legacy input/label switches fail. `entry_open` still raises the clear Stage 10 unsupported error in the feature contract.
- Existing manifests, database schemas, history metadata and Stage 5 sample versions remain unchanged. Live supported-history/publication evidence is still unknown until supplied by an operator. Historical evidence overwritten by corrections cannot be reconstructed; unavailable historical prices are excluded rather than assigned fabricated publication times. No new persistent prediction cache is introduced; later caches must additionally bind artifact identity and decision/data/specification identities.

**Interpreter, dependencies and check commands:**

IDE discovery continues to select Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. The missing base installation remains unchanged. All Python invocations followed IDE discovery and used the existing process-local matching-runtime workaround:

```powershell
$stage6Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage6Runtime
$env:PYTHONPATH = "$stage6Runtime\python313.zip;$stage6Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

The required CPU acceptance dependency was installed in that same configured environment, without switching/recreating the interpreter:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pip install 'torch>=2.6,<3' --index-url https://download.pytorch.org/whl/cpu
```

Installed Torch is **2.14.1+cpu** with its required dependencies. Pip reported the pre-existing Scalene requirement for missing `psutil`; unrelated dependencies were not repaired. This CPU build is for this environment's fixture verification; deployment should select the appropriate Torch build for its intended device.

Checks from the repository root included:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/test_simulate_trading_utils.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage6 tests/test_sparse_linear.py tests/test_random_percent_linear.py tests/stage1/test_imports.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/stage6 tests/test_simulate_trading_utils.py tests/test_sparse_linear.py tests/test_random_percent_linear.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.train_model --help
git diff --check
```

Staged changes also receive `git diff --cached --check`. New/untracked source, fixture, migration, packaging and documentation files receive individual `git -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --no-index --check -- NUL <path>` checks. `--no-index` may return 1 solely because a new file differs from NUL; actual whitespace diagnostics are checked separately. No formatter was introduced or file-wide formatting applied to unrelated files.

**Checks run and results:**

- Verified prerequisite baseline before implementation: **337 passed** in **53.98 seconds**, strict mypy passed on **99 source files**.
- Final integrated fixture suite: **419 passed** in **57.66 seconds** (**67 Stage 1**, **65 Stage 2**, **71 Stage 3**, **54 Stage 4**, **76 Stage 5**, **78 Stage 6**, **4 legacy window utilities**, **4 existing sparse/random model checks**). Strict mypy passes on **132 source files** without suppressions/checker exceptions. Three expected Torch nested-tensor optimization warnings preserve the existing transformer behavior.
- Tiny deterministic CPU models train, export and load with matching predictions across all five existing MLP layer choices, all nine supported hidden activations and the original convolution/transformer architecture. Tests check exact sparse/mask behavior, unrestricted outputs, one-layer layout, metadata and float32 independence from process default dtype. There is no production training run.
- Training/holdout tests prove the T-1 entry remains immature before T's close despite stored future outcomes, preserve excluded dates, use the hand-calculated close returns (5% and -10%), reject overlapping/future-cutoff/incompatible/no-mature evaluation, and identify in-sample metrics. Fitting and inference use Stage 5 sample identities. Corrections invalidate checkpoint compatibility; full and resumed shuffled CPU training produce identical weights, metrics and artifact identities, including optimizer/scheduler/random restoration.
- Artifact fixtures exercise moved standalone loading from an empty working directory without any controller, optimizer, experiment-layout or training-package import. They verify allowlisted metadata and tensor-only weights, absent credentials/database/controller/allocation fields, corrupt/incomplete/legacy/specification/target/entry-open rejection, metadata maturity, weight/layout validation, changed references, non-finite failures, and atomic export interruption preserving the existing artifact.
- Ensemble fixtures compare explicitly weighted predictions, load after original member files are removed, and reject equal-width ticker/transform/adjustment/source/calendar mismatches, invalid weights, missing members and changed member identities. Unsupported target transforms are rejected by artifact decoding. Failure fixtures propagate training/checkpoint/export and experiment-member errors without success or new artifacts.
- The complete new training CLI runs against fake read-only closes and persisted temporary history, honoring custom source roots/configuration and actual cutoffs. Tests check JSON results, exclusions/metrics, invalid-job rejection before execution, redacted nonzero failure results, unknown-history failure without creating directories, root/frontend delegation and documented JSON parsing. Legacy touched modules pass AST-only syntax checks without importing their database/controller dependencies.
- All-package import safety still passes from an empty working directory with application file/network/subprocess access guarded and no NumPy/Torch/legacy imports. Module help exits 0. PyCharm's selected-file build reports success with limited build diagnostics; pytest/mypy provide substantive verification. Tracked/new-file whitespace checks pass; CRLF normalization notices are informational.
- Existing Python/Java/data/model changes are preserved. New package/test files and the redirected legacy controller test appear staged after IDE integration; no staging/unstaging command was run by the agent, and the existing index is left intact. No production source/database/schema/data/model directory, provider history, email or trade was modified by verification; no branch or commit was created.

**Pre-existing failures or remaining limitations:**

The missing Python base runtime still requires the documented process-only workaround or operator repair. Torch is now installed; unrelated dependency inconsistencies, including missing `psutil`, remain. The seven recorded legacy feature-builder unpacking failures are not repaired or used as Stage 6 acceptance. Old controller tests/commands still have their pre-existing dependency/data assumptions and are not new-strategy verification. CUDA/AMP, worker processes and native MySQL/provider deployment were not exercised; tiny CPU and fake adapters supply the required acceptance evidence. Large-market training/export memory and throughput were not benchmarked. Live source history/publication evidence and historical revision reconstruction remain the previously documented limitations.

**Data/model rebuild or migration requirements:**

All old open-to-close models, datasets, caches, model manifests and resume checkpoints are incompatible with the new inference/training formats and require rebuilding from verified Stage 5 samples. There is no dimension-based or directory-discovery compatibility fallback. Existing user files are retained. Root model re-exports and explicit legacy controller/connection adapters are temporary and must be removed by Stage 9 after their callers migrate. The retained hardcoded key remains `DEFAULT_API_KEY` in `polygon_fetch.py`; its value is absent from new code, artifacts, metadata and documentation, and eventual externalization is not a prerequisite.

**Next stage and concrete prerequisites:**

Implement **Stage 7 only** next. Verify Stages 1-6 with the configured interpreter and runtime workaround; Torch is now available for tiny CPU fixtures. Build current/retrain recommendation workflows around explicit `ArtifactReference`/`load_artifact` or `load_ensemble`, `build_sample`, `predict_sample`, and `train_model`/`train_from_history`. Current must never train; retrain must use the returned artifact rather than CLI argument lists or a mutable training directory. Refresh and validate source readiness, using evidenced history/publication policy and actual receipt cutoffs, then resolve IDs after ingestion. Validate artifact/input/source/target/calendar compatibility and model information cutoff before avoidable side effects. Extract ranking, availability-safe eligibility and recommendation reporting into their owning modules; do not query entry-session finalized transaction counts before the decision. Migrate `choose_next_day_stocks.py` and `run_recommender_from_model_dir.py` without reconnecting the isolated legacy controller/NPZ/target path. Preserve explicit member semantics when using ensembles. Do not implement Stage 8 cash ledgers/planning/simulation or Stage 9's remaining batch/incremental/cleanup work.

### Stage 7 handoff - 2026-10-15

**Final integration:** Stage 7's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 8 has not started. Both recommendation policies run through shared refresh, read-only validation, previous-close construction, availability-safe liquidity, pure prediction, ranking and reporting. The two root recommendation scripts are thin frontends. Ranked suggestions explicitly do not enforce portfolio cash rest or record orders/fills.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/recommendations/configuration.py`: immutable `RecommendationJob`, `RetrainingPlan`, and `ArtifactKind`; `preparation.py`: `prepare_request`; `artifacts.py`: `load_recommendation_artifact` and `require_compatible_artifact`. All shortened package paths in this entry are under `src/stockguidance/`.
- `recommendations/eligibility.py`: `EligibilityPolicy`, `LiquidityObservation`, `EligibilityState`, `TickerEligibility`, pure `assess_eligibility`, and `eligibility_revision`; `recommendations/ranking.py`: pure `rank_scores`. `recommendations/request.py` extends the existing immutable request with validated eligibility and `top_k`, and rejects source-adjustment mismatches. `recommendations/result.py` retains its ranked-suggestion contract and clarifies readiness responsibility.
- `database/repositories/eligibility.py`: `EligibilityRepository` and `VerifiedEligibilityRepository`. `database/repositories/verified_closes.py` exposes `inspect(query) -> (CloseData, DatabaseInspection)`; its existing `read` delegates to the same verified projection. Transaction counts use those exact inspected, manifest-verified raw observations rather than an unchecked second database snapshot.
- `inference/scores.py`: `score_sample` ties pure Stage 6 predictions explicitly to the ordered ticker IDs. Prediction does not access storage, apply eligibility, rank, write files or send email.
- `workflows/recommend.py`: `recommend(job, boundary, calendar, services, clock, *, trainer=None) -> RecommendationRunResult`; `workflows/recommendation_dependencies.py`: `RecommendationServices` and `HistoryTrainer`; `workflows/recommendation_result.py`: structured `RecommendationRunResult`. Reuses the typed updater, validator, `build_sample`, `train_from_history`, explicit artifact loaders and shared prediction. It never constructs CLI strings or reads a mutable training directory.
- `reporting/recommendations.py`: allowlisted `recommendation_payload` and `render_recommendations`. Results include the exact request/specifications, entry/exit/cutoff/close times, artifact/members/weights, source and combined revision, refresh/feature revisions, feature identity, eligibility evidence, all aligned ticker scores, ranking, readiness and optional training results.
- `cli/recommend.py`: `build_parser`, `parse_args`, `main`; `cli/recommendation_configuration.py`: exact JSON decoding and validated configuration; `cli/recommendation_setup.py`: explicit dependency assembly from persisted history and configured roots. `market_data/providers/configured_polygon.py`: lazy `ConfiguredPolygonProvider` resolves credentials only when a download is needed. `pyproject.toml` registers the `recommend` console script for the next ordinary install.
- `choose_next_day_stocks.py` and `run_recommender_from_model_dir.py`: each now delegates directly to `stockguidance.cli.recommend.main`. Legacy private helpers, directory/SingleRun discovery, CLI-based training, numerical ID-window assumptions, current-session transaction lookup, inline duplicated credential, diagnostic branches, implicit model-directory reports and automatic email are removed from these frontends. No remaining consumer imports their removed private helpers.
- `tests/stage7/conftest.py`, `test_workflow.py`, `test_availability_and_artifacts.py`, `test_eligibility_and_ranking.py`, `test_refresh_and_readiness.py`, `test_provider_setup.py`, and `test_cli.py`: **76** deterministic acceptance tests. `tests/stage3/test_legacy_update_delegation.py` replaces its three obsolete AST tests with three shared-frontend contract checks; actual post-ingestion lookup and refresh failure behavior now have complete workflow tests in Stage 7.
- `docs/recommendations.md`: complete current/retrain JSON schema/examples, commands, eligibility/cutoff/revision/error contracts and limitations. `docs/market_data_updating.md` updates its recommendation delegation and credential-location notes. No dependency installation or environment replacement was performed.

**Decisions and contract/schema changes:**

- The application accepts exactly `current` and `retrain`. Current requires an explicit file plus `ArtifactReference` SHA256 identity, loads it before provider/database work, validates exact source/adjustment/feature/target/ticker/calendar/version semantics and every member's information cutoff, and never calls training. Retrain requires an explicit historical decision plan/configuration/destination, calls the typed training API once after readiness, and loads the **returned** reference even when its path differs from the requested destination. It does not discover model directories or reuse legacy artifacts.
- Preflight checks timing, supported-history bounds, full required lookbacks, coverage through the previous session, authoritative calendar/anchor and chronological training decisions before updating. The live updater records actual receipts; the final request/decision cutoff is captured **after successful refresh**. Read-only readiness, inference features, liquidity and the training-label cutoff use that same recorded instant. This avoids treating a receipt acquired after an initial preflight instant as already available then. The entry/exit sessions remain fixed, and success is rejected if refresh or fitting/prediction misses the entry close. There is no historical live-refresh/as-of option or fabricated publication evidence.
- Reconciliation covers all configured supported sessions, including leading gaps. Read-only validation assesses the exact model feature specification, and a separate inference sample must have the entire previous-close window. Newly imported date mappings are read after refresh; no entry-session finalized database ID or completed bar is required for a pre-close recommendation. Source/history/publication unknowns still fail rather than using guessed dates/delays or database minima.
- Liquidity uses the **immediately previous completed exchange session's** request-scoped verified full-day transaction count, available by the decision cutoff. The explicit default remains **200** transactions. Missing/null/unverified/late observations for any required ticker fail readiness; stale earlier observations and the unfinished entry session cannot substitute. Verified below-threshold counts exclude tickers. No eligible tickers means failure; fewer than `top_k` gives a shorter valid list. A configured zero minimum still requires an available observation. These are recorded policies, not inferred listing/trading status.
- Score alignment uses the artifact's exact ordered ticker universe; IDs are keys, never numerical offsets. Ranking sorts eligible finite scores descending, with numeric ticker-ID order for ties. There is no ensemble score normalization; embedded member identities/weights remain explicit. Allocation policy does not change model inputs, outputs or feature identities.
- All inputs/targets retain Stage 5/6 specifications. Training excludes immature outcomes, including the T-1 entry before T closes, and preserves exclusion dates/reasons. Historical features retain their original decision cutoff. Refreshing/correcting source evidence today does not prove prior availability; historical samples can become unavailable and a run with no mature complete training samples fails. Training metrics remain explicitly in-sample rather than an unbiased evaluation claim.
- `RecommendationRequest` adds immutable eligibility and `top_k` defaults while preserving earlier contract callers. `RecommendationRunResult` contains the ranked `RecommendationResult` plus scores, eligibility, sample, refresh/validation results, member semantics and optional training result. The combined source `data_revision` fingerprints refresh content, feature revision and actual eligibility evidence; feature identity still includes source/request/specification/calendar/timing/availability. No persistent prediction cache is introduced and no legacy cache is read.
- Reports state `kind=ranked_suggestions`, `orders_planned=false`, `fills_recorded=false`, and `cash_rest_enforced=false`, and identify Stage 8 portfolio-state planning as outstanding. Both allocation policies/explicit anchors are carried through requests/results. Stage 7 creates no cash ledger, sizing plan, simulated fill, allocation rebalance or trade submission.
- The CLI requires persisted evidenced history and the compatible existing updater schema; it never initializes/converts schema or guesses live provider limits. Only its dependency assembly selects Polygon; the typed workflow accepts injected adapters. New configuration has no credential field. The lazy provider honors `POLYGON_API_KEY` or the retained `DEFAULT_API_KEY` in `polygon_fetch.py`, whose value is absent from new code/docs/results/artifacts. Eventual externalization remains future work.
- Exit **0** means all required stages completed before entry close. Exit **2** means invalid CLI/JSON configuration; unsupported `entry_open` explicitly names Stage 10. Exit **1** means required setup/refresh/validation/artifact/training/prediction/reporting failure. Failures have sanitized stage/code/messages and cannot produce a successful recommendation payload. Output goes to stdout; no automatic model-directory report, email, diagnostic dump or trade is emitted.

**Interpreter and check commands:**

IDE discovery continues to select Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. Every Python invocation followed IDE discovery and used the existing process-local matching-runtime workaround; the missing base installation was not replaced or repaired:

```powershell
$stage7Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage7Runtime
$env:PYTHONPATH = "$stage7Runtime\python313.zip;$stage7Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

Commands from the repository root included:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/stage6 tests/test_simulate_trading_utils.py tests/test_sparse_linear.py tests/test_random_percent_linear.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage7 tests/stage3/test_legacy_update_delegation.py tests/stage1/test_imports.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/stage6 tests/stage7 tests/test_simulate_trading_utils.py tests/test_sparse_linear.py tests/test_random_percent_linear.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.recommend --help
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' choose_next_day_stocks.py --help
git diff --check
git diff --cached --check
```

Operational command forms, using operator-configured JSON and an artifact identity from training/export (not fixture dates as live configuration):

```powershell
& '<configured-python>' -m stockguidance.cli.recommend --config recommendation.json --policy current --artifact selected.sgmodel --artifact-id '<64-character-artifact-sha256>'
& '<configured-python>' -m stockguidance.cli.recommend --config recommendation-retrain.json --policy retrain --artifact retrained.sgmodel
```

For explicit ensembles add `--artifact-kind ensemble` in current mode. Both root frontends take the same arguments. Module/root commands work without refreshing editable installation; the new console command appears on the next normal install. New/untracked source, test, documentation, brief and packaging files also receive individual `git -c core.safecrlf=false -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --no-index --check -- NUL <path>` checks. No formatter was introduced.

**Checks run and results:**

- Verified the actual Stage 1-6 prerequisite baseline: **419 passed** in **70.92 seconds**; strict mypy passed on **132 source files**. No dependency installation was needed.
- Focused Stage 7/frontend/import suite passed **76 checks** before four final additional acceptance checks. The final integrated suite passes **495 tests** in **108.12 seconds**: **67 Stage 1**, **65 Stage 2**, **71 Stage 3**, **54 Stage 4**, **76 Stage 5**, **78 Stage 6**, **76 Stage 7**, **4 legacy window utilities**, and **4 existing sparse/random model tests**. Three existing Torch optimization warnings are unchanged. Strict mypy passes on **147 source files**, without suppressions/checker exceptions.
- Both entire CLI/workflow paths run with fake providers, actual temporary SQLite/fake-MySQL production adapters, manifests and tiny deterministic CPU models. Tests prove zero current-mode training calls, exactly one retrain call, use of a different returned artifact path, custom roots/database configuration, mature/excluded outcomes and exact training/inference feature parity. All required-stage exceptions/incomplete readiness and reporting failures produce failure, with no successful result or leaked exception credentials.
- Availability fixtures prove no entry-session finalized feature/liquidity query, exact previous-session eligibility, null/missing/late/stale-count rejection, threshold boundaries, sparse/reversed ticker alignment and stable ranking ties. Real fixture refreshes repair a leading gap, recreate a missing session mapping before validation/inference reads, fetch a missing manifest, and make its actual new receipt usable only at the recorded post-refresh cutoff. Provider failure and entry-close overruns prevent successful recommendations.
- Artifact fixtures reject missing/corrupt/changed valid references, equal-width feature changes, source mismatches and future model cutoffs before refresh/database work. Ensembles retain their explicit members/weights and match shared predictions. Both allocation policies produce identical model scores without claiming cash-rest enforcement.
- CLI checks cover argument parsing, both complete modes, JSON/text results, missing history without creating directories, invalid/legacy/unsupported specifications before dependency assembly, nonzero sanitized setup/workflow/reporting failures and root delegation. The documented complete JSON schema parses without live access. Lazy credential tests guard no import/transport before fetch, environment/default resolution and provider reuse without output.
- All-package import safety still passes from an empty directory, guarding application file/network/subprocess access and excluding Torch/NumPy/legacy imports. Module/root help exits 0. Searches find no remaining removed-private-helper consumers; `batch_run.py` retains only its explicitly deferred CLI-main integration. Tracked/staged and 25 new-file whitespace checks pass; ordinary CRLF notices are informational.
- Verification used only temporary source/history/model/database files and tiny CPU fixtures. No production database/schema/provider history/model/data directory, Java work, email or trade was modified. Unrelated existing changes and the Git index were preserved; no branch, commit, reset or stash was made.

**Pre-existing failures or remaining limitations:**

The configured interpreter's missing base runtime still requires the documented process-only workaround or operator repair. Previously recorded legacy feature-builder unpacking failures and unrelated dependency inconsistencies remain outside this stage. Native MySQL/Polygon deployment, CUDA/AMP and production-scale training were not exercised. Live supported-history/publication/correction policy evidence still requires operator configuration, and latest manifests cannot reconstruct overwritten historical revisions. A fresh archive download may therefore leave too few historically available training samples; the workflow reports failure instead of backdating them.

Remaining `StockRecommenderFromModel.py`, legacy trainer/simulation/batch/incremental/diagnostic/viewer paths are isolated migration work. `batch_run.py` still passes obsolete recommendation CLI arguments; that integration is not claimed migrated and belongs to Stage 9. The two supported recommendation frontends now intentionally reject those old arguments. Ticker IDs are reported explicitly without a legacy symbol/date-ID discovery fallback. Cash/state-aware order sizing, independent allocation ledgers, cash rest and chronological simulation remain Stage 8. Legacy connection/credential adapters and other cleanup remain Stage 9.

**Data/model rebuild or migration requirements:**

All legacy open-to-close models, directories/manifests, datasets and NPZ caches require rebuilding with Stage 5/6 specifications. Stage 7 does not convert, delete or relabel them. Inference/ensemble/checkpoint/source-manifest/history/database schema versions are unchanged. Existing typed requests gain optional validated eligibility/`top_k` defaults. Use explicit files/identities or typed retraining jobs and persist an evidenced history boundary before the supported workflow. No production files were rebuilt during verification.

**Next stage and concrete prerequisites:**

Implement **Stage 8 only** next. Verify the Stage 1-7 fixtures and strict mypy with IDE discovery and the configured-runtime workaround. Build portfolio ledgers, anchored allocation schedules and the deterministic shared order planner behind the existing `AllocationSpec`/cash-rest contracts. Reuse `assess_eligibility`, `rank_scores`, `build_sample`, explicit artifacts, `score_sample` and the exact target/cutoff/source semantics from this stage. Preserve each A/B balance independently and report actual supplied portfolio state before claiming cash-rest-backed sized recommendations. Simulation must apply separate fills/events, keep fixed pre-period models distinct from chronological retraining, and retain skipped-session parity and immature-label exclusions. Do not use the live-refresh recommendation API to invent historical source availability. Do not begin Stage 9 batch/incremental/frontend cleanup or optional Stage 10 opening inputs.


### Stage 8 handoff - 2026-10-15

**Final integration:** Stage 8's current commands and limitations are superseded by the shared Stage 1-8 integration addendum and completed Stage 9 handoff in this file. Use the full supported `tests` suite and strict mypy; removed source paths in this historical record are not supported APIs.

**Status:** Complete. Stage 9 has not started. Both allocation schedules now use independent cash-rest ledgers, deterministic shared order planning, chronological model scoring and explicit historical fills. Capital-aware recommendations produce plans without booking fills. The old 1,300-line simulation command is a thin CLI frontend.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/portfolio/state.py`: immutable `PortfolioState`, `AllocationState`, `Position`, `RestingProceeds`, `initial_portfolio` and authoritative `validate_calendar`. `precision.py`: `ExecutionPolicy`, Decimal/currency/share checks and sizing. `schedule.py`: `scheduled_allocation` and `buy_decision_index`; `ledger.py`: pure `release_proceeds`. All shortened package paths in this entry are under `src/stockguidance/`.
- `portfolio/planning.py`: `CashOrder`, `OrderPlan`, `PlanState` and shared pure `plan_orders`. `marks.py` and `valuation.py`: independent closing marks, position/allocation/portfolio valuations and `value_portfolio`. `serialization.py`: allowlisted portfolio schema, `portfolio_payload`, `decode_portfolio`, `load_portfolio` and strict decimal-string decoding. `portfolio/policy.py` updates its completed-ledger documentation.
- `recommendations/scoring.py`: shared `RankedPrediction` and `predict_ranking`, reusing Stage 7 eligibility/ranking and Stage 6 pure inference. `workflows/recommend.py` accepts optional actual `portfolio` and `ExecutionPolicy`, validates state before refresh, invokes the same planner after ranking and checks the entry-close deadline after planning. `workflows/recommendation_result.py` adds optional supplied state and order plan.
- `simulation/configuration.py`: immutable `SimulationRequest`, `EvaluationPolicy` and `ScorePolicy`. `score_source.py`: `HistoricalSignals`, `FixedModelScores`, `WalkForwardTraining`, `ChronologicalModelScores`, explicit `DecisionForecast` and source protocol. Historical scoring reuses `build_sample`, previous-session verified liquidity, `assess_eligibility` and the same `predict_ranking` as live recommendations.
- `simulation/prices.py`: exact-scoped `ExecutionFrame`, `ExecutionPriceSource` and `RepositoryExecutionPrices`, with an explicit retrospective audit cutoff. `execution.py`: separate `sell_positions`, `fill_orders` and `RealizedTrade`; `events.py`: immutable typed events; `benchmarks.py`: decision-frozen close-to-next-close outcomes and percentiles.
- `simulation/engine.py`: `simulate(request, boundary, calendar, scores, prices) -> SimulationResult`; `simulation/result.py` retains all events, plans, forecasts, training runs, execution evidence, equity, positions, trades and benchmarks. The engine iterates authoritative exchange sessions even when a decision/input/order is skipped. Package initialization remains import-safe.
- `reporting/orders.py`, `reporting/simulation.py` and `reporting/recommendations.py`: allowlisted order/portfolio/simulation JSON and readable output, including cash categories, independent balances, stale marks, actual versus intended returns, execution revisions and explicit planning/fill claims.
- `cli/simulation_configuration.py`, `simulation_setup.py` and `simulate.py`: exact JSON decoding, read-only dependency assembly, argument parsing and exit mapping. Root `simulate_trading.py` delegates only to the new CLI. `pyproject.toml` registers `simulate_trading` for the next ordinary install; module/root commands work without reinstalling.
- `cli/recommend.py` and `recommendation_setup.py` add `--portfolio-state`, initial-anchor-only `--capital`, and `--fee-per-fill`. Existing current/retrain model semantics and refresh/readiness requirements remain shared.
- `tests/stage8/conftest.py`, `test_portfolio.py`, `test_schedules.py`, `test_missing_data.py`, `test_benchmarks.py`, `test_chronology.py`, `test_recommendations.py`, `test_simulation_cli.py` and `test_documented_configuration.py`: **81** deterministic acceptance tests. Root `tests/test_simulate_trading.py` replaces three obsolete intraday/CSV tests with three new frontend/rejection checks. Two Stage 7 test files follow the extracted scoring seam and current portfolio-state wording.
- `docs/simulation.md`: both schedules, both model policies, complete JSON examples, execution/rounding/missing-data contracts, snapshot schema, public APIs and migration limits. `docs/recommendations.md` documents actual capital/state planning. No dependency installation, interpreter replacement or production data/model rebuild occurred.

**Decisions and contract/schema changes:**

- Session parity is anchored to `AllocationSpec.anchor_session.sequence`, never database IDs or the count of successful trades. Alternate A buys every other session; staggered A/B alternate buys across sessions. Initial staggered cash is split exactly once into equal whole cents; an odd-cent initial amount is rejected. A run beginning after the anchor starts with explicitly supplied initial evaluation cash while retaining that original parity; it does not invent earlier fills.
- Every allocation enforces cash + resting proceeds + position cost bases = its initial share + its own realized PnL. This prevents transfers, rebalancing and double counting. Sale proceeds become available at the next actual exchange-session **open**, never at the sale close. Availability transitions are idempotent and chronological. Initially unused B cash remains cash.
- Plans use equal fixed **maximum-debit cash budgets**, floored to cents, including the configured fixed fill fee. Entry prices and quantities are unknown at the decision. Simulation sizes fractional shares at the actual close to **eight decimals**, rounded down, with Decimal division precision **38**; gross buy cost rounds up to cents, gross sale proceeds down, and equity marks half-even. Default fee is **0.00**, charged only for actual fills on each side. Residual cents and unsuccessful budgets stay in their original allocation and are never redistributed.
- Ledger, plan and valuation operations are separate. Recommendations retain the actual supplied snapshot plus a projected eligible-state plan, validate all snapshot sessions against the authoritative calendar, and never create positions/sales/fills. Without state they remain ranked suggestions with no cash-rest claim. `orders_planned` reflects actual nonempty planned orders; `cash_rest_enforced` indicates that supplied-state planning ran; `fills_recorded` remains false. `--capital` is accepted only at the initial anchor, while later sessions require the full actual snapshot.
- Simulation processes next-session availability, pre-close decision/training/orders, closing sales/fills, benchmarks and marks. Plans are finalized before reading entry execution evidence or crediting that close's sale proceeds. Both schedules share exactly the same model/specification/sample/eligibility/scoring path. No application invokes a CLI or reads a mutable training controller.
- `fixed` requires the exact compatible Stage 6 artifact/ensemble. Every member's information cutoff must precede the first evaluation open and its final training exit must precede the evaluation entry date; future/in-period models fail before historical data reads. `chronological_retrain` filters the explicit expanding historical decision set at each fitting cutoff, calls `train_from_history`, excludes immature labels (including T-1 before T closes), and loads the returned artifact reference rather than assuming the requested destination.
- Retraining bootstraps at the first scheduled buy, then runs when the anchored scheduled-buy index is divisible by `every_buy_decisions`. Indices include missed inputs/orders and apply across A/B's combined buys. Cadence and every successful training result/cutoff/exclusion/reference are recorded, even if a later required prediction input is unavailable. A required fitting failure cannot silently reuse an older model. Chronological sources must be fresh for each evaluation.
- Expected missing features/liquidity skip a scheduled decision without changing parity; required unexpected training/prediction/execution failures prevent a successful run. Missing/suspended entry prices leave that order unfilled with its cash intact. Missing exits or insufficient proceeds to cover fees retain the whole position, report an unfilled exit and retry later closes. An open position blocks its allocation's scheduled purchase before a late same-close sale. End-period positions remain open; there is no forced future fill.
- A delayed exit records intended and actual exit sessions, actual gross/net returns and `exited_on_time=false`. Its intended next-session `close_return` stays unavailable; extended-period returns are never relabeled as the model's target. Missing marks carry the last observed close, with its date and `mark_current=false`; equity remains explicitly uncertain and incomplete rather than silently dropping a losing/unpriced position.
- Benchmarks freeze the decision-eligible universe and selected tickers before fills/outcomes. All targets, on-time fills, universe/selected means and strict-lower percentiles use `close(T+1) / close(T) - 1`. Missing universe outcomes invalidate the universe mean and percentiles; missing selected outcomes invalidate the selected mean. No survivor-only redistribution occurs. A final entry's unobserved next-session benchmark is an explicit expected boundary.
- Retrospective verified execution evidence uses a separately recorded `execution_audit_cutoff`, after decisions/orders, and cannot feed the signal/eligibility path. Every quote revision, explicit issue, full source identity and combined execution revision are reported. Historical signals retain their original receipt cutoffs; there is no live refresh/as-of invention. **Prediction caching is disabled** and no legacy saved-score/NPZ fallback is read.
- New standalone JSON schemas are **stockguidance-portfolio-1** and **stockguidance-simulation-1**. Financial configuration/snapshots require decimal strings and exact allowlisted fields. Existing feature/target/model/ensemble/history/source-manifest/database schemas are unchanged.
- Simulation exit **0** means successful and complete; **2** means invalid configuration or a completed run with skipped decisions/unfilled orders/stale marks/missing mature benchmark evidence; **1** means required setup/model/training/execution/reporting failure. Expected end-period open positions/immature benchmarks alone do not imply failure. Recommendation's existing 0/1/2 error contract is retained, with an explicit planning-stage failure if the entry close passes before sized plans are ready.

**Interpreter and check commands:**

IDE discovery selects Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`. Checks used that configured executable with the existing process-only workaround for its removed base installation. No environment files or installed dependencies were changed:

```powershell
$stage8Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage8Runtime
$env:PYTHONPATH = "$stage8Runtime\python313.zip;$stage8Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
```

After IDE environment discovery, commands from the repository root included:

```powershell
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/stage6 tests/stage7 tests/test_simulate_trading_utils.py tests/test_sparse_linear.py tests/test_random_percent_linear.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage8 tests/stage7 tests/test_simulate_trading.py tests/stage1/test_imports.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage1 tests/stage2 tests/stage3 tests/stage4 tests/stage5 tests/stage6 tests/stage7 tests/stage8 tests/test_simulate_trading.py tests/test_simulate_trading_utils.py tests/test_sparse_linear.py tests/test_random_percent_linear.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.simulate --help
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' simulate_trading.py --help
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m stockguidance.cli.recommend --help
git diff --check
git diff --cached --check
```

Operational simulation command forms (use configured JSON/history and actual artifact identities):

```powershell
& '<configured-python>' -m stockguidance.cli.simulate --config simulation.json --model-policy fixed --artifact pre-period.sgmodel --artifact-id '<64-character-artifact-sha256>'
& '<configured-python>' -m stockguidance.cli.simulate --config simulation-retrain.json --model-policy chronological_retrain --artifacts-directory models/walk-forward
& '<configured-python>' -m stockguidance.cli.recommend --config recommendation.json --policy current --artifact selected.sgmodel --artifact-id '<64-character-artifact-sha256>' --portfolio-state portfolio.json
```

Both allocation policies are explicit JSON settings. An initial-anchor recommendation may replace `--portfolio-state` with `--capital '1000.00'`. New/untracked files also receive individual `git -c core.safecrlf=false -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --no-index --check -- NUL <path>` checks. No formatter was introduced.

**Checks run and results:**

- Verified the actual Stage 1-7 prerequisite baseline: **495 passed** in **113.39 seconds**; strict mypy passed on **147 source files**.
- Final integrated fixture suite: **579 passed** in **134.31 seconds**: 67 Stage 1, 65 Stage 2, 71 Stage 3, 54 Stage 4, 76 Stage 5, 78 Stage 6, 76 Stage 7, **81 Stage 8**, three migrated simulation frontend tests, four existing legacy window utilities and four sparse/random model tests. Three existing Torch optimization warnings are unchanged. Earlier focused checks also pass; the final integrated run includes the last deadline/calendar/late-return regressions.
- Strict mypy passes on **170 source files**, without suppressions or checker exceptions. All-package import safety still passes from an empty directory without application file/network/subprocess activity, heavy-library imports or legacy-controller imports. New simulation module/root help and updated recommendation help all exit 0.
- Hand calculations verify alternate equity 1000/2000/2000/1000/1000 and staggered equity 1000/1500/1250/750/1000. A/B become 1000/250 independently; B's next budget remains 250. Fixtures prove idle B cash, exact anchored parity through July 4/weekend sessions, residual budgets, independent conservation and no double counting.
- A rising-price fixture yields **4000**, not the **8000** possible with illegal every-close proceeds reuse. A missing-exit fixture preserves **2000**, not the **10000** possible by recycling a late sale at the same close. Missing/losing/suspended positions, failed entries, stale marks, partial selections, end positions, fees and unavailable benchmarks remain visible without capital redistribution.
- Real tiny CPU artifacts run fixed scoring and chronological retraining for both schedules. Tests prove exact shared sample/model/target/scores, immature-label exclusions despite stored future rows, anchored retraining cadence through a skipped input, use of changed returned artifact paths, no stale-model fallback, future/in-period/equal-width semantic rejection and data-revision changes after corrections. No persistent prediction cache is used.
- Full capital-aware recommendation and simulation fixtures use the identical model, sample, ranking and order plan. Snapshot/capital CLI tests prove no hypothetical fills, initial-anchor enforcement, schema/precision validation, future-state rejection and refusal to succeed when planning crosses entry close.
- Both complete simulation CLI model policies and allocation schedules run with fake adapters. Another CLI fixture exercises the production verified manifest/SQL adapters against temporary SQLite/fake MySQL with remapped nonconsecutive/nonchronological IDs (901, 11, 450, 2, 100), producing exactly **1050.00** equity with SELECT/transaction-start SQL only, unchanged commit count and no provider calls. Documented JSON examples parse. Errors/unknown history/missing fills/report failures produce the expected nonzero sanitized results.
- Tracked/staged and **35 new/untracked-file** whitespace checks pass; CRLF normalization notices are informational. Verification uses temporary files/databases and tiny deterministic CPU jobs only. No production database/schema/provider/history/model/data directory, Java work, email or trade was modified. No branch, commit, reset, stash, staging or unstaging command was made.

**Pre-existing failures or remaining limitations:**

The configured interpreter still needs the documented process-only runtime workaround or operator repair. Previously recorded legacy feature-builder unpacking failures and unrelated dependency inconsistencies remain outside this stage. Live provider/MySQL deployment, CUDA/AMP and production-scale simulation/training/event-report memory were not exercised. Live supported-history/publication/correction evidence still requires operator configuration; overwritten historical revisions cannot be reconstructed and are never backdated.

Removing the root simulation monolith deliberately removes its old saved-score/directory/CSV helper APIs. Remaining `scripts/get_metrics_for_runs.py`, `viewers/compare_runs_viewer.py`, `viewers/compare_run_statistics_viewer.py`, `viewers/run_selection_dashboard.py`, `trainer/rep_handler.py` and `incremental_training_harness.py` still reference those helpers/obsolete output layouts and are **not migrated or supported by this stage's new frontend**. Their integration and remaining batch/legacy cleanup belong to Stage 9; no compatibility implementation reconnects them to the new economics. Java work is unchanged.

**Data/model rebuild or migration requirements:**

Legacy intraday/open-to-close model directories, models, datasets, NPZ caches and saved-score CSVs remain incompatible and require Stage 5/6 rebuilding. Nothing was deleted or relabeled. New simulation runs require explicit compatible artifacts or chronological training settings, evidenced persisted history and historically available raw/manifest evidence. Existing source/database/inference schema versions are unchanged. Portfolio/simulation JSON formats are new; no automatic migration from old CSV/controller state exists. The retained key remains `DEFAULT_API_KEY` in `polygon_fetch.py`, without its value in new code/docs/results; eventual externalization remains future work.

**Next stage and concrete prerequisites:**

Implement **Stage 9 only** next. Verify the 579-test integrated suite and strict mypy using IDE discovery/the existing runtime workaround. Migrate the actual remaining Python callers, particularly removed root simulation-helper consumers and obsolete score/CSV/viewer/metrics formats, onto explicit typed application APIs and current report schemas; do not restore legacy target or directory-discovery fallbacks. Finish batch/incremental/controller/diagnostic/email/run-filesystem extraction, remove temporary legacy connection/model/training adapters after migrating their real consumers, and document complete update/validation/training/current/retrain/fixed/chronological commands for both portfolios. Preserve explicit model/source/spec/cutoff identities, independent allocation conservation, missing-exit handling and real supplied recommendation state. Keep Java/user data/models unchanged unless an actual shared-format dependency requires documented coordination. Main refactoring is not complete until Stage 9's integrated acceptance criteria pass. Leave Stage 10's entry-session opening inputs optional.

### Stage 9 handoff - 2026-10-15

**Status:** Complete. Main refactoring (Stages 1-9) is complete. Package integration, remaining frontend migration, source retirement, focused decomposition and all required fixture/type checks pass. Stage 10 remains optional and deferred; no opening-price signal is implemented.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/database/configuration.py` and `database/connections.py`: validated explicit JSON configuration and direct lazy `MySQLConnectionFactory`, sanitized errors and repository-owned transactions. Temporary legacy connection/credential adapters and parent-directory discovery are removed. Read/write protocols remain; no live configuration/schema/database is changed.
- Training JSON/configuration and job/history assembly move to `config/training.py` and `training/jobs.py` (`load_job`, `open_job_history`, `execute_job`). Recommendation/simulation loaders and typed options live in `recommendations/jobs.py`, `recommendations/options.py`, `simulation/jobs.py` and `simulation/options.py`. Dependency assembly lives in `workflows/recommendation_runtime.py` and `workflows/simulation_runtime.py`, not CLI-private helpers.
- `workflows/batch_tasks.py`, `workflows/batch.py`, `config/batch.py` and `cli/batch.py`: immutable typed tasks/plans, exact `stockguidance-batch-1` parsing, preflight validation, direct application calls, actual returned artifact dependencies, atomic reports and fail-fast execution. No application constructs CLI arguments, discovers model directories or depends on mutable controller state.
- `cli/incremental.py` uses one chronological simulation ledger and requires `chronological_retrain`; it never stitches or rescales separate simulations. `cli/inspect_features.py`, `diagnostics/features.py` and `diagnostics/correlations.py` provide read-only seeded, chronologically sorted feature inspection and mature close-to-close correlations, preserving unavailable/excluded samples and exact identities.
- `reporting/documents.py`, `metrics.py`, `artifacts.py`, `storage.py`, `email.py` and `cli/reports.py`: explicit report/artifact inputs, schema/economics validation, compatible comparisons, total-equity return/drawdown, retained positions/incomplete marks, atomic persistence, email drafts and explicitly injected transport. No frontend sends email or places trades.
- `src/stockguidance/viewers/reports.py` and `viewers/artifacts.py`: lazy optional Streamlit, explicit report files or identified artifacts, no controller/CSV/cache fallback. Root viewer and page scripts now delegate to these implementations.
- `docs/stage9_retirement.md` is the requested exact deletion list grouped by reason: **43 training/model/controller adapters**, **15 feature/calendar/data utilities**, **17 obsolete tests**, and **10 CSV/directory/intraday benchmark utilities** are deleted (**85 total**). Numerical sparse/random/activation/initialization checks remain and import the preserved package models. No pytest exclusions hide legacy tests.
- **21 retained files** are rewritten in place as thin frontend delegations: root recommendation/batch/incremental/feature/correlation commands; database population commands; validation commands; reporting scripts; viewer/page scripts. Their exact paths and destinations are listed separately from deletions in the retirement report. Three obsolete benchmarks are deleted rather than misleadingly redirected to non-benchmark feature inspection.
- `polygon_fetch.py` retains the unchanged `DEFAULT_API_KEY` declaration and delegates its command to the shared updater; obsolete downloader/discovery code is removed. `pyproject.toml` packages that root module alongside `src/stockguidance` so lazy credential lookup works outside the checkout. The credential is never reproduced in new configuration, documentation, reports, logs or inference artifacts.
- `pyproject.toml` declares batch/incremental/inspection/report console commands and database/viewer extras. `README.md`, `docs/frontends.md`, `docs/model_training.md`, `docs/close_samples.md`, `docs/market_data_updating.md` and `docs/recommendations.md` now describe completed migration, current arguments, evidenced history, rebuild requirements, read-only validation/explicit repair, both recommendation modes and both allocation schedules.
- `tests/stage9/` contains **75 checks**, including `test_retired_frontends.py`'s **24 checks** for actual root/script/viewer delegation, retired-path absence, all supported callers' imports, and isolated wheel build/import/credential fallback. Existing prerequisite fixtures and numerical tests remain supported.

**Decisions and contract/schema changes:**

All supported inputs use `previous-close-v1`; targets/fills remain close T to next exchange-session close T+1. Decision cutoffs, mature fitting labels, previous-session eligibility, mandatory overnight cash rest and independently compounding A/B balances are unchanged. Allocation policy remains independent of model semantics. Explicit current artifacts and chronological retraining share the same sample/prediction/ranking/planning code.

Training and recommendation reports now identify `stockguidance-training-1` and `stockguidance-recommendation-1`; simulation remains `stockguidance-simulation-1`. Comparisons require identical source/feature/target/calendar/evaluation/capital/execution/eligibility/selection specifications and anchor, while displaying differing model/allocation policies. Cash/resting proceeds remain part of total marked equity. Incomplete marks/fills and end positions are never hidden.

Physical `legacy-open-to-close-v1` derived columns retain explicit storage-maintenance/audit semantics; they are not model inputs or targets. Changing those columns would require shared database/Java coordination. New numerical targets use verified raw closes. Java's legacy saved-score/CSV consumers do not receive an implicit compatibility format; Java sources remain unchanged.

**Checks run and results:**

- Final full supported suite: **660 passed**, **3 known Torch transformer optimization warnings**, in **129.37 seconds**. This includes all Stages 1-9, both recommendation modes, both allocation schedules, fixed/chronological simulations, real tiny CPU training/export/loading, and retained numerical model checks.
- Focused frontend retirement/packaging suite: **24 passed** in **4.93 seconds**. Earlier focused Stage 9 package suite: **51 passed** in **18.72 seconds**. All 75 also pass together in the full suite.
- Strict mypy: **no issues in 191 source files**, with no blanket suppressions or checker exceptions.
- All retained frontend wrappers import without executing applications and invoke exactly the owning package command once. AST checks confirm every reviewed retired path is absent and supported Python callers import no removed modules. The all-package import fixture still guards file/network/subprocess side effects and heavy/legacy imports from an empty directory.
- A wheel is built in a temporary project without installing or replacing the user's environment. It contains the package, typing marker and retained credential module, excludes old controller/model/database packages, and imports outside the checkout under file/network/process guards. Lazy provider assembly resolves the retained key with injected transport/provider only; no network request occurs.
- Batch fixtures run complete current/retrain recommendation paths for both schedules and propagate actual training-returned artifact references. Fixed and chronological evaluations run both schedules; recorded training exit/cutoff identities prove chronology. Required-stage/report failures and incomplete simulations stop execution.
- Default read-only validation detects a planned leading fixture gap without commits. Its explicit saved repair plan drives the real updater frontend over fake storage and passes a second validation. Integrity/schema manual-review requirements are not weakened.
- Report fixtures hand-check total equity/drawdown, retain open/unpriced positions, and reject incompatible schemas/economics. Diagnostic fixtures preserve unavailable windows and mature outcomes without zero filling. Database/CLI/artifact tests verify sanitized failures and no credential/controller dependency. Fake-UI and injected-email tests perform no live browser/email action.
- Tracked/index and new-file whitespace checks pass. The local recovery archive has **107 original entries** and unchanged SHA256 `4CFEABD1485B96F7BCF7E1CB0EF057C7A39F2E31047EDAD53A0D0EA9021908A5`; the retained credential declaration is checked against that archive without displaying its value.
- The user committed originals at `fae92d5` before cleanup; Git was clean when resumed. Normal patching needed elevated `apply_patch` access for 32 Windows-protected source/test files; the scoped approved patch completed. Earlier approval blockers are resolved. No branch/commit/reset/stash/staging/unstaging command is issued by the agent.
- Verification touches temporary fixture storage only: no live database/schema mutation, production downloads/training, user model/data cleanup, Java change, email or trade.

Commands use IDE-discovered Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`, and the existing process-only matching-runtime workaround. Discover the environment before each Python invocation.

```powershell
$stage9Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage9Runtime
$env:PYTHONPATH = "$stage9Runtime\python313.zip;$stage9Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage9/test_retired_frontends.py -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
git diff --check
git diff --cached --check
git -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --no-index --check -- NUL tests/stage9/test_retired_frontends.py
```

The last command may exit 1 because the new file differs from NUL; whitespace diagnostics, not that expected difference status, determine failure. The wheel/import check runs within the focused/full pytest suite. Refresh installed console metadata only during the next ordinary install; module commands and checkout wrappers already use the new implementation.

**Pre-existing failures or remaining limitations:**

The configured base Python installation is missing, so ordinary commands still need the documented workaround or operator repair. Unrelated dependency inconsistencies remain. Live source history/publication/correction evidence must be supplied by the operator, not guessed from earliest rows. Current manifests cannot reconstruct overwritten historical revisions. Native MySQL/Polygon, CUDA/AMP, production-scale performance and a live Streamlit browser session are not fixture verification claims.

All supported Python callers are migrated and the full suite passes. There is no remaining Stage 9 approval/cleanup blocker. The Git-ignored recovery archive must remain local because it preserves original source/private configuration literals. User data/models and Java work are retained.

**Data/model rebuild or migration requirements:**

Rebuild old intraday/open-to-close datasets, NPZ caches, artifacts, checkpoints and saved scores from verified shared samples; no compatibility fallback or dimension-only relabeling exists. Keep user data/model directories. Install appropriate training/database/optional viewer extras and refresh console metadata through the configured environment when deploying. Existing database/history/source/inference/portfolio/simulation schemas retain their versions.

**Next stage and concrete prerequisites:**

No further main-refactoring stage is required. **Stage 10 remains optional and deferred**, and must be explicitly requested. It would require evidenced pre-close entry-opening observations for the full ticker universe, a distinct recorded feature/artifact specification, and deterministic availability/selection/schedule comparisons without changing close-to-close execution or cash rest. Do not begin it as part of Stage 9.

### Stage 10 handoff - 2026-10-15

**Status:** Complete. The explicitly requested optional enhancement is implemented and its acceptance checks pass. Stage 9 was verified before implementation: the user-committed checkout was clean, the full baseline suite passed **660 tests** with three known Torch optimization warnings in **131.65 seconds**, and strict mypy passed on **191 source files**. Stage 9's retirement, delegation, import-safety and isolated-wheel checks were included; no prerequisite cleanup was repeated.

**Implemented paths and public APIs/commands:**

- `src/stockguidance/market_data/openings.py`: immutable `OpeningObservation`, `OpeningQuery`, `OpeningData` and runtime-checkable `OpeningRepository` contracts. `market_data/opening_store.py` supplies atomic receipt persistence and read-only, cutoff-aware retrieval. `database/repositories/signals.py` composes existing verified closes and stored openings through `SignalRepository` without changing public training API signatures.
- `market_data/providers/openings.py` and `providers/configured_openings.py`: focused `PolygonOpeningProvider` and lazy configured provider assembly. `market_data/opening_refresh.py` supplies full-universe `OpeningRefresh`, obtains ticker symbols from verified prior-session observations and recovers partially completed captures without discarding earlier valid receipts. `providers/http.py` accepts the exact bounded minute endpoint; `providers/credentials.py` shares lazy credential resolution with the daily provider. The existing key declaration in `polygon_fetch.py` is unchanged and its value is not copied.
- `features/specification.py`, `features/construction.py`, `features/opening_values.py`, `datasets/building.py` and `artifacts/decoding.py`: explicit opening feature order/layout, sample availability, data revisions and persisted artifact compatibility. `training/jobs.py`, `simulation/score_source.py` and `workflows/simulation_runtime.py` read stored opening evidence for training, diagnostics and simulation; they do not capture historical data or backdate availability.
- `workflows/recommend.py`, `workflows/recommendation_dependencies.py` and `workflows/recommendation_runtime.py`: both `current` and `retrain` automatically capture the current full-universe opening after daily reconciliation and before the final cutoff. Historical daily readiness stays close-only; the exact inference sample checks current openings, and historical retraining excludes unavailable original opening samples.
- `validation/openings.py`, `validation/close_samples.py`, `validation/findings.py`, `validation/repair.py`, `workflows/validate_database.py` and CLI validation assembly: standalone read-only opening checks, the `entry_open_observations` finding and the explicit `capture_opening_evidence` proposal. Opening evidence is not silently treated as an automatic daily-data repair.
- `simulation/signal_comparison.py`: `compare_signal_modes` and `SignalComparison`, with matched training and decision cohorts. `workflows/signal_comparison_runtime.py` assembles the chronological workflow. `cli/update_openings.py` and `cli/compare_signals.py` expose focused commands, registered in `pyproject.toml`; existing training/recommendation/simulation/feature-inspection/validation frontends accept explicit opening specifications.
- `docs/entry_open.md` records the exact JSON contract, provider documentation, commands, operational limitations, missing-data policy and reproducible comparison. `README.md`, `docs/frontends.md`, `docs/close_samples.md`, `docs/model_training.md`, `docs/recommendations.md`, `docs/simulation.md`, `docs/database_validation.md` and the Stage 10 deferral wording in `docs/stage9_retirement.md` are updated. No source files or user data/model directories are deleted in Stage 10.
- `tests/stage10/conftest.py`, `test_opening_samples.py`, `test_opening_provider.py`, `test_workflows.py`, `test_comparison.py`, `test_frontends.py` and `test_capture_recovery.py`: **56 deterministic checks**. Earlier unsupported-mode assertions now require the explicit opening channel, and credential fixtures patch the shared lazy resolver. No test collection exclusions are added.

**Decisions and contract/schema changes:**

- `entry_open` retains the same completed-session close transformations and appends `entry_open_over_previous_close_minus_one`, namely `open(T) / close(T-1) - 1`, as the final channel for every ordered ticker. This stateless channel is repeated across the historical rows of the explicit `(lookback_sessions, ticker_count, feature_count)` array. Feature version is `entry-open-v1`; its layout records the repetition. `previous-close-v1` and its artifact fields remain unchanged. No fitted scaling or future-session normalization is introduced.
- Opening acquisition uses `/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}`, bounded to the exchange opening millisecond through +59,999 ms, ascending, limit 1 and explicit source adjustment. It reads only `o` after validating status, ticker, adjustment, count, exact bucket timestamp and absence of continuation. It never requests the entry session's completed daily bar. The inspected official Custom Bars documentation at `https://massive.com/docs/rest/stocks/aggregates/custom-bars` returned HTTP 200 and confirms millisecond bounds, split adjustment, extended-session bars and absent bars without qualifying trades; no live market-data API request was used for verification.
- The precise opening method is `first_regular_session_minute_open-v1`: the first qualifying trade in the first regular-session minute, **not a separately certified official auction price**. A security that first trades later is unavailable under this version. Capture begins after that minute completes and must finish before the actual exchange close, including early closes. Observed time is the opening bucket boundary; availability is the actual response receipt, never the original date of a historical download.
- Receipts use `stockguidance-opening-1` under `<data-root>/market_data/openings/<source-and-method-hash>/<date>/<ticker-id>.json`, with exact session/calendar, source/method, price, receipt time and content/response identities. Corrections and receipt changes affect revisions. Missing, late, corrupt or unverified evidence for **any ordered ticker** makes the entire sample unavailable. Never fill openings, shrink the universe, substitute daily-bar opens or reinterpret old artifacts. Artifacts persist endpoint, method, availability, missing policy, transformation, mode, preprocessing and layout; equal tensor width does not establish semantic compatibility.
- Targets and executions remain close T to next exchange-session close T+1. Eligibility remains previous-session based; fitting labels must be mature at each training cutoff. Both allocation schedules retain anchored parity, mandatory overnight sale-proceeds rest and independently compounding A/B balances. Missing decisions do not remove sale/mark sessions, transfer capital or shift the schedule.
- The paired comparison uses the intersection of eligible mature samples at each fitting cutoff and skips both modes at a decision when either input is unavailable. Period, ordered universe, close transformations, capital, schedule, execution/eligibility, training configuration, seed and retraining cadence are shared. Excluded fitting dates/cutoffs and decision dates/reasons are explicit in `stockguidance-signal-comparison-1`, beside both normal simulation reports. This is a matched-cohort comparison, not an unconstrained availability comparison. Generic report comparisons still reject incompatible feature specifications.

**Checks run and results:**

- Final full supported suite: **716 passed**, **3 known Torch transformer optimization warnings**, in **145.47 seconds**. It includes the Stage 1-9 regression suite, import-time guards and isolated wheel/credential checks. Both signal modes, recommendation policies and allocation schedules remain covered.
- Focused Stage 10 suite: **56 passed** in **19.59 seconds**. Fixtures prove opening-driven selection changes without changed close fills; full-universe availability and exact ordering; strict provider/receipt boundaries; pre-open, delayed, empty, corrupt and partial captures; revision invalidation; future-daily-data exclusion; artifact mismatch rejection; read-only validation; current/retrain integration and supported frontend behavior.
- Small aligned-price chronological comparison: **4 passed** in **6.53 seconds**, covering both allocation policies with complete and missing opening evidence. Labels, feature inputs and execution closes use the same fixture history. The recorded July 5-11, 2024 experiment starts with **1000.00**, seed **42**, one tiny CPU epoch and retraining every **two anchored buy decisions**. Complete-evidence final equities are **1799.97 / 524.99** for alternate sessions and **3224.95 / 1833.88** for staggered halves (previous-close / entry-open). Missing July 2/5 evidence for one ticker excludes July 2 from both fitting cohorts and July 5 from both decision paths; final equities are **499.99 / 499.99** and **1821.38 / 1821.38**, respectively. These synthetic results demonstrate reproducibility/cohort accounting, not profitability; see `docs/entry_open.md` for the table and command.
- Strict mypy: **no issues in 204 source files**. No blanket suppressions, checker exceptions or new formatter are introduced.
- Tracked/index and **21 new-file** CRLF-aware whitespace checks pass. Java changes: **0**; changed index entries: **0**. No branch, commit, reset, stash, staging or unstaging command is issued. Checks use temporary fixture files and tiny CPU jobs, not live database/schema mutations, production downloads/training, email or trades.

Commands use the IDE-discovered Python **3.13.9**, `C:\Projects\StockGuidance\.venv\Scripts\python.exe`, package manager `pip`, and the existing process-only matching-runtime workaround. Discover the configured environment before each Python invocation.

```powershell
$stage10Runtime = Join-Path $env:TEMP 'stockguidance-python3139-stdlib'
$env:PYTHONHOME = $stage10Runtime
$env:PYTHONPATH = "$stage10Runtime\python313.zip;$stage10Runtime;C:\Projects\StockGuidance\.venv\Lib\site-packages"
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage10 -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests/stage10/test_comparison.py -q -s --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m pytest tests -q --tb=short --maxfail=5
& 'C:\Projects\StockGuidance\.venv\Scripts\python.exe' -m mypy
git diff --check
git diff --cached --check
git -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --no-index --check -- NUL docs/entry_open.md
```

Repeat the last check for each new source/test file. A new-file comparison may exit 1 solely because the file differs from NUL; whitespace diagnostics determine failure. The focused comparison prints its recorded summary; normal `stockguidance.cli.compare_signals` emits the full paired JSON report. Refresh installed console metadata through the configured environment during deployment; module commands already use this implementation.

**Pre-existing failures or remaining limitations:**

The configured base Python installation is still missing; ordinary commands require the existing process-only workaround or operator repair. Unrelated dependency inconsistencies remain outside this stage. Native MySQL/Polygon, live minute entitlements/publication/delay/rate limits, CUDA/AMP, production-scale performance and a live browser are not fixture-verification claims. Daily history metadata enforces a lower bound but does not establish minute-history entitlement. Large universes may not finish throttled per-ticker requests before the close; capture then fails rather than silently narrowing coverage.

Original historical opening receipts cannot be reconstructed or backdated from downloads made today. Overwritten historical revisions remain unrecoverable. Historical training/comparison requires retained receipts available at the original cutoffs; an existing daily database alone is insufficient. This implementation deliberately does not promise an official auction price or support later-minute substitutes. The retained credential remains at `DEFAULT_API_KEY` in `polygon_fetch.py`, with eventual external configuration still future work; its value is absent from new receipts, reports, artifacts and documentation. User data/models, the local recovery archive and Java work are retained.

**Data/model rebuild or migration requirements:**

Train distinct opening-mode artifacts from explicit `entry_open` JSON and evidenced original opening receipts. Do not toggle a previous-close artifact's mode or relabel tensors based on dimension. Existing previous-close artifacts remain compatible; old intraday/open-to-close datasets, artifacts and caches still require the previously documented rebuild. Existing database/history/source/inference/portfolio/simulation schema versions are unchanged; opening receipts and paired comparison reports add separate schemas. No automatic migration from legacy scores or daily-bar openings exists.

**Next stage and concrete prerequisites:**

No further numbered stage is required: **Stages 1-10 are complete**. Operator deployment requires evidenced source/history/publication settings, appropriate minute access, retained contemporaneous opening receipts, separately trained compatible artifacts and refreshed console metadata. These operational prerequisites are not permission to invent historical availability, change cash-rest economics or start a production experiment.
