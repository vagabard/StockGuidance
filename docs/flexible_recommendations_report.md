# Flexible recommendations: implementation and verification

Implemented November 25, 2026. Usage and configuration are described in
[recommendations.md](recommendations.md).

## Implemented behavior

- Optional configuration; date flags accept ISO dates and `-Ny` / `-Nd` starts
  relative to the requested end. Both negative-offset argument forms work.
- Omitted end dates use today in New York, completed published data, and the
  next available entry. Explicit historical ends use a separate replay clock.
- Dynamic membership intersects good positive-close populations at the two
  endpoints, followed by a shared 100% full-period usable-close coverage filter.
  This filter also applies to explicit lists and always checks every requested
  exchange session, even when volume ranking is sampled or disabled. Exclusions
  include symbols and missing-session counts. Surviving explicit symbols retain
  their order and reject percentile trimming.
- Dollar-volume trimming and the fixed transaction minimum default to zero.
  Trimming uses mean shares times close, floor rounding, and stable ID ties.
- Exact SQL aggregation is the default. Explicit deterministic sampling and
  data/design-sensitive aggregate caching are available. Timings go to stderr
  and saved reports; disabled filtering does not fetch volume history.
- A fresh matching model is the default. Models freeze stable IDs and labels.
  Explicit compatible artifacts can be reused; incompatible universes fail.
- Chronological simulations use the shared selector at each retraining boundary.
  Old positions/benchmarks retain price coverage across universe changes.
  Saved forecasts and execution quotes support offline investment replay.
- Current-history session chronology and the versioned raw-data validation gate
  remain active. No import-time eligibility or detailed provenance was added.

## Stored-data benchmark

After adding the full-period coverage filter, the same stored range produced
10,327 endpoint candidates, 1,013 coverage exclusions, and 9,314 fully covered
tickers. The subsequent 5% dollar-volume trim removed 465, leaving 8,849.
Coverage counting took 4.334 seconds; dollar-volume aggregation took 3.552
seconds; total selection took 8.967 seconds. The complete selection report is
saved locally as `output/one-year-full-coverage-selection.json`.

These measurements predate the full-period coverage filter added later that day.
Range: November 25, 2025 through November 24, 2026; 251 completed trading
sessions; 10,327 endpoint candidates; 5% trimming leaves 9,811 tickers.

| Case | Filter time | Total selection time | Process peak working set |
|---|---:|---:|---:|
| Exact, all 251 sessions | 5.241 s | 6.436 s | 120.0 MB |
| Explicit 40-session sample | 1.370 s | 2.018 s | 124.6 MB |
| Cached exact aggregate | 0.019 s | 0.664 s | 128.1 MB |

The process peak includes the interpreter and calendar/data libraries and is
cumulative across cases. Identical retained counts do not imply identical
memberships between exact and sampled rankings. This benchmark excludes
training, downloads, and inference. Repeat with:

```powershell
& .\.venv\Scripts\python.exe tools/benchmark_endpoint_universe.py --start-date 2025-11-25 --end-date 2026-11-24
```

Results: `output/dynamic-universe-benchmark.json`.

## Functional acceptance

- AAPL/MSFT: the new flag-based path trained two epochs on 59 samples and
  generated live recommendations. Reusing the exported artifact succeeded
  without retraining and preserved its identity.
- Dynamic universe: endpoint selection and 99.9% dollar-volume trimming produced
  a bounded 12-stock universe. A small model trained two epochs on 59 samples,
  exported with verified IDs/labels, and generated live recommendations.
- Historical AAPL/MSFT: an explicit November 13 endpoint generated a November 16
  replay with current revised history clearly identified.
- Historical investing: November 16–24, $1,000 initial simulated capital, four
  chronological retraining decisions and six realized simulated trades. The
  run completed without missing data/fills and ended at $1,011.62 marked equity,
  including positions still open at the evaluation boundary.
- Replaying that saved simulation with no model/database/provider calls produced
  exactly the same final portfolio. Synthetic tests also verify changing
  memberships, settlement of retired members, and retraining cadence.

The 99.9% setting bounded the acceptance test and is not a recommended liquidity
policy. These two-epoch runs verify execution, not predictive quality or
out-of-sample investment performance. No market orders were submitted.

Reports and models are under `output/flexible-recommendation-acceptance`.

## Checks

256 distinct focused tests passed across recommendation, simulation, training,
endpoint-repository, and readiness checks. The main combined run passed 252;
four subsequently added edge/correctness tests passed in focused runs.
Strict mypy checks passed for the 16 new/changed frontends and runtime modules
checked together, with imported-module errors suppressed. Diff checks passed.
Existing pandas timezone deprecation warnings remain.

No schema migration is required by this feature. Existing explicit JSON jobs
and legacy portfolio execution behavior remain supported.
