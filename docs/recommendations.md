# Recommendations using current or retrained models

## Date flags and automatic ticker selection

The normal command refreshes required data, selects tickers, trains a matching
model, and ranks recommendations. Run from the project directory:

```powershell
& .\.venv\Scripts\python.exe -m stockguidance.cli.recommend --start-date=-1y --format text
& .\.venv\Scripts\python.exe -m stockguidance.cli.recommend --start-date -1y --trim-dollar-volume-percent 5 --format text
& .\.venv\Scripts\python.exe -m stockguidance.cli.recommend --start-date=-1y --trim-dollar-volume-percent 5 --volume-sample-sessions 40
& .\.venv\Scripts\python.exe -m stockguidance.cli.recommend --start-date 2026-09-01 --tickers AAPL MSFT
```

`--start-date` accepts an ISO date, `-Ny` calendar years, or `-Nd` calendar days.
The offset is relative to `--end-date`, which defaults to today in New York.
Leap-day year offsets clamp to February 28. Both separated and equals forms
of negative offsets work. Start dates advance to the first exchange session;
end dates use the latest completed, published session in the requested range.
Warm-up history is additional and does not change the membership endpoints.

Before today's close the entry is normally today. After close, on weekends,
and on holidays, the entry is the next exchange session. Publication delays
can postpone data readiness; the default is zero minutes after close. An
explicit historical end generates a labeled historical recommendation for
the next session. It uses current revised data with session chronology, not
original receipt-time reconstruction. Source preparation uses real operational
time; only historical scoring uses a replay clock.

With no ticker list, the candidates are the intersection of tickers having a
good positive close at the two resolved endpoints. A shared coverage filter then
requires a good positive close on **every exchange trading session in the
requested period**, before dollar-volume trimming. Missing rows, invalid states,
and unusable closes exclude the ticker. Weekends and exchange holidays do not
count. This also applies to explicit lists, preserving surviving ticker order.
Reports record excluded IDs, symbols, missing-session counts, and coverage timing.
For now coverage is fixed at 100%; a configurable percentage is not exposed.
Membership is frozen for the trained model. Additional warm-up history can still
exclude individual training samples. If no complete samples remain, the command
fails and saves `training-exclusions.json` alongside the model destination.
Explicit symbols are case sensitive and cannot be combined with nonzero
percentile trimming. Saved artifacts must match the surviving ticker dimensions.

Dollar-volume trimming defaults to **0%**, so no dollar-volume aggregate is read
in that case. When enabled, rank by average `volume × close` across valid
observations in the period. Missing observations are not zero-volume trades;
tickers without a measurement are excluded and reported. Remove
`floor(measured_candidate_count × percentage / 100)` bottom candidates, with
stable ticker-ID tie breaks. Percentages must be finite and in `[0,100)`.
The fixed previous-session transaction minimum also defaults to **0**; use
`--minimum-transactions` to enable it explicitly. Existing JSON settings with
an explicit minimum remain authoritative unless overridden.

The full-period aggregation runs in SQL and transfers only per-ticker totals.
`--volume-sample-sessions N` explicitly chooses at most N evenly spaced
sessions, including both endpoints, with N at least 2. There is no automatic
switch to sampling. Sampling only affects dollar-volume ranking: the coverage
filter always checks all requested sessions, including when trimming is 0%.
Versioned caches under `data/.universe-cache` reuse valid
aggregates; changes in scoped data/repair versions, settings, or algorithm
version invalidate them. Overlapping pending maintenance blocks selection.

Every CLI run prints filter timing to stderr, preserving JSON stdout. New
reports include requested/resolved dates, selection and filter times, endpoint
counts, selected symbols, exclusions, measurements, sample dates, and cache
status under `resolution`. Current-history reports do not claim original
point-in-time replay.

Sampling can change which tickers survive the percentile trim; it is an
explicit speed/precision choice. The report records the original date
expressions and the requested sample size as well as the resolved dates.

## Optional model settings and saved models

The one-year per-ticker z-score run has an explicit configuration:

```powershell
& .\.venv\Scripts\python.exe -u -m stockguidance.cli.recommend --config recommendation-one-year-zscore.json --start-date=-1y --trim-dollar-volume-percent 5 --format text
```

It uses `price_delta_zscore_20` as its sole input channel and the existing
20-session per-ticker return z-score as its target. The target standardizes
`close(X) / close(X-1) - 1` using that ticker's prior 20 mature returns.
Raw returns remain attached to training labels and simulation realized returns
continue to use actual close prices. Per-ticker z-score rankings measure unusual
gains relative to each ticker's history. The command without this configuration
retains its relative-close input and raw-return target defaults.

