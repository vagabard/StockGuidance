# Close-based samples

Stage 5 exposes one numerical construction path for training, inference, and
future simulation: `stockguidance.datasets.building.build_sample`. It has no
model, device, portfolio, CLI, or mutable training-controller dependency.

## Specifications and layout

`FeatureSpec` records an ordered tuple of positive ticker IDs, an exact number
of prior exchange sessions, signal mode, interval, missing-history policy,
ordered transformations, and price adjustment. Its numerical version is
`previous-close-v1`. The default transformation is
`close(s) / close(first session in this window) - 1`, independently per ticker.
It uses all and only the specified historical closes. The first value is zero
because two observed prices are equal, not because missing history was filled.
A one-session window therefore contains zero relative features; choose a longer
window for price movement information. Explicit `CloseFeature.LOG_CLOSE` is
also supported, alone or in an explicitly ordered feature tuple.

For v1 jobs both transformations are stateless. There is no fitted scaler,
cross-sectional normalization, ranking, full-history extrema, or intraday
input in the v1 contract. Stage 6 adds explicit v2 targets and fitted
preprocessing in [target_preprocessing.md](target_preprocessing.md); it does
not change v1 job semantics. The numerical
layout is `(lookback_sessions, ticker_count, feature_count)`, flattened in C order:
oldest session first, then the declared ticker order, then the declared feature
order. Sparse ticker IDs are identities, not array offsets. Missing prices
invalidate the whole feature vector; no ticker is dropped or reordered.

`TargetSpec` supports only raw `close(T+1) / close(T) - 1`. Raw and transformed
target tuples are identical and follow the feature specification's ticker order.
There is no implicit target transformation. Legacy ranked, shifted, and
open-to-close target specifications are incompatible. `TargetSpecV2` adds
explicit raw, per-ticker return z-score, and same-day cross-sectional return
z-score targets while retaining raw labels. Stage 10 adds an explicit
[entry-opening feature channel](entry_open.md) without changing these targets.
Price adjustment and daily interval must match the
non-secret source/request identity before storage is accessed.

## Timing and sample construction

Construct `DecisionTiming` with authoritative previous, entry, and next exchange
sessions and an aware pre-close decision cutoff. Supply an evidenced
`HistoryBoundary`, a calendar, an explicit `DailyPublicationPolicy`, and a
`CloseRepository`. Sessions come from the calendar rather than database IDs.

```python
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import FeatureSpec
from stockguidance.targets.specification import TargetSpec

specification = FeatureSpec((17, 3), lookback_sessions=3)
inference = build_sample(
    specification, TargetSpec(), timing, boundary, calendar, publication, repository,
)
historical = build_sample(
    specification, TargetSpec(), timing, boundary, calendar, publication, repository,
    label_cutoff=training_cutoff,
)
```

`timing`, `boundary`, `calendar`, `publication`, `repository`, and `training_cutoff`
in this example are explicit application dependencies, not global defaults.
Omitting `label_cutoff` requests inference features only and authorizes no outcome
reads. Supplying it requests training/evaluation labels while preserving the
sample's original feature-decision cutoff. Entry and exit prices are queried
only when the exit has closed and both prices are expected published by that
cutoff. Actual source/import receipt times must also prove maturity. An entry
at T-1 is excluded from training before T closes/publishes, even if storage
contains later history. The entry close is never a pre-close signal.

`Sample.features.values` is an immutable flat tuple or `None`.
`feature_state` describes calendar/publication readiness; actual storage failures
are separate typed `features.issues`. Insufficient history has an empty window,
not a shorter or zero-filled window. `targets.raw` retains `None` for each
unavailable outcome. `target_state` distinguishes ready, not requested,
not yet available, unverified, and unavailable. Per-price issues distinguish
missing, invalid, unverified, not-yet-available, and verified provider absence.
`training_eligible` requires a complete feature vector and all mature targets.

`build_training_dataset` takes an explicit chronological tuple of decisions and
one training cutoff. Its `eligible` and `excluded` samples retain their original
dates and reasons; missing samples do not shift the selected training period.
`datasets.numpy_arrays.training_arrays` converts eligible samples to contiguous
float64 `(samples, input_size)` and `(samples, ticker_count)` matrices. Its
`validate_arrays` rejects wrong dimensions/dtype, noncontiguous layouts, and
NaN/infinity. NumPy is imported only on adapter invocation; no device transfer
or label sanitization occurs. Torch integration belongs to Stage 6.

