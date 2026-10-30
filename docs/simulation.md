# Cash-rest portfolios and chronological simulation

Both allocation schedules buy at entry session T's close and sell at the next
exchange session's close. Decisions precede the entry close and use the same
`previous-close-v1` samples, explicit artifacts, previous-session liquidity,
prediction and ranking as recommendations. The simulation never refreshes or
backdates historical data. Use the updater and read-only validator separately;
persist an evidenced source history boundary first. See
[market_data_updating.md](market_data_updating.md),
[database_validation.md](database_validation.md) and
[model_training.md](model_training.md).

## Schedules and accounting

`alternate_sessions` uses allocation A at anchor, anchor+2, anchor+4, etc.
`staggered_halves` splits initial cash equally into A/B **once**: A buys at anchor,
B at anchor+1, A at anchor+2, and so forth. Each sells at its next close. The
calendar's stable session ordinals determine parity through holidays, weekends,
missing records, failed predictions and missed orders. Database IDs never
determine dates or cadence. A simulation can begin after the explicit anchor;
initial capital is all cash at the evaluation start, with the original parity.
It does not claim trades occurred before that start.

Each allocation conserves its initial share plus its own realized PnL. It has
separate available cash, positions, resting proceeds and fees. Transfers and
rebalancing fail that invariant. A sale creates resting proceeds; they become
available at the **next exchange session open**. They cannot fund a purchase at
the sale close. B can buy at A's sale close using B's own existing cash.

For example, start with 1,000, split into 500/500, and closes
100, 200, 100, 50, 100. After the third close, A is worth 1,000 and B 250.
The next B purchase has a 250 budget; total equity is never split again.
Equity is available cash + resting proceeds + marked positions, counted once.

The shared `portfolio.planning.plan_orders` creates immutable **cash-notional**
plans. Each chosen ticker receives the same fixed maximum debit, rounded down
to cents. That debit includes the configured fixed fill fee. Entry closes are
unavailable to selection/sizing; fractional quantities are calculated at the
closing fill under `pre_close_decision_same_close_fill`. The simulator supports
fractional shares to eight decimals, rounded down; gross buy costs round up to
cents, gross sale proceeds down, and marks use half-even cents. Residual cents
remain in their allocation. The default fee is `0.00`; it applies only to actual
fills, on both sides. Quantity division uses decimal precision 38. Financial
configuration/state values are decimal strings, never JSON floats.

Missing/suspended entry quotes leave the affected budget in cash without
redistributing it to successful orders. Missing exits retain the entire position
and retry at later closes. A retained position blocks that allocation's next
scheduled purchase, even if its late sale ultimately fills at that close. Such
trades report both the intended next-session exit and actual exit, with
`exited_on_time: false`. Their next-session `close_return` remains unavailable;
`actual_price_return` and net return describe the extended actual holding period.
Fees exceeding sale proceeds also leave the exit unfilled.
No position is discarded or replaced with a zero return after its outcome is
known. End-period positions remain open and marked; there is no forced sale or
read of a future exit. A missing mark carries the last observed close, identified
by its session and `mark_current: false`; that equity is uncertain, explicitly
incomplete, and does not create cash.

## Model chronology and evidence

`fixed` requires an explicit Stage 6 model/ensemble file and SHA256 identity.
Every member's information cutoff must precede the first evaluation open, its
last training exit must precede the evaluation entry date, and all exact
source/feature/target/ticker/calendar specifications must match. A model fitted
during the evaluation period is rejected before signal/execution reads.

`chronological_retrain` fits on the supplied expanding historical decision set,
filtered at each scheduled buy decision. Training uses the shared
`train_from_history` API and excludes every unavailable/immature label. In
particular, T-1's entry outcome is still immature before T closes, even if future
rows exist in storage. The first scheduled buy bootstraps a model. Subsequent
fits occur when the **anchored scheduled-buy index** is divisible by
`every_buy_decisions`; skips still consume an index. For cadence 2, alternate
sessions retrain at buy indices 0, 2, 4; staggered halves do likewise across
A/B's combined scheduled buys. Failed training never silently reuses an old
artifact. Returned artifact references, including changed destinations, are
loaded and checked. Use a fresh chronological source for each run.

Training outcomes and metrics, excluded dates, cutoffs and artifacts are retained
even when a fit is followed by an unavailable prediction input. Training metrics
remain in-sample. Reports distinguish fixed pre-period evaluation from
chronological retraining and make no blanket claim of unbiased performance.
Simulation decision leads must place decisions after each session's actual open
and before close, including early closes.

Signal/eligibility reads retain their historical availability cutoffs. The
separate `RepositoryExecutionPrices` adapter reads verified retrospective close
evidence at an explicit `execution_audit_cutoff`, only after each session's
decisions/orders have been recorded. Those prices implement historical fills
and marks; they never enter that session's signal or liquidity. Provider receipt
time does not become the simulated trade time. The report records the audit
cutoff, every execution quote's revision, explicit price issues, and a combined
execution revision. Historical receipt/revision evidence overwritten by a later
correction cannot be reconstructed; unavailable inputs are skipped/reported.
There is no prediction cache or legacy CSV/NPZ score fallback.