`--config` is optional. It can supply a model/settings template without dates,
tickers, or a source; for example:

```json
{
  "features": {"lookback_sessions": 20},
  "model": {"hidden_widths": [64, 64, 64]},
  "training": {"epochs": 80, "batch_size": 256, "device": "auto"},
  "top_k": 5,
  "runs_root": "output/recommendations"
}
```

Use this template with `--start-date=-1y`. Explicit flags override its date,
ticker, percentile, sampling, minimum-transactions, and path settings. Defaults
come from the database source, `dbinfo.id`, `data`, 20 previous closes, raw-return
targets, five returned tickers, and the existing typed model/training defaults
(80 epochs). Missing start dates are rejected rather than inferred.

`retrain` is the default and creates a fresh artifact inside its saved run.
An explicit `--artifact` selects the destination. `--policy current` requires
`--artifact` and `--artifact-id`; its ticker order, features, source, and target
must match the resolved selection. Retrain when the dynamic universe changes.
Full legacy JSON jobs below continue to work without date/selection overrides.
Their explicit training and entry dates remain unchanged.

## Historical investment simulations

The existing `simulate` command accepts an optional `recommendation` object in
its simulation JSON:

```json
"recommendation": {
  "start_date": "-1y",
  "trim_dollar_volume_percent": 5,
  "volume_sample_sessions": 40
}
```

Use it with `--model-policy chronological_retrain` and the existing retraining,
portfolio, fee, and allocation settings. At each scheduled retraining, the
shared selector resolves the trailing period against that decision's previous
session. It never uses the final simulation date for earlier membership.
Between retraining boundaries, the artifact and membership remain fixed.
The ledger retains old positions and benchmark quotes when a later model's
universe changes. Reports save each decision's universe and support offline
score/quote replay without reloading a model or accessing the database.

The outer simulation `features` object continues to use the existing schema;
its ticker IDs are replaced by the first resolved recommendation universe.
To preserve an explicit list in this mode, supply `tickers` inside the
`recommendation` object. A nonzero percentile trim remains incompatible with
an explicit list.

This integration uses the existing simulator's previous-close v1 feature and
raw-return target contract. New flexible recommendations also accept configured
feature templates, subject to their existing stored-channel readiness checks.

## Legacy explicit configuration

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

The following is a **fixture-only schema example**, with hypothetical IDs/dates/policies.
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

Use `python -m stockguidance.cli.recommend` from a checkout. The `recommend`
console script is registered after an ordinary editable install. The former root
commands and their legacy switches are retired.

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
## Stage 9: saved-artifact recommendations

Current recommendations use the selected artifact as the authority for the
feature tensor, target semantics, fitted preprocessing, ordered ticker IDs,
and (for versioned artifacts) ticker labels. The workflow does not rebuild a
universe from today's surviving rows or override model dimensions from the
configuration. Versioned scores are emitted after artifact-owned output
preprocessing is inverted and retain explicit `score_units` (`fractional_return`
for raw-return targets and `z_score` for transformed targets).

Use `--refresh-mode validate-only` when the required history or prepared
target database should only be checked. This mode verifies the same bounded
coverage, source identity, completed-session evidence, receipt cutoffs, and
timing as refresh mode, but makes no provider calls and performs no writes.
Refresh mode remains the strict production default. A stale boundary,
unfinished preparation operation, missing receipt, or entry-close deadline
still fails the recommendation.

Every attempt is saved below the configured runs root (`runs` by default, or
`runs_root` in the configuration). A completed recommendation contains an
external artifact reference, the ordered pre-top-n scores, every eligibility
assessment and exclusion, score units, eligible count, odd/even medians, and
`reports/recommendation.json`. Versioned reports include stable ticker labels;
ties are ordered by stable numeric ticker ID. Versioned target jobs may
successfully return fewer than `top_k`, including an empty selection, with
null medians. Legacy raw-return v1 jobs preserve the prior nonempty-result
failure behavior.

The result is informational only. Stage 9 does not submit orders, send
notifications, modify production databases, migrate schemas, or download
live data when validate-only is selected. SQL/database preparation remains a
separate bounded, inspectable workflow; the recommendation run records its
resulting revision and provenance rather than silently preparing schema.

Stage 10 [entry-opening recommendations](entry_open.md) require the explicit
opening channel, a compatible distinct artifact and timely captured observations
for the full ordered universe. Both model policies capture current openings after
daily reconciliation; no unfinished daily bar enters the signal or eligibility.

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
Stage 9 migrates all supported batch/incremental/diagnostic consumers and removes
legacy controller/discovery paths. Package frontends are the only supported
entry points; see [frontends.md](frontends.md) and the
[grouped retirement list](stage9_retirement.md).
