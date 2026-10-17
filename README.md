# StockGuidance

Daily recommendations use historical closing prices available through the
previous completed exchange session. The strategy buys at entry-session close
and sells at the next trading session's close, with mandatory overnight rest
for sale proceeds. Both `alternate_sessions` and independent `staggered_halves`
allocations use the same feature, target, eligibility and order-planning code.

Start with [frontend commands and migration notes](docs/frontends.md).
For day-to-day operation, date-subset training and simulation, read the
[operations guide](docs/operations_guide.md). The
[Luna maintenance prompts](docs/luna_maintenance_prompts.md) record the
completed automatic history discovery, archive preservation and measured Java
work.
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
rebuilding. The completed Stage 10 workflow also supports the distinct
[entry-opening signal and paired chronological comparison](docs/entry_open.md),
without changing close-to-close fills. Java remains an opt-in read-only
validator; Python is the semantic and operational default.
