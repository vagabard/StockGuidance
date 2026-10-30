# Configured rolling feature channels

Stage 5 adds the versioned `FeatureSpecV2` contract while preserving the v1
`FeatureSpec` and its `previous-close-v1` / `entry-open-v1` meanings.

```python
from stockguidance.features.specification import FeatureSpecV2, PriceDeltaZScore, PriceZScore, VolumeZScore

features = FeatureSpecV2(
    ticker_ids=(17, 3),
    lookback_sessions=10,
    channels=(PriceZScore(20), PriceDeltaZScore(20), "raw_volume", VolumeZScore(60)),
)
```

The saved tensor shape is `(time, ticker, channel)`, flattened only by the
existing model adapter in C order. The declared ticker tuple is stable and
ordered; database IDs are never tensor offsets or descriptors. A price
z-score channel with lookback `L` and window `W` requires `L + W - 1` prior
closes. A price-delta z-score requires one additional predecessor close. The
same `L + W - 1` dependency span is used for a volume z-score materialization.

Stored and prepared channels are read through the injected
`FeatureChannelRepository`. Each bounded read includes its dependency
sessions, scored feature sessions, cutoff, value status, readiness evidence,
and revision. Missing, unverified, late, pending, stale, invalid, and
zero-variance values remain explicit issues; they are never converted to zero.
`build_sample` and `build_training_dataset` accept the same repository so
training and inference use the same layout and availability decisions.

The production adapter is read-only:

```python
from stockguidance.database.repositories.features import MySQLFeatureChannelRepository
channel_repository = MySQLFeatureChannelRepository(source, connect, table_names)
```

The adapter checks the selected table registry, simplified schema, pending
operation guard, completion metadata, series identity, and bounded ticker/date
scope. It never creates tables or prepares values while reading. Volume
z-scores must be prepared explicitly with the Stage 3 operation API, for
example:

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step plan
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step select --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step calculate --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step apply --operation-id <operation-id>
.venv\Scripts\python.exe -m stockguidance.cli.prepare_database --config examples\stage3\volume-zscore-preparation.json --step validate --operation-id <operation-id>
```

These commands are bounded and rerunnable; review the rendered SQL before any
manual database-interface execution. Price and price-delta materializations
reuse the existing rolling population z-score calculation and metadata.
