# Close-to-close training and inference artifacts

Stage 6 provides a typed training API and standalone inference artifacts. Inputs
are Stage 5's exact historical closing prices through the previous session,
optionally supplemented by the distinct [Stage 10 opening feature](entry_open.md);
targets are raw `close(T+1) / close(T) - 1`. All old open-to-close datasets,
NPZ caches, model manifests and checkpoints require rebuilding. No old file is
deleted or relabeled. Recommendation, cash-rest planning and chronological
simulation use the same package workflows. See [frontend integration](frontends.md)
for current commands and completed cleanup status. Each signal mode requires its
own trained artifact; opening receipts must satisfy each original decision cutoff.

## Environment and commands

Use PyCharm's configured Python executable and its package manager. The optional
`training` dependency supplies Torch; an ordinary development installation is
`& '<configured-python>' -m pip install -e '.[dev,training]'`. Install the Torch
build appropriate to the machine. CPU is used for deterministic acceptance
fixtures; production device selection remains `auto`, `cpu` or `cuda`.

Run from the checkout without refreshing the editable installation:

```powershell
& '<configured-python>' -m stockguidance.cli.train_model --config training-job.json --artifact output/model.sgmodel
& '<configured-python>' -m stockguidance.cli.train_model --config training-job.json --artifact output/model.sgmodel --checkpoint output/training.pt
& '<configured-python>' -m stockguidance.cli.train_model --config resumed-job.json --artifact output/resumed.sgmodel --resume output/training.pt
```

The `train_model` console command is also registered after the next installation.
Exit 0 reports an actual artifact path and content identity, metrics, exclusions
and effective resources as JSON. Exit 2 indicates invalid arguments/job
configuration; exit 1 indicates failed data readiness, fitting, checkpointing or
export. CLI failures use fixed messages without external exception bodies.
The typed API raises the underlying exception for application error handling.

To validate the checked-in fixture without touching a provider or database:

```powershell
& '<configured-python>' -c "from pathlib import Path; from stockguidance.training.jobs import load_job; load_job(Path('examples/stage5/previous-close-training-subset.json'))"
```

## Explicit job configuration

This is a **fixture example**, not a statement about live Polygon history or
publication. Supply real evidenced policies and an existing source-specific
history boundary established by the updater. The command only reads market data;
it does not update data, initialize schemas, persist a guessed history boundary,
send email, simulate trades or discover models in experiment directories.

