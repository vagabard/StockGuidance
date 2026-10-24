# Entry-session opening signals

Stage 10 adds `entry_open` alongside `previous_close`. Both buy at entry-session
close T and sell at the next exchange-session close T+1. An opening price is
selection information, never an execution price. Previous-session liquidity,
same-close execution assumptions, mature labels, cash rest and independent A/B
balances are unchanged. No full intraday system or brokerage integration is added.

## Exact feature contract

Use an explicit full ordered universe; never restrict opening requests to the
stocks selected later. In training, recommendation and simulation JSON, replace
the `features` object with, for example:

```json
{
  "ticker_ids": [17, 3],
  "lookback_sessions": 3,
  "signal_mode": "entry_open",
  "interval": "daily",
  "missing_history": "unavailable",
  "feature_order": [
    "close_over_first_window_close_minus_one",
    "entry_open_over_previous_close_minus_one"
  ],
  "adjustment": "adjusted"
}
```

IDs are illustrative, not a live universe. The closing-history transformations
remain exactly those of `previous_close`: L completed exchange sessions before
T, in their original order. `log_close` may also be explicitly configured.
The last channel must be `entry_open_over_previous_close_minus_one`, namely
`open(T) / close(T-1) - 1` for each ticker. It is repeated across the L historical
rows to keep an explicit rectangular `(L, ticker_count, feature_count)` layout
without inventing an entry-session closing bar. No scaling is fitted. The
close-only mode rejects this channel; the opening mode rejects a missing channel.

Version is `entry-open-v1`; layout records the repeated opening channel. Artifacts
also persist the exact opening endpoint, method, actual-receipt availability,
full-universe missing policy and stateless preprocessing. Feature/data revisions
include source, adjustment, opening content/provenance/receipt time, method and
the existing close revisions. Cutoffs, ticker order, transform and model identity
remain part of their appropriate sample/prediction identities.

Train a distinct artifact for each signal. Equal tensor widths are not proof of
compatibility, and changing a JSON mode cannot convert an existing artifact.
Previous-close artifact fields/semantics remain unchanged; no old artifact,
daily-bar-open, saved-score or cache fallback is introduced.

## Opening acquisition and provenance

Polygon supports per-ticker minute aggregates using
`/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}`. This adapter requests only
the first regular-session minute, with exact exchange-open millisecond bounds
through open + 59,999 ms, ascending order, limit 1 and the source adjustment flag.
It reads only that aggregate’s `o` as the opening signal. It does not request the
entry session’s daily bar, a premarket aggregate or a later opening substitute.

