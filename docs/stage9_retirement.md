# Stage 9 source retirement review

Status: awaiting explicit approval. Automatic approval review rejected the bulk
retirement because it affects uncommitted source/test files, even after recovery
verification. No item in this manifest has been retired or replaced by that
blocked patch. This is the exact reviewable cleanup scope, not a data purge.

All 107 current files have a byte-for-byte, SHA256-verified local recovery copy
in `cache/stage9-source-before-retirement-20261015.zip` (Git-ignored).
Archive SHA256: `4CFEABD1485B96F7BCF7E1CB0EF057C7A39F2E31047EDAD53A0D0EA9021908A5`.
Archive entries retain workspace-relative paths. Recover into a new, empty
directory for inspection before restoring any source. The archive is local and
must not be published; it preserves existing source contents, including any
pre-existing private configuration literals.

Only Python source/tests are listed. Java, source datasets, models, NPZ files,
CSV files, databases, caches, the Git index and user configuration are not
cleanup targets. The user's model implementations already reside in
`src/stockguidance/models`. Existing sparse/random/activation/initialization
checks are migrated and retained. Obsolete controller/dense-window/intraday
checks are superseded by the Stage 5-9 availability, artifact, workflow and
portfolio behavioral suites; they will not be hidden by pytest exclusions.

The retained `polygon_fetch.py` key stays at its current location and value.
Its obsolete downloader/discovery code will be removed, and its command will
delegate to the shared updater. No credential value is copied to documentation,
reports, new configuration or model artifacts.