Benchmarks freeze the eligible universe and selection at the original decision.
Every return and strict-lower percentile uses `close(T+1) / close(T) - 1`.
A missing universe outcome makes its equal-weight mean and percentiles
unavailable; a missing selected outcome also invalidates the selected mean.
There is no survivor-only redistribution. A final entry's future benchmark
remains unavailable as an expected end boundary.

## Commands and configuration

Use the configured interpreter after IDE environment discovery. All dates,
publication delays and paths in this example are **fixture-only**, not live
provider entitlement/history/publication evidence. Replace the source identity,
feature/ticker specification and paths with those of the rebuilt artifact and
persisted history. No schema initialization, data updates, provider requests,
email or trade submission occur in simulation.

```json
{
  "source": {
    "provider": "polygon",
    "endpoint": "/v2/aggs/grouped/locale/us/market/stocks/{date}",
    "market": "us-stocks",
    "universe": "us-stocks-excluding-otc",
    "adjustment": "adjusted",
    "interval": "1d"
  },
  "data_root": "fixture-data",
  "database_config": "fixture-db.id",
  "features": {
    "ticker_ids": [17, 3],
    "lookback_sessions": 2,
    "signal_mode": "previous_close",
    "interval": "1d",
    "missing_history": "unavailable",
    "feature_order": ["close_over_first_window_close_minus_one"],
    "adjustment": "adjusted"
  },
  "start_date": "2024-07-08",
  "end_date": "2024-07-11",
  "availability_policy": "strict",
  "data_snapshot": null,
  "allocation": {"policy": "alternate_sessions", "anchor_date": "2024-07-08"},
  "initial_capital": "1000.00",
  "decision_minutes_before_close": 30,
  "publication": {"delay_minutes": 15, "evidence": "Fixture-only publication"},
  "execution_audit_cutoff": "2024-07-11T21:00:00+00:00",
  "eligibility": {"minimum_transactions": 200},
  "top_k": 1,
  "fee_per_fill": "0.00",
  "retraining": null
}
```

The same parser-valid, non-secret fixed-period fixture is checked in at
[`examples/stage5/fixed-later-simulation.json`](../examples/stage5/fixed-later-simulation.json).
It uses disposable `fixture-data`/`fixture-database.json` paths and strict
previous-close inputs; replace those paths and source identity with the rebuilt
artifact's verified storage before an operational run. The corresponding
chronological-retraining configuration is
[`examples/stage5/chronological-retraining-simulation.json`](../examples/stage5/chronological-retraining-simulation.json).

```powershell
& '<configured-python>' -m stockguidance.cli.simulate --config simulation.json --model-policy fixed --artifact models/pre-period.sgmodel --artifact-id '<64-character-artifact-sha256>'
```

Set `allocation.policy` to `staggered_halves` for the second schedule. Initial
capital must split equally into whole cents. For a fixed exported ensemble add
`--artifact-kind ensemble`. The `simulate_trading` console script becomes
available on the next ordinary installation; module/root commands work directly.

For chronological retraining replace `retraining: null` with:

```json
{
  "start_date": "2024-07-03",
  "end_date": "2024-07-11",
  "decision_minutes_before_close": 30,
  "every_buy_decisions": 2,
  "model": {"hidden_widths": [4], "final_activation": "none"},
  "training": {"epochs": 2, "batch_size": 2, "device": "cpu", "seed": 31}
}
```

```powershell
& '<configured-python>' -m stockguidance.cli.simulate --config simulation-retrain.json --model-policy chronological_retrain --artifacts-directory models/walk-forward
```

The walk-forward example begins before the evaluation range so its first model
has bootstrap history. Each retraining artifact is written only to the explicit
`--artifacts-directory`; it is never discovered from an experiment directory.

The only writes are explicitly requested retraining artifacts. Output is JSON
on stdout by default or text with `--format text`. Reports contain event-ledger
transitions, plans, models/scores, execution evidence, cash categories,
independent balances, realized PnL/returns/fees, marked equity, open positions,
stale marks and benchmarks. Exit **0** means a successful complete run; **2**
means invalid configuration or a completed run with skipped decisions, missing
fills/marks/mature benchmarks; **1** means a required setup/model/training/
execution/reporting failure. Expected end-period open positions and immature
benchmarks do not by themselves make a run incomplete. Inspect `successful`,
`complete`, events and per-benchmark completeness separately.

Simulation configuration accepts the same `availability_policy` values as
training. `strict` is the default and uses actual receipts at each historical
decision cutoff. `retrospective_daily_previous_close` requires the explicit
aware `data_snapshot` and the matching research artifact; it applies the
evidenced close-plus-publication-delay assumption to verified previous-close
features, labels and liquidity while preserving actual receipt times. The
policy and snapshot are reported, included in sample/cache and artifact
identities, and must match fixed or chronological-retraining evaluation. It
rejects `entry_open`, future/unpublished inputs and immature labels. Results
must be described as retrospective research, not original point-in-time replay;
current corrected/adjusted history and the chosen universe can contain hindsight.