## Storage and revision identities

`VerifiedCloseRepository(source, MySQLValidationRepository(source, connection_factory))`
is the read-only production adapter. It uses the configured database path,
diagnostic raw snapshots and ticker mappings, validated source manifests, exact
raw content identities, and actual receipt times. A valid count or an internal
receipt without matching provider evidence is insufficient. It accepts only
bounded `CloseQuery` objects, accounts explicitly for every session/ticker key,
and returns no future observation. It uses the existing compatible updater
schema without creating or migrating it. Legacy derived values are never model
features or close-to-close labels.

`feature_cache_key` includes source/request/interval/adjustment, full feature
specification and version, exact layout, ticker order, decision/entry/exit,
calendar identity/version, and the historical raw content/availability revision.
`cache_key` additionally includes the target specification, label cutoff, and
outcome dependencies. Corrections and backfills invalidate affected identities.
Label-only corrections leave the feature identity unchanged. Row ordering and
database date IDs do not affect identities. These are identities for new caches;
Stage 5 does not introduce a persistent cache or reuse legacy NPZ files.
Model identities must be added by later prediction-cache consumers.

Current manifests/receipts cannot reconstruct an earlier revision after a
correction or recheck replaces its evidence. A historical decision whose required
data was received later remains unavailable. No old availability or price is
invented. Live supported history and publication entitlement remain unknown
without operator evidence, as documented in the updater/validation guides.

## Configured rolling feature inputs

`FeatureSpecV2` now connects the shared sample builder to prepared price,
price-delta, raw-volume, and volume z-score channels. See
[`feature_channels.md`](feature_channels.md) for the typed repository boundary,
readiness rules, dependency arithmetic, and standalone volume-preparation
commands. The z-score family, window, calculation version, precision,
availability policy, revision, and explicit `(time, ticker, feature)` layout
are part of the sample identity. Its warm-up scope extends by the rolling
window in exchange sessions in addition to the model's own number of timesteps;
those are different concepts and must not be conflated. Training and inference
use the same configured adapter and availability decisions.

A later artifact-owned encoding adapter may map stable database ticker IDs to a
stock vocabulary. The vocabulary and the unknown-stock policy are intentionally
not chosen in this release. Database IDs remain storage identities and never
become vector offsets. No embedding class, encoding enum or network integration
is added until that feature design is versioned and shared by all consumers.

A future versioned feature adapter may add other prepared families only with an
explicit calculation version, normalization window, availability policy, and
input layout. Those identities must remain bound through the sample, cache and
artifact identity; stable database ticker IDs remain identities and never
become vector offsets.

## Validation and migration

The standalone read-only validator computes numerical close targets from
verified raw prices for its declared coverage. Add `--ticker-ids '17,3'
--lookback-sessions 3` to assess the exact default model feature specification.
The typed `ValidationSettings.features` accepts any supported explicit feature
order. Reports include sample dates/cutoffs, tickers, exact windows, numerical
values or unavailable states, dependencies, and revision identities. Historical
feature decisions use one microsecond before each entry close. Their label
assessment cutoff is the requested validation cutoff. Expected historical
availability is never substituted for actual receipts. Feature context extends
to the specified lookback, bounded by supported history and publication.

New targets are computed on demand; no new target column is persisted. Existing
derived columns are still validated separately as `legacy-open-to-close-v1`.
Mature in-scope missing dependencies block validation; unavailable final labels,
verified provider ticker absence, and insufficient boundary history stay
explicit. Beyond-scope unverified dependencies are reported as not assessed.
Numerical repair findings identify their dependency date and defer to an existing
source/import repair for that same cause. They never request rebuilding a legacy
column as though it stored a new target.

Stage 9 retires the root feature builder, guessed-window helpers, tensor loader,
training provider and `trainer/label/transform`. Supported training,
recommendation, simulation and diagnostic callers use these shared samples and
independent artifacts. Old datasets, caches and models must be rebuilt; matching
dimensions do not prove compatible semantics. The exact source-only deletions
are [grouped by reason](stage9_retirement.md). No existing user data/model
directory is deleted or rewritten.