| Current path | Disposition after approval |
|---|---|
| `StockRecommenderFromModel.py` | Replace with thin frontend for `stockguidance.cli.recommend` |
| `batch_run.py` | Replace with thin frontend for `stockguidance.cli.batch` |
| `benchmark_index_dataset.py` | Replace with thin frontend for `stockguidance.cli.inspect_features` |
| `benchmark_score_reading.py` | Replace with thin frontend for `stockguidance.cli.inspect_features` |
| `benchmark_score_reading_threaded.py` | Replace with thin frontend for `stockguidance.cli.inspect_features` |
| `calculate_ticker_correlations.py` | Replace with thin frontend for `stockguidance.cli.inspect_features:correlation_main` |
| `convert_correlations_to_binary.py` | Retire superseded source/test; preserve the local recovery copy |
| `data_loading_shared.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/create_dbinfo.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/database_date_id_queries.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/db_connect.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/db_info.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/identify_tickers_with_missing_dates.py` | Retire superseded source/test; preserve the local recovery copy |
| `database/populate_db_daily_delta_stock_data.py` | Replace with thin frontend for `stockguidance.cli.update_market_data` |
| `database/populate_db_raw_stock_info.py` | Replace with thin frontend for `stockguidance.cli.update_market_data` |
| `database/populate_db_ticker_info.py` | Replace with thin frontend for `stockguidance.cli.update_market_data` |
| `database/query_tickers_by_date.py` | Retire superseded source/test; preserve the local recovery copy |
| `debug_feature_logging.py` | Replace with thin frontend for `stockguidance.cli.inspect_features` |
| `feature_vector_builder.py` | Retire superseded source/test; preserve the local recovery copy |
| `feature_windowing.py` | Retire superseded source/test; preserve the local recovery copy |
| `generate_rep_averages_to_compare_single_runs.py` | Retire superseded source/test; preserve the local recovery copy |
| `get_ticker_position_on_date.py` | Retire superseded source/test; preserve the local recovery copy |
| `incremental_training_harness.py` | Replace with thin frontend for `stockguidance.cli.incremental` |
| `load_database_rows_into_tensors.py` | Retire superseded source/test; preserve the local recovery copy |
| `load_latest_raw_stock_data_date.py` | Retire superseded source/test; preserve the local recovery copy |
| `load_raw_stock_data_rows.py` | Retire superseded source/test; preserve the local recovery copy |
| `models/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `models/convolution_transformer.py` | Retire superseded source/test; preserve the local recovery copy |
| `models/multi_layer_mlp.py` | Retire superseded source/test; preserve the local recovery copy |
| `models/sparse_linear.py` | Retire superseded source/test; preserve the local recovery copy |
| `models/utils.py` | Retire superseded source/test; preserve the local recovery copy |
| `organize_rep_runs.py` | Retire superseded source/test; preserve the local recovery copy |
| `polygon_fetch.py` | Retain existing credential constant; delegate command to the shared updater |
| `populate_db_ticker_info.py` | Retire superseded source/test; preserve the local recovery copy |
| `scripts/aggregate_run_options.py` | Replace with thin frontend for `stockguidance.cli.reports` |
| `scripts/generate_simulation_stats.py` | Replace with thin frontend for `stockguidance.cli.reports` |
| `scripts/get_metrics_for_runs.py` | Replace with thin frontend for `stockguidance.cli.reports` |
| `test_reuse_tensors.py` | Retire superseded source/test; preserve the local recovery copy |
| `test_training_instance_provider.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_family_controller.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_feature_vector_builder.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_feature_vector_builder_from_db.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_metrics_eval_epoch.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_mse_loss_behavior.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_mse_restricted.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_ranking_metrics.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_simulate_trading_utils.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_split_datasets_floating_head_tail_provider.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_splits_head_tail.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_stock_recommender_from_existing_model_uniform_range.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_stock_recommender_from_model_random_argmax.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_ticker_id_persistence.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_train_pytorch_dry_run_head_tail.py` | Retire superseded source/test; preserve the local recovery copy |
| `tests/test_training_instance_provider_unit.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/ai_type.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/args_train.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/dataloaders.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/dataset/ticker_info_for_dates.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/dataset/time_training_instance.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/dataset_classification.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/datasets.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/factory.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/file_structure_family_run_handler.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/file_structure_single_run_handler.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/io_run.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/label/transform/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/label/transform/transformers.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/legacy_arguments.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/legacy_training.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/loss_functions/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/loss_functions/losses.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/loss_functions/ranking.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/loss_functions/ranking_error.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/metric_file_handler.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/metrics.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/model_loader.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/rep_handler.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/splits.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/state/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/state/family_run.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/state/single_run.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/torch_interface/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/torch_interface/compat.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/torch_interface/device.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/torch_interface/memory.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/train_single_run.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/training_bundle.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/util/__init__.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/util/argparse_utils.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/util/date_bounds.py` | Retire superseded source/test; preserve the local recovery copy |
| `trainer/util/utils.py` | Retire superseded source/test; preserve the local recovery copy |
| `training_instance_provider.py` | Retire superseded source/test; preserve the local recovery copy |
| `verify_operations/verify_daily_delta_stock_data.py` | Replace with thin frontend for `stockguidance.cli.validate_database` |
| `verify_operations/verify_daily_delta_stock_data_per_ticker.py` | Replace with thin frontend for `stockguidance.cli.validate_database` |
| `verify_operations/verify_raw_stock_load.py` | Replace with thin frontend for `stockguidance.cli.validate_database` |
| `viewers/compare_run_statistics_viewer.py` | Replace with thin frontend for `stockguidance.viewers.reports` |
| `viewers/compare_runs_shared.py` | Retire superseded source/test; preserve the local recovery copy |
| `viewers/compare_runs_viewer.py` | Replace with thin frontend for `stockguidance.viewers.reports` |
| `viewers/correlation_matrix_viewer.py` | Retire superseded source/test; preserve the local recovery copy |
| `viewers/examine_models_viewer.py` | Replace with thin frontend for `stockguidance.viewers.artifacts` |
| `viewers/pages/compare_runs.py` | Replace with thin frontend for `stockguidance.viewers.reports` |
| `viewers/pages/compare_statistics.py` | Replace with thin frontend for `stockguidance.viewers.reports` |
| `viewers/pages/examine_models.py` | Replace with thin frontend for `stockguidance.viewers.artifacts` |
| `viewers/populate_runs_viewer.py` | Retire superseded source/test; preserve the local recovery copy |
| `viewers/populate_statistics_viewer.py` | Retire superseded source/test; preserve the local recovery copy |
| `viewers/run_selection_dashboard.py` | Replace with thin frontend for `stockguidance.viewers.reports` |
