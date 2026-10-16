# Recommendations using current or retrained models

Both policies reconcile the entire configured available coverage, validate it,
construct `previous-close-v1` samples, check liquidity, predict, and rank.
`current` loads an explicit compatible artifact before updating and never trains.
`retrain` calls the typed training API once and loads its returned artifact.
Legacy model directories, controller state, NPZ caches and open-to-close models
are incompatible; rebuild with [model_training.md](model_training.md).

Run before the identified entry session closes. Inputs contain only the exact
historical closing-price window through its previous exchange session. The
decision cutoff is captured **after refresh**, so actual new receipts can be
available. Validation, feature construction, liquidity and label maturity use
that same cutoff. The command fails if refresh crosses the entry close or if
training/prediction finishes after it. It does not backdate newly downloaded
history. Historical replay uses the [chronological simulator](simulation.md).

The requested entry fills at its close, and exit is the next exchange session's
close under the explicit `pre_close_decision_same_close_fill` simulation
assumption. Scores estimate raw `close(T+1) / close(T) - 1`. No order is submitted.

## Setup and configuration

Use the IDE-configured interpreter. An evidenced, source-specific supported
history boundary must already exist under `<data_root>/history/`. Establish it
through the [updater](market_data_updating.md); database minima are insufficient.
The updater's compatible schema must also exist. Recommendation never initializes
or alters schema. Unknown live history/publication entitlements still require
operator evidence; no production date or publication delay is assumed.

