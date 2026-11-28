# Prepared history for training and simulation

Versioned-database commands resolve dates, select a fixed ordered ticker list
with SQL coverage counts, stream the required values into float64 arrays,
and slice windows as model batches need them. Selection and loading share a
repeatable-read snapshot. The default is 1,000 tickers per query and 8,192 rows
per cursor fetch. No expanded array of all overlapping windows is retained.
Normalization fits only eligible training cases using incremental statistics.

Tickers need usable data across training and simulation, including lookbacks
and required outcome dependencies. Configured stored channels must be ready.
Approved interpolated raw observations count as usable. Missing ticker values
exclude that ticker. Incomplete sessions, missing channel completion metadata,
and unfinished database operations require maintenance. A final live prediction
or end-period open position does not require an outcome beyond the period.

Run `update_market_data` separately. Training, simulation, and recommendation
loading never download or repair data. A normal update maintains registered
z-score series and includes their largest window in its context. Series and
physical tables must already be configured; consumers never create them.

New database runs record `availability_policy: current_prepared`. They read
current effective values without modification or historical receipt timestamps.
Trading-date chronology and mature training labels still apply. Existing
configuration availability fields remain accepted, but the database path
reports its effective current-prepared semantics. Legacy/injected repositories
retain their earlier availability contracts. Old artifacts remain readable;
different availability semantics require retraining for the new path.
Checkpoint identity includes the loaded values and layout.

Recommendation-style simulations freeze membership across the full training
and simulation period and reuse the history on each fit. Results describe this
retrospectively complete universe.

Training, simulation, and recommendation configurations accept optional bounds:

```json
"data_loading": {
  "ticker_batch_size": 1000,
  "fetch_rows": 8192,
  "max_array_bytes": 536870912
}
```

The memory limit is optional and covers base arrays, not total process memory.
Allocation is estimated before value loading. Keep data-loader workers at zero
to avoid copying history to worker processes on Windows.

Timings go to stderr. Training saves `<model>.data.json` beside the artifact;
training and simulation reports include `data_preparation`. Measurements cover
phases, per-query execution/fetch/consumption, rows, array bytes, and accumulated
case materialization time. Execute timing can include driver/network buffering;
it is not server-only query time.

## Read-only benchmark

```powershell
.venv/Scripts/python.exe tools/benchmark_prepared_history.py --cases 20 --tickers 1000 --lookback 20
```

On November 27, 2026, the existing database supplied 899 eligible tickers from
the first 1,000 IDs. With Python allocation tracing enabled:

| Measurement | Result |
| --- | ---: |
| Bulk selection and loading | 2.475 seconds |
| Construct 20 array cases | 0.0011 seconds |
| Old object construction from the same loaded snapshot | 25.839 seconds |
| Array-path traced allocation peak | 5.18 MB |
| Object-path traced allocation peak | 26.71 MB |
| Base numeric arrays | 0.369 MB |
| Bulk value queries | 1 |

Features and targets matched within `1e-14`. The old database-backed baseline
was blocked by its validation gate; the tool measured its object construction
against the loaded snapshot instead. These figures isolate construction costs,
not a complete old-versus-new database comparison. The tool never updates
validation receipts or fits a model.

A second read-only check used `price_delta_zscore_20` inputs and 20-session
per-ticker return z-score targets. It selected 898 tickers and loaded 20 cases
in 1.168 seconds without allocation tracing, using two bulk value queries
and 0.986 MB of base arrays.