The parser-valid, non-secret Stage 5 subset configuration is checked in at
[`examples/stage5/previous-close-training-subset.json`](../examples/stage5/previous-close-training-subset.json).
Its `start_date` and `end_date` select entry sessions only; the lookback sessions
and next-session close needed for each label are separate dependencies. The
`fixture` source and `fixture-data` paths are placeholders for disposable
acceptance storage, not live provider access.

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
  "model": {
    "architecture": "mlp",
    "hidden_widths": [64, 64, 64],
    "activation": "leaky_relu",
    "layer_types": ["linear"],
    "bias": "all",
    "layer_normalization": true,
    "final_activation": "none",
    "implementation_version": "stockguidance-models-v1"
  },
  "training": {
    "epochs": 3,
    "batch_size": 2,
    "learning_rate": 0.001,
    "learning_rate_gamma": 1.0,
    "optimizer": "adamw",
    "device": "cpu",
    "precision": "fp32",
    "seed": 17,
    "shuffle": false,
    "num_workers": 0,
    "pin_memory": false
  },
  "start_date": "2024-07-03",
  "end_date": "2024-07-08",
  "training_cutoff": "2024-07-09T19:00:00+00:00",
  "availability_policy": "strict",
  "data_snapshot": null,
  "publication_delay_minutes": 15,
  "publication_evidence": "Fixture-only policy; replace with actual evidence",
  "decision_minutes_before_close": 30,
  "evaluation": {
    "start_date": "2024-07-09",
    "end_date": "2024-07-11",
    "label_cutoff": "2024-07-11T21:00:00+00:00"
  }
}
```

`interval` is the explicit daily identity `1d`. Dates are exchange sessions;
calendar closures inside the range do not create samples. The CLI uses actual
calendar closes, including early closes, minus the supplied decision lead.
Use `evaluation: null` for fitting without a holdout. `model` and `training`
may omit individual fields to use the typed defaults; other job fields are
required. Unknown fields and legacy label/input switches fail validation.
Relative data/database paths resolve from the working directory.

The history store is `<data_root>/history/`, keyed by full source identity.
`VerifiedCloseRepository` requires matching raw data, source manifests and actual
receipt timestamps. A historical download made today is not evidence of earlier
availability. Current evidence cannot reconstruct overwritten revisions. Missing
or late historical observations remain unavailable; the command never invents
receipt times or falls back to legacy caches/derived target columns.

`availability_policy` defaults to `strict`. The explicit opt-in value
`retrospective_daily_previous_close` is for research only and requires an aware
`data_snapshot` timestamp at or after `training_cutoff`. It accepts verified
backfilled daily previous closes and labels when the calendar close plus the
evidenced `publication_delay_minutes` would have preceded each sample cutoff.
The stored `ObservationTiming.available_at` receipt remains unchanged, and the
snapshot, policy and data revision are retained in sample/cache identities and
the exported artifact. This is an availability assumption, not an original
point-in-time replay; corrected/adjusted history and the selected universe may
contain hindsight. The policy rejects `entry_open`, synthetic opening evidence,
future snapshots and immature labels. Strict and retrospective caches/artifacts
cannot be mixed; strict artifacts remain strict when their legacy metadata is
unambiguous. Production recommendation commands reject retrospective artifacts.

## Programmatic API and chronology

Application callers use `stockguidance.datasets.training.build_training_dataset`
and `stockguidance.training.service.train_model`:

```python
result = train_model(
    dataset, configuration, Path("output/model.sgmodel"),
    evaluation=held_out_dataset,
    checkpoint_path=Path("output/training.pt"),
)
loaded = load_artifact(result.artifact)
scores = predict_sample(loaded, inference_sample)
```

Import `Path` from `pathlib`, `load_artifact` from
`stockguidance.artifacts.loading`, and `predict_sample` from
`stockguidance.inference.prediction`. `dataset` and `held_out_dataset` are Stage 5
`TrainingDataset` objects, and `configuration` is a `TrainingConfig` from
`stockguidance.training.configuration`. `HistoryTrainingRequest` and
`train_from_history` in `stockguidance.workflows.train_model` assemble those
same datasets from explicit decisions, source history, publication, calendar and
an injected read-only close repository. Neither API constructs CLI arguments or
returns a mutable training directory.

Only complete, mature samples enter the dense training tensors. Excluded entry
dates and feature/target/price states remain in the result. A sample for a T-1
entry is excluded before T's exit close/publication even if future prices exist
in storage. Training and inference share exactly the same stateless window
transforms and ticker order. No fitted preprocessing or target normalization
is introduced.

Evaluation is optional and strictly chronological: its entire requested period
follows the entire requested training period, all its decisions follow the
fitting cutoff, and it uses identical source/feature/target/calendar semantics.
Evaluation outcomes are read at a separate explicit later cutoff and never
enter fitting or inference metadata. Training metrics are always `in_sample`;
separate evaluation is `chronological_holdout`. Reusing a holdout for choosing
hyperparameters does not establish an unbiased final test estimate.

`run_experiment(dataset, members, evaluation=...)` in
`stockguidance.training.experiments` executes explicit immutable
`ExperimentMember(configuration, artifact_path)` tuples and stops on failure.
It does not discover members or reconstruct a family controller.

## Models and execution

The shared package preserves the existing configurable MLP, structured sparse
connections, fixed random masks (0.4 for `random-percent`, 0.1 for
`sparse-random`), transformer layers, and convolution/transformer architecture.
Convolution uses three 16-channel kernel-15 stages, two 4-head transformers
(feedforward width 64, GELU, norm-first), and an unrestricted projection with
global mean pooling. Architecture constants are tied to implementation version
`stockguidance-models-v1`. The MLP uses its explicit width tuple, repeated last
layer type, bias policy, and original activation-dependent initialization;
LayerNorm still applies only when input width exceeds 5000. Empty hidden widths
correctly build one direct linear mapping; the old implementation accidentally
appended another hidden/output path after that mapping.

Raw return regression requires unrestricted output: bounded activations and
softmax/logsoftmax are rejected by `ModelSpec`. Hidden activation choices remain
available. Default fitting retains L1/MAE loss, AdamW, learning rate `1e-7`, batch
size 256, fp32, no shuffling and automatic device selection. Adam/SGD,
exponential learning-rate decay, CUDA fp16/bf16 autocast, worker count and
pin-memory settings are explicit options. CPU requires fp32. No distributed
trainer or automatic production sweep is introduced. The seed is recorded.
The existing expandable-segments allocator default and CUDA device policy
excluding GPU names containing `4070` are retained. Explicit allocator overrides
remain intact; CPU fixture execution requires no CUDA device.
NaN/infinity in batches, outputs, losses, gradients, weights or metrics fails
the run; values are never sanitized into zero.

## Inference schema 1

A `.sgmodel` is a single ZIP containing exactly `manifest.json` and `weights.pt`.
Manifest schema is `stockguidance-inference-1`. Its SHA256 content identity
covers all metadata and the weight digest. It records:

- Exact non-secret source/request scope, adjustment and observation interval.
- Ordered ticker IDs/features, lookback, missing-history policy, feature version,
  layout and stateless transformations.
- Raw close-to-next-session-close target formula, close execution, and explicit
  `pre_close_decision_same_close_fill` simulation assumption.
- Calendar identity/version, model specification/implementation version,
  framework version and float32 inference weight dtype.
- Fitting and information cutoffs, fitted entry/exit bounds and last exit close
  timestamp, training dataset revision, eligible/excluded sample identities,
  seed and completed epochs.

The information cutoff conservatively records the entire allowed fitting
cutoff. Prediction rejects any decision earlier than that cutoff, and checks
all input/source/target/calendar semantics and finite numerical layouts.
Inference scores follow the exact artifact ticker order. Allocation policy,
eligibility/ranking, cash state, database access and email are outside this API.

Loading uses `torch.load(..., weights_only=True)` on the embedded tensor state,
with exact key/shape/dtype/finite checks. There is no optimizer, scaler,
`SingleRun`, `FamilyRun`, credential, database configuration, experiment path or
directory search in the inference contract. Explicit loading works after moving
the single file into an empty directory with only the installed package/Torch.
Imports alone do not load Torch or touch application files/network.

Exports rebuild and validate the model and compare predictions before atomic
replacement. Interrupted export preserves the existing destination. A required
checkpoint/export failure propagates; no successful result is returned.

## Checkpoints and ensembles

Resumable `.pt` checkpoints use separate schema `stockguidance-checkpoint-1`.
They contain model, optimizer, scheduler, AMP scaler, completed epoch, CPU/CUDA
random state and loader generator state. Their compatibility digest binds the
full training configuration (except total epoch count), resolved device/precision,
framework version and exact dataset revision. Resume requires a larger total
epoch count. A correction, changed
sample/cutoff/specification or optimizer configuration rejects resume. Training
and evaluation use separate loader generators, so evaluation does not change
the resumed shuffle sequence. Dense CPU fixtures prove uninterrupted and resumed
training produce identical weights and artifact identity. Each checkpoint is
atomic and separate from the artifact; an earlier completed checkpoint may
remain usable if a later export fails.

`export_ensemble(path, references, weights=None)` and `load_ensemble(reference)`
in `stockguidance.artifacts.ensembles` use `stockguidance-ensemble-1`. The single
archive embeds each explicitly identified model artifact plus its weight.
Weights must be positive and sum to one (default equal weights); duplicate
members fail. Full source/feature/target/ticker/calendar semantics must match,
even when array dimensions match. Architecture and fitting cutoffs may differ;
prediction enforces every member's information cutoff. Original member paths
are unnecessary after export. Nested ensembles and legacy artifact inference
are unsupported.

## Migration and checks

Stage 9 removes the root `models/` re-exports, `trainer/` controller internals,
legacy argument/configuration loaders and temporary connection adapters.
Supported training, recommendation, batch and incremental frontends now call
typed packages directly. User model implementations and numerical tests are
retained in the package; old controller artifacts/datasets require rebuilding.
See [the grouped retirement list](stage9_retirement.md) for exact source paths.

The retained hardcoded provider key is `DEFAULT_API_KEY` in `polygon_fetch.py`;
eventual external configuration remains future work. Training does not load it.

Stage 10 acceptance uses the same typed training workflow to fit a small ordered
subset, reads `artifact.path` and the canonical `artifact.artifact_id` from the
training result, and evaluates that artifact only over a later fixed period.
The acceptance fixture is `tests/integration/test_archive_to_simulation.py`; it uses
fake receipts and a disposable close repository, not live provider history.

```powershell
& '<configured-python>' -m pytest tests/training tests/models -q
& '<configured-python>' -m pytest tests -q
& '<configured-python>' -m mypy
```

Fixtures use temporary files and fake source/database adapters, tiny CPU models,
and no live data writes, provider requests or production training. CUDA and
native MySQL deployment require separate operational verification. Torch may
emit its original transformer nested-tensor optimization warnings; these do not
change the architecture or indicate invalid predictions.