This is a **fixture-only schema example**, with hypothetical IDs/dates/policies.
Replace its source evidence, paths, ticker IDs, lookback, dates and policy
parameters with the exact model/source configuration. `start_date: null` means
the persisted supported-history boundary; an explicit date cannot precede it.
Coverage must include all required previous closes and requested training inputs.
`end_date` must cover the previous session and cannot exceed the entry date.

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
  "data_root": "data",
  "database_config": "dbinfo.id",
  "features": {
    "ticker_ids": [17, 3],
    "lookback_sessions": 3,
    "signal_mode": "previous_close",
    "interval": "1d",
    "missing_history": "unavailable",
    "feature_order": ["close_over_first_window_close_minus_one"],
    "adjustment": "adjusted"
  },
  "start_date": null,
  "end_date": "2024-07-09",
  "entry_date": "2024-07-09",
  "allocation": {"policy": "alternate_sessions", "anchor_date": "2024-07-01"},
  "publication": {"delay_minutes": 15, "evidence": "Fixture-only publication policy"},
  "corrections": {
    "recent_window_days": 30,
    "recent_refresh_hours": 24,
    "archive_refresh_days": 30,
    "evidence": "Fixture-only correction policy"
  },
  "eligibility": {"minimum_transactions": 200},
  "top_k": 5,
  "retraining": null
}
```

Save a configured job as `recommendation.json`. Current mode requires both a
file and its content identity, obtained from the training/export result:

```powershell
& '<configured-python>' -m stockguidance.cli.recommend --config recommendation.json --policy current --artifact models/selected.sgmodel --artifact-id '<64-character-artifact-sha256>'
```

For an explicit Stage 6 ensemble use `--artifact-kind ensemble` and its ensemble
identity. Members, ticker semantics and weights remain exactly as exported;
there is no per-member score normalization or directory discovery. Every member's
information cutoff must precede the decision cutoff. Source, adjustment, feature
order, ticker order, target and calendar/version must match, even at equal widths.

For retraining, replace the job's `retraining: null` with an explicit history and
model/training configuration. All existing Stage 6 model/resource choices are
available; omitted training/model options use the documented typed defaults.

```json
{
  "start_date": "2024-07-03",
  "end_date": "2024-07-08",
  "decision_minutes_before_close": 30,
  "model": {"hidden_widths": [4], "final_activation": "none"},
  "training": {"epochs": 2, "batch_size": 2, "device": "cpu", "seed": 31}
}
```

```powershell
& '<configured-python>' -m stockguidance.cli.recommend --config recommendation-retrain.json --policy retrain --artifact models/retrained.sgmodel
```

Training entries precede the recommendation entry. Only complete mature outcomes
enter fitting; excluded dates and reasons remain in the result. In particular,
an entry at T-1 is immature before T closes. If refreshed historical evidence
cannot establish availability at earlier decisions, those samples are excluded;
the workflow fails when none remain. It never fabricates historical availability.

Both root commands, `choose_next_day_stocks.py` and
`run_recommender_from_model_dir.py`, accept these same arguments and delegate to
the same CLI. Their former switches are removed. The `recommend` console script
is registered on the next ordinary editable install. No install refresh is
required for module/root commands in this checkout.

## Eligibility and results

The liquidity rule uses the **immediately previous completed session's** verified
full-day transaction count, published by the decision cutoff. The explicit default
threshold remains 200. Missing/null/unverified/late counts fail readiness, even if
other tickers qualify. Earlier stale counts or the unfinished entry session's
finalized counts cannot substitute. A verified count below the minimum excludes
that ticker; an empty eligible universe fails. A threshold of zero still requires
an available count. No ticker lifetime or missing count is inferred.

Prediction is pure: `inference.scores.score_sample` returns finite scores tied to
the artifact's exact ordered ticker IDs. `recommendations.eligibility` assesses
availability independently, and `recommendations.ranking.rank_scores` filters and
sorts eligible scores descending, breaking ties by numeric ticker ID. Fewer than
`top_k` eligible tickers produces a shorter list without adding excluded tickers.

Default stdout is JSON; `--format text` produces readable output. Results include
entry/exit/cutoff/close times, artifact and ensemble members, source scope,
combined data revision, refresh/feature revisions, feature identity, exact
feature/target/allocation/eligibility specifications, all ticker scores, eligibility
evidence, ranking, coverage readiness and optional training results. Credentials,
database configuration and controller state are excluded. Reports do not write
into model directories or send email. Output can be saved explicitly by the caller.

Both `alternate_sessions` and `staggered_halves` use the shared deterministic
portfolio planner. Without supplied capital/state, output remains a **ranked
suggestion**, with `orders_planned: false`, `fills_recorded: false`, and
`cash_rest_enforced: false`.

For a capital-aware plan, supply `--portfolio-state portfolio.json`. The snapshot
schema and cash-rest rules are documented in [simulation.md](simulation.md).
For the initial entry **at the allocation anchor only**, `--capital '1000.00'`
declares actual initial cash instead. Later entries require the actual complete
snapshot, including positions and resting proceeds. `--fee-per-fill '1.00'`
sets an explicit fixed fee; the default is zero. Initial staggered capital must
split equally into whole cents.

The result includes the supplied snapshot and an `order_plan` with fixed maximum
debits per ticker, projected eligible cash, fees, timing and a reason when no buy
is scheduled or possible. `cash_rest_enforced` then becomes true; `orders_planned`
reports whether the plan actually contains orders. Maximum debits include the
fee and do not assume a known entry close;
fractional quantities are determined only at execution. A failed fill keeps its
budget in that allocation. Recommendations never change the supplied state or
record hypothetical fills: `fills_recorded` remains false.

Exit 0 means all required stages completed and suggestions are ready before the
entry close. Exit 2 means invalid arguments/configuration. Exit 1 means required
setup, refresh, validation, artifact, training, prediction or reporting failure;
there is no successful recommendation payload following a required failure.
`entry_open` fails explicitly as the unimplemented Stage 10 enhancement.

The typed application entry point is `workflows.recommend.recommend`, taking a
`RecommendationJob`, evidenced boundary, calendar, `RecommendationServices`,
injected clock and optional typed trainer, `portfolio: PortfolioState` and
`execution: ExecutionPolicy`. It returns `RecommendationRunResult`.
Source/provider/database adapters are injectable; only the concrete CLI selects
Polygon. Newly imported session mappings are read after refresh. No application
code builds CLI strings or reads mutable training-directory properties.

The retained provider key is `DEFAULT_API_KEY` in `polygon_fetch.py`, eventually
to become external configuration. The new lazy provider uses `POLYGON_API_KEY`
when present and loads the retained constant only if a download is needed. Its
value is absent from new reports/configuration/artifacts. Native provider/MySQL
deployment and historical revision reconstruction remain separate limitations.
Stage 9 still owns the remaining legacy batch/incremental/diagnostic consumers.