## Capital-aware recommendations and state

Recommendation accepts `--portfolio-state portfolio.json` or, at the initial
anchor only, `--capital '1000.00'`. It calls the same `plan_orders` function but
never applies fills or mutates the snapshot. Later entries require supplied
actual state; a ranked list alone does not enforce cash rest. Fees can be set
with `--fee-per-fill '1.00'`. See [recommendations.md](recommendations.md).

An initial staggered snapshot has this exact allowlisted schema:

```json
{
  "schema": "stockguidance-portfolio-1",
  "as_of": "2024-07-08T19:00:00+00:00",
  "initial_capital": "1000.00",
  "policy": "staggered_halves",
  "anchor_session": "2024-07-08",
  "allocations": [
    {"name": "A", "available_cash": "500.00", "realized_pnl": "0.00", "fees_paid": "0.00", "positions": [], "resting": []},
    {"name": "B", "available_cash": "500.00", "realized_pnl": "0.00", "fees_paid": "0.00", "positions": [], "resting": []}
  ]
}
```

A position contains `ticker_id`, decimal-string `quantity`, `entry_session`,
intended `exit_session`, decimal-string `entry_price` and `cost_basis` including
entry fee. Resting proceeds contain decimal-string `amount`, `sale_session` and
`eligible_session`, which must be the next authoritative exchange session.
Snapshot timing cannot follow the recommendation decision. Positions must
already have filled, follow their allocation schedule, and remain in the
snapshot until an actual sale is recorded. Every allocation's cash + resting
proceeds + position cost bases must equal its initial share + realized PnL.
`portfolio.serialization.portfolio_payload/decode_portfolio` round-trip these
states; a simulation's `final_portfolio` is a simulated snapshot, not evidence
that real trades happened.

The public simulation API is `simulation.engine.simulate(request, boundary,
calendar, scores, prices) -> SimulationResult`. The score sources are
`FixedModelScores` and `ChronologicalModelScores`, sharing `HistoricalSignals`;
the latter takes `WalkForwardTraining`. Pure ledger/planning/state/valuation
functions live under `portfolio/`; historical fills/events/benchmarks live under
`simulation/`; rendering lives under `reporting/`. No application invokes a CLI.

Old open-to-close models, score CSVs, NPZ caches and numeric-date simulation
switches are incompatible. Rebuild models/datasets under Stages 5/6. Stage 9
removes legacy viewer/metrics/controller/incremental callers of root helpers and
obsolete formats; all supported frontends use typed packages. Stage 10 adds the
[entry-opening signal and matched chronological comparison](entry_open.md), using
stored original receipts and the same execution/schedule rules. Missing openings
skip anchored decisions without dropping positions or moving sale sessions. The
retained hardcoded key is `DEFAULT_API_KEY` in `polygon_fetch.py`; eventual
external configuration remains future work and its value is never copied here.

The cross-stage acceptance fixture also proves the fixed-model chronology: the
subset artifact's final mature label precedes the later simulation range, the
simulation performs no retraining, and feature/label reads do not cross each
decision cutoff. Use the reported artifact ID rather than an archive file hash.

## Stage 10: train-then-simulate, paired schedules, and replay

The configurable simulation defaults are one selected ticker, `1000.00` initial
capital, and ten exchange sessions. `train_then_simulate` fits one artifact on
the explicitly supplied pre-evaluation decisions, requires their labels to be
mature before the first evaluation decision, and reuses that artifact for the
whole simulation. `chronological_retrain` remains an explicit expanding-history
policy; fixed-artifact simulation remains unchanged.

`simulation.persistence.persist_simulation_result` writes the versioned
`stockguidance-simulation-2` report and the run manifest's prediction,
eligibility, exclusion, execution-quote, trade, equity, benchmark, and
provenance sections. The report retains all ordered model-universe scores, not
only the selected top-n, so a changed simulation top-n can be checked rather
than silently approximated. Missing entries, exits, and end positions retain
the engine's existing explicit accounting semantics.

`simulation.orchestration.simulate_paired` runs alternate-session and
staggered-half schedules against one memoized forecast set and independent
portfolios. `simulation.persistence.replay_saved_simulation` creates a REPLAY
child run and consumes only saved scores and execution quotes; it does not load
a model, query a database, or contact a provider. A changed allocation schedule
requires saved scores for every affected session, and a changed top-n requires
the full saved model universe. `rerun_pinned_simulation` creates a separate
RERUN child using explicitly supplied typed score/price sources and records the
chosen data revision; it never mutates the parent run.

Failure manifests retain the exception type and message for local diagnosis;
the run-contract sanitizer redacts credential-shaped values. CLI output remains
generic, and no production database, live provider, or trade endpoint is used
by replay or rerun services.
