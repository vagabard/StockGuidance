# StockGuidance

Daily recommendations use historical closing prices available through the
previous completed exchange session. The strategy buys at entry-session close
and sells at the next trading session's close, with mandatory overnight rest
for sale proceeds. Both `alternate_sessions` and independent `staggered_halves`
allocations use the same feature, target, eligibility and order-planning code.

Start with [frontend commands and migration notes](docs/frontends.md).
Stage 9 cleanup is documented in the [deletion list grouped by reason](docs/stage9_retirement.md);
retained thin frontends are listed separately from deletions.
Detailed specifications cover [updating](docs/market_data_updating.md),
[read-only validation](docs/database_validation.md),
[features and targets](docs/close_samples.md),
[training and artifacts](docs/model_training.md),
[recommendations](docs/recommendations.md), and
[simulation](docs/simulation.md).

Use the configured PyCharm interpreter. Live operation requires evidenced source
history/publication/correction settings and compatible storage. No live supported
history date is assumed. Legacy open-to-close datasets, caches and models require
rebuilding. `entry_open` remains the optional, unsupported Stage 10 enhancement.
