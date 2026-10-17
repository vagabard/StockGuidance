# Stage 9 source retirement and frontend migration

Status: complete (2026-10-17). Stage 9 deleted 85 obsolete implementations;
the subsequent source cleanup retired its 21 thin frontends and the remaining
root-level adapters. The credential fallback module remains. Cleanup changes
source/tests only, not user data.

## Recovery and exclusions

All 107 original files have a byte-for-byte, SHA256-verified local recovery copy in `cache/stage9-source-before-retirement-20261015.zip` (Git-ignored).
Archive SHA256: `4CFEABD1485B96F7BCF7E1CB0EF057C7A39F2E31047EDAD53A0D0EA9021908A5`.
The originals are also retained in Git history. Recover archive entries into a new, empty directory for inspection before restoring source. Keep the archive local: it contains original source and may contain private configuration literals.

Java, source datasets, models, NPZ/CSV files, databases, caches, the Git index and user configuration are not cleanup targets. Only the Python source/test paths listed below are deleted or rewritten. Model implementations in `src/stockguidance/models` and their numerical tests are retained.

## Files deleted, grouped by reason

### Refactored into typed training, model and artifact packages (43)

Implementations now live in `src/stockguidance/training`, `models`, `artifacts`, `config` and `reporting`. Mutable controllers, directory-discovery and temporary re-export adapters are no longer supported.

- `models/__init__.py`
- `models/convolution_transformer.py`
- `models/multi_layer_mlp.py`
- `models/sparse_linear.py`
- `models/utils.py`
- `trainer/__init__.py`
- `trainer/ai_type.py`
- `trainer/args_train.py`
- `trainer/dataloaders.py`
- `trainer/dataset/ticker_info_for_dates.py`
- `trainer/dataset/time_training_instance.py`
- `trainer/dataset_classification.py`
- `trainer/datasets.py`
- `trainer/factory.py`
- `trainer/file_structure_family_run_handler.py`
- `trainer/file_structure_single_run_handler.py`
- `trainer/io_run.py`
- `trainer/label/transform/__init__.py`
- `trainer/label/transform/transformers.py`
- `trainer/legacy_arguments.py`
- `trainer/legacy_training.py`
- `trainer/loss_functions/__init__.py`
- `trainer/loss_functions/losses.py`
- `trainer/loss_functions/ranking.py`
- `trainer/loss_functions/ranking_error.py`
- `trainer/metric_file_handler.py`
- `trainer/metrics.py`
- `trainer/model_loader.py`
- `trainer/rep_handler.py`
- `trainer/splits.py`
- `trainer/state/__init__.py`
- `trainer/state/family_run.py`
- `trainer/state/single_run.py`
- `trainer/torch_interface/__init__.py`
- `trainer/torch_interface/compat.py`
- `trainer/torch_interface/device.py`
- `trainer/torch_interface/memory.py`
- `trainer/train_single_run.py`
- `trainer/training_bundle.py`
- `trainer/util/__init__.py`
- `trainer/util/argparse_utils.py`
- `trainer/util/date_bounds.py`
- `trainer/util/utils.py`

### Refactored into shared close-based features, calendar and data repositories (15)

Replaced by `src/stockguidance/features`, `targets`, `calendar`, `database/repositories`, `market_data` and `validation`. These remove intraday inputs, guessed window sizes, global-latest dates and duplicate database configuration.

- `data_loading_shared.py`
- `database/create_dbinfo.py`
- `database/database_date_id_queries.py`
- `database/db_connect.py`
- `database/db_info.py`
- `database/identify_tickers_with_missing_dates.py`
- `database/query_tickers_by_date.py`
- `feature_vector_builder.py`
- `feature_windowing.py`
- `get_ticker_position_on_date.py`
- `load_database_rows_into_tensors.py`
- `load_latest_raw_stock_data_date.py`
- `load_raw_stock_data_rows.py`
- `populate_db_ticker_info.py`
- `training_instance_provider.py`

### Obsolete controller, intraday and guessed-window tests (17)

Their removed APIs and old target/layout assumptions are replaced by behavioral tests in `tests/stage5` through `tests/stage9`. No tests are hidden by collection exclusions. Numerical sparse/random/activation/initialization tests are retained and import the package models.

- `test_reuse_tensors.py`
- `test_training_instance_provider.py`
- `tests/test_family_controller.py`
- `tests/test_feature_vector_builder.py`
- `tests/test_feature_vector_builder_from_db.py`
- `tests/test_metrics_eval_epoch.py`
- `tests/test_mse_loss_behavior.py`
- `tests/test_mse_restricted.py`
- `tests/test_ranking_metrics.py`
- `tests/test_simulate_trading_utils.py`
- `tests/test_split_datasets_floating_head_tail_provider.py`
- `tests/test_splits_head_tail.py`
- `tests/test_stock_recommender_from_existing_model_uniform_range.py`
- `tests/test_stock_recommender_from_model_random_argmax.py`
- `tests/test_ticker_id_persistence.py`
- `tests/test_train_pytorch_dry_run_head_tail.py`
- `tests/test_training_instance_provider_unit.py`

### Obsolete CSV, directory-discovery and intraday benchmark utilities (10)

Old run folders, saved scores, binary correlations and intraday performance assumptions have no supported conversion. Current report/artifact readers use explicit schema-identified inputs; close-based feature inspection is not misrepresented as a performance benchmark.

- `benchmark_index_dataset.py`
- `benchmark_score_reading.py`
- `benchmark_score_reading_threaded.py`
- `convert_correlations_to_binary.py`
- `generate_rep_averages_to_compare_single_runs.py`
- `organize_rep_runs.py`
- `viewers/compare_runs_shared.py`
- `viewers/correlation_matrix_viewer.py`
- `viewers/populate_runs_viewer.py`
- `viewers/populate_statistics_viewer.py`

## Subsequent frontend retirement

The thin frontends above and the remaining root-level adapters are now deleted.
Use `python -m stockguidance.cli.<command>` from a checkout, or the console
commands declared in `pyproject.toml` after installation. Streamlit views run
from `src/stockguidance/viewers/reports.py` and
`src/stockguidance/viewers/artifacts.py`. The deleted paths remain recoverable
from Git history; no redirect or compatibility parser remains.

## Credential module retained (not a deletion)

- `polygon_fetch.py`: keep the existing `DEFAULT_API_KEY` constant at its original location and value. Package the module so lazy credential lookup also works outside the checkout. It is a credential fallback, not an executable updater frontend. Never copy the credential into documentation, configuration, reports or artifacts.

The three obsolete benchmark files are retired rather than redirected to feature inspection: inspection does not benchmark their former CSV/intraday workloads. Stage 9 deferred `entry_open`; the subsequently requested enhancement is documented in [entry_open.md](entry_open.md).