The inspected [official Custom Bars contract](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
confirms millisecond bounds, split adjustment, extended-session coverage and empty
intervals when no qualifying trades occur. Hence regular-session timestamp bounds
and exact returned-bucket checks are required, rather than a date-only request.

The precise operational definition is **the first regular-session minute’s first
qualifying trade**, not a separately certified official auction price. A security that first
trades in a later minute is unavailable under this version. That conservative
choice is recorded as `first_regular_session_minute_open-v1`; a different method
requires a new specification and compatible artifacts.

Capture starts only after that minute completes and must finish before the entry
close, including on early-close sessions. Responses must identify the exact
ticker, adjustment, first-minute timestamp, successful status and complete count,
without continuation. Errors, delayed/unverified responses and empty first
minutes cannot establish complete coverage. No waiting for final daily data,
zero filling, forward filling or dropping tickers is allowed.

`OpeningStore` atomically persists `stockguidance-opening-1` receipts under
`<data-root>/market_data/openings/<source-and-method-hash>/<date>/<ticker-id>.json`.
They contain only the opening, exact session/calendar/observed-open time, actual
receipt time, non-secret source/method identity and response/content hashes.
The observed time identifies the provider bucket's exchange-open boundary, not
an invented timestamp for its first individual trade.
Corrections change revisions. Corrupt, incompatible or late receipts are visible
as unavailable/unverified, never used as successful model inputs. Every query
accounts for every requested ticker/session exactly once.

Daily history metadata is a lower-bound safety constraint, not evidence of minute
history entitlement. Live minute access/delay/rate limits must be checked against
the operator’s provider plan. Per-ticker throttling can prevent a large universe
from completing before the close; that is a required-stage failure, not permission
to shrink the universe. No live provider entitlement or history date is assumed.

The retained key remains `DEFAULT_API_KEY` in `polygon_fetch.py`, overridden by
`POLYGON_API_KEY`, and resolved lazily by both providers. Its value is not copied
into receipts, reports, artifacts or documentation.

## Commands and readiness

Use the configured interpreter and the existing runtime workaround in
`REFACTORING_PROMPTS.md` when necessary. Establish evidenced supported history and
reconcile completed daily data first, following [frontends.md](frontends.md).

```powershell
& $stockguidancePython -m stockguidance.cli.update_openings --config opening-training.json --session '<entry-session-date>'
& $stockguidancePython -m stockguidance.cli.train_model --config opening-training.json --artifact output/opening.sgmodel
& $stockguidancePython -m stockguidance.cli.recommend --config opening-recommendation.json --policy current --artifact output/opening.sgmodel --artifact-id '<SHA256 identity>'
& $stockguidancePython -m stockguidance.cli.recommend --config opening-retrain.json --policy retrain --artifact output/opening-retrained.sgmodel
& $stockguidancePython -m stockguidance.cli.validate_database --data-root data --dbinfo-file dbinfo.id --ticker-ids '17,3' --lookback-sessions 3 --signal-mode entry_open --publication-delay-minutes '<evidenced delay>' --publication-evidence '<evidence>' --format json
```

`update_openings` uses a training job only for explicit source/universe/history
configuration; it never trains. Its session must be in the loaded calendar and
supported history, with current capture after the first minute and before close.
Both recommendation policies automatically capture the current full-universe
openings after daily reconciliation and before the final decision cutoff. Symbols
come from verified previous-session data, never the unfinished daily bar.
Historical daily readiness remains close-only; current opening readiness is
checked by the exact inference sample. Retraining additionally excludes each
historical sample whose own opening was unavailable at its original cutoff.

Standalone validation remains read-only and audits all required opening receipts
in the requested feature scope. Missing/corrupt evidence uses the distinct
`entry_open_observations` finding and `capture_opening_evidence` proposal. Such a
proposal is not an automatic daily-data repair and cannot backdate receipts.
Training/simulation/diagnostics read stored openings only, without network calls.
Downloading historical minute data today does not prove availability at an old
decision; absent original receipts remain excluded. Existing historical revision
reconstruction limitations also apply to openings.

Both normal simulation schedules accept opening artifacts through the existing
fixed/chronological commands in [frontends.md](frontends.md). Missing openings
skip the whole decision, retain cash/positions, preserve allocation parity and
leave an explicit incomplete result. Sales/marks still occur on their calendar.

## Fair chronological comparison

```powershell
& $stockguidancePython -m stockguidance.cli.compare_signals --config chronological-simulation.json --artifacts-directory output/paired > output/signal-comparison.json
& $stockguidancePython -m pytest tests/simulation/test_comparison.py -q -s
```

The JSON configuration supplies one allocation schedule, period, exact universe,
close transformations/lookback, initial capital, execution/eligibility rules,
chronological training budget, cadence and seed. The comparison derives the two
signal specifications and trains distinct artifacts under explicit mode-specific
directories. Rerun with the other allocation policy for a separate comparison.

At each fit cutoff, both modes use the **intersection of eligible samples**;
labels must be mature, and both original input cutoffs must be satisfied. At
each anchored scheduled decision, missing inputs in either mode skip both.
Sales and valuation sessions are not deleted. Excluded training dates/cutoffs
and excluded decisions/reasons are reported explicitly beside both complete
simulation reports in `stockguidance-signal-comparison-1`. This estimates a
matched-cohort comparison, not either mode’s unconstrained availability advantage.
Generic report comparisons still reject different feature specifications; only
this explicit paired workflow compares signal modes.

Exit 0 requires both simulations complete; 2 means incomplete paired data/fills
or invalid arguments/configuration; 1 means required execution failure. Opening
capture returns 1 for a failed capture and argparse uses 2 for invalid syntax.
Tests use tiny CPU fits, fake providers, temporary storage and fixed seed 42;
they are not a production experiment or evidence of profitability.

### Recorded fixture comparison

The focused comparison command passes four cases over July 5–11, 2024, with
initial capital `1000.00`, a one-epoch tiny CPU model, seed 42 and retraining every
two anchored buy decisions. Training labels, features and closing fills use the
same fixture close history. Results include retained end positions and cash.

| Allocation | Opening evidence | Previous-close final equity | Entry-open final equity | Shared skipped decisions |
|---|---|---|---|---|
| alternate_sessions | Complete | 1799.97 | 524.99 | 0 |
| staggered_halves | Complete | 3224.95 | 1833.88 | 0 |
| alternate_sessions | Missing July 2 and July 5 for one ticker | 499.99 | 499.99 | 1 |
| staggered_halves | Missing July 2 and July 5 for one ticker | 1821.38 | 1821.38 | 1 |

July 2 is excluded from both fitting cohorts when its opening is missing;
July 5 is skipped by both decision paths. Later sales/marks and allocation parity
remain anchored. These synthetic outcomes demonstrate reproducibility and
cohort accounting, not a recommendation to prefer either signal.
