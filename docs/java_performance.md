# Stage 6 performance study

Stage 6 is complete as a bounded offline study. It does not port Python code or
claim semantic equivalence for the Java prototypes. The runnable harness is
`tools/stage6_benchmark.py`, the implementation is in
`src/stockguidance/performance/stage6.py`, and the fixed non-secret fixture
specification is `benchmarks/fixtures/stage6.json`.

## Reproduction

Run from `C:\Projects\StockGuidance` with the configured PyCharm interpreter:

```powershell
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $pythonExe tools\stage6_benchmark.py --fixture benchmarks\fixtures\stage6.json `
  --output output\stage6-performance.json
```

The fixture identity for this run is
`8b4171bfc4d81c74d68a94a839aacccfc31003b1e37df8763adbbd0e2566e361`. It uses
seed `20261016`, sizes of 16/48/120 exchange-like sessions and 8/24/64
tickers, and three warm repeats. The harness validates response decoding, raw
and derived reconciliation, array shapes and finiteness, inference outputs,
portfolio event accounting, and independent cash conservation. It performs no
provider calls, SQL calls, schema setup, writes to market data, or production
database operations.

Cold is the first timed call in the process after fixture construction. Warm is
the median of three repeated calls in that process. Throughput units are rows
for JSON, session/ticker checks for reconciliation, array values for tensor
construction, samples for inference, and decision sessions for portfolio
processing. Memory is peak Python allocation from `tracemalloc`; optional
resident deltas are sampled with `psutil`, so these are not a substitute for a
full process RSS profile.

## Results

The configured run used Python 3.14.8, NumPy 2.5.4, Torch 2.14.1+cu132, one
Torch CPU thread, Windows 11, and an AMD64 Family 26 Model 68 processor on a
24-CPU host. Times are seconds in `cold / warm` order; warm throughput is shown
in the final column.

| Operation | Small (16 × 8) | Medium (48 × 24) | Large (120 × 64) | Large warm units/s |
|---|---:|---:|---:|---:|
| JSON decode and SHA-256 | 0.0196 / 0.0335 | 0.2882 / 0.2544 | 1.7972 / 1.7697 | 4,340 rows/s |
| Raw/derived decode, compute, compare | 0.0474 / 0.0737 | 0.5672 / 0.5922 | 4.0857 / 3.9397 | 1,917 checks/s |
| Tensor construction and validation | 0.0011 / 0.0003 | 0.0009 / 0.0006 | 0.0025 / 0.0009 | 16.75M values/s |
| Single-sample inference | 0.0076 / 0.0040 | 0.0053 / 0.0106 | 0.0288 / 0.0094 | 12,580 samples/s |
| Batched inference candidate | 0.0007 / 0.0005 | 0.0013 / 0.0008 | 0.0007 / 0.0019 | 62,135 samples/s |
| Portfolio event processing | 0.0718 / 0.1041 | 0.3492 / 0.3406 | 0.8662 / 0.8665 | 136 decisions/s |

Large-case peak tracked Python allocations were approximately 11.3 MB for
raw/derived reconciliation, 168 KB for JSON decoding, 131 KB for tensor
construction, 6 KB for single-sample inference, 66 KB for the batched
candidate, and 70 KB for portfolio event processing. The fixture carries
833,160 bytes of decoded response payloads at the large size.

SQL/query count and SQL transfer were not measured: no disposable SQL service
was available, and the run deliberately made zero database connections. The
existing Python discovery paths already use bounded inventory and one bounded
legacy session query; that behavior needs a disposable database plan before it
can be compared with any Java reader.

## Findings

Ranked by the large warm run:

1. `raw_derived_reconciliation` is the primary CPU and allocation hotspot. It
   repeatedly decodes typed rows, recomputes the legacy Decimal-based derived
   formulas, sorts the legacy top-100 set, and compares every column. The
   measured path is `mysql_market_read.py`, `legacy_derived.py`, and
   `validation/derived.py`; numerical and revision semantics make an unsafe
   floating-point rewrite inappropriate without parity fixtures.
2. `json_decode_and_hash` is the second hotspot. The current manifest path
   parses each response with `json.loads`, validates every bar, and hashes the
   canonical response bytes. Streaming or bulk parsing may help, but source
   content identity and typed validation must remain identical.
3. `portfolio_event_processing` grows with the typed Decimal ledger and
   repeated allocation/position scans in planning, fills, exits, and marks. The
   study uses a fixed top-eight recommendation list while still carrying the
   full ticker quote frame; larger recommendation breadth must be measured
   separately before changing the portfolio contract.
4. Single-sample inference is measurable but small in this workload. A batched
   Torch candidate is roughly 5 times faster than the large warm single-sample
   path, but batching future decisions would violate chronological simulation
   semantics. It is not adopted as a production change.
5. Tensor construction is not a measured bottleneck at these sizes. The
   current NumPy validation and Torch conversion are retained.

The smallest evaluated Python improvement was the batched inference candidate;
it remains an experiment because the simulation must preserve one decision
cutoff at a time. Existing bounded/batched database reads could not be
evaluated without SQL storage, and no production schema, index, or data-path
change was justified by this offline run. A future read-only reconciliation
profile should first test bounded streaming row batches and reuse of already
decoded session maps before considering a port.

## Java correctness review

The Java sources under `StockGuidance/java/src/stockguidance` are not a valid
performance baseline:

- `DatabaseRowsToTensors` buffers the full result in nested Java collections,
  uses database date/ticker IDs as the working chronology, calls `getDouble`
  without checking `ResultSet.wasNull`, and has identical flattened-array
  branches for `return2d`.
- `JsonUtils` and `SingleRun` use regular expressions for JSON, so escaped
  strings, nulls, nested structures, exponent numbers, and malformed input do
  not have the typed contract required by the current Python path.
- `DbInfo` has a minimal regex/key-value decoder, while `DbConnect` searches
  parent directories and opens a mutable MySQL connection without the current
  explicit-path, read-only transaction, allowlisted-identifier, and
  credential-safe error contract.
- `StockRecommenderFromSavedScores` emits `index + 1` instead of the supplied
  ordered ticker IDs. `SimulationEngine` also uses legacy date IDs and CSV
  output rather than the current source, revision, receipt, availability,
  calendar, and portfolio-event contracts.
- No Java build, pinned dependency set, real JSON parser, interchange contract,
  parity fixture set, or read-only transaction test is present. Therefore this
  study intentionally records no Java timings and does not recommend a Java
  port yet.

If a later review establishes a real bottleneck, the candidate order remains
streaming read-only reconciliation, bulk imports, then frozen-score simulation.
Python keeps Torch fitting, feature contracts, provider/calendar policy, and
the semantic reference until a future stage proves parity and a measured
end-to-end benefit including process and transport costs.
