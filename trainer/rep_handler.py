from __future__ import annotations

import csv
import gc
import os
import queue
import threading
import time
import traceback
from typing import List, TYPE_CHECKING

import torch
from torch import nn
from torch.optim.lr_scheduler import ExponentialLR

from StockRecommenderFromModel import StockRecommenderFromModel
from trainer.dataloaders import build_dataloaders
from trainer.metrics import eval_epoch
from trainer.model_loader import load_model_from_checkpoint

if TYPE_CHECKING:
    from trainer.state.family_run import FamilyRun
    from trainer.state.single_run import SingleRun
    from trainer.training_bundle import TrainingBundle
    from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler


_rep_queue: queue.Queue = queue.Queue()
_rep_threads: List[threading.Thread] = []
_rep_stop_event = threading.Event()
_ensemble_lock = threading.Lock()
_progress_lock = threading.Lock()

def _rep_worker(device, input_size, output_size, save_interval):
    """Worker function to process repetition tasks from the queue."""
    while not _rep_stop_event.is_set():
        try:
            task = _rep_queue.get(timeout=1)
            if task is None:
                _rep_queue.task_done()
                break

            run_idx, training_bundle_list, rep_files_list, family_run, single_run, \
            start_epoch, run_start_time, cumulative_total_epochs_across_reps, \
            current_epochs_across_reps_container, ensemble_parts = task

            try:
                RepHandler.run_single_repetition_logic(
                    run_idx=run_idx,
                    training_bundle_list=training_bundle_list,
                    rep_files_list=rep_files_list,
                    family_run=family_run,
                    single_run=single_run,
                    input_size=input_size,
                    output_size=output_size,
                    device=device,
                    start_epoch=start_epoch,
                    run_start_time=run_start_time,
                    cumulative_total_epochs_across_reps=cumulative_total_epochs_across_reps,
                    current_epochs_across_reps_container=current_epochs_across_reps_container,
                    save_interval=save_interval,
                    ensemble_parts=ensemble_parts
                )
            except Exception as e:
                print(f"Error in repetition {run_idx}: {e}")
                traceback.print_exc()
            finally:
                _rep_queue.task_done()
        except queue.Empty:
            continue

def _train_and_log_epoch(
    model,
    optimizer,
    scaler,
    use_amp,
    amp_dtype,
    train_loader,
    val_loader,
    device,
    family_run: FamilyRun,
    single_run: SingleRun,
    epoch,
    run_start_time,
    cumulative_total_epochs_across_reps,
    current_epochs_across_reps,
    one_file_structure: FileStructureSingleRunHandler,
    rep_index,
    save_interval,
    input_size,
    output_size,
):
    from trainer.train_single_run import _train_one_epoch_for_rep

    should_print_message = False
    if family_run.log_interval and family_run.log_interval > 0 and (
        epoch % family_run.log_interval == 0 or epoch == family_run.total_epochs_to_train):
        should_print_message = epoch % family_run.log_interval == 0 and epoch != 0

    epoch_start = time.time()

    train_mse, val_mse, train_top10, val_top10, all_counts = _train_one_epoch_for_rep(
        model=model,
        optimizer=optimizer,
        scaler=scaler,
        use_amp=use_amp,
        amp_dtype=amp_dtype,
        train_loader=train_loader,
        val_loader=val_loader,
        device=device,
        family_run=family_run,
        epoch=epoch,
    )

    train_mse = float(train_mse)
    val_mse = float(val_mse)
    train_top10 = float(train_top10)
    val_top10 = float(val_top10)

    epoch_dur = time.time() - epoch_start
    if should_print_message:
        current_time = time.time()
        elapsed_time = current_time - run_start_time
        epochs_remaining = cumulative_total_epochs_across_reps - current_epochs_across_reps
        time_remaining = (
                                 elapsed_time / current_epochs_across_reps) * epochs_remaining if current_epochs_across_reps > 0 else 0
        rem_h = int(time_remaining // 3600)
        rem_m = int((time_remaining % 3600) // 60)
        rem_s = int(time_remaining % 60)

        tc = all_counts["train"]
        vc = all_counts["val"]

        msg = (
            f"Epoch {epoch:03d} | train_mse={train_mse:.6f} val_mse={val_mse:.6f} "
            f"train_top10={train_top10:.6f} val_top10={val_top10:.6f} "
            + (" [rank_err:on]" if family_run.enable_rank_error_loss_in_training else "")
            + f" | time={epoch_dur:.2f}s"
            + f" | time remaining={rem_h:02d}:{rem_m:02d}:{rem_s:02d}"
            + f"\n         | Train: LBL pos={tc['label_pos']} neg={tc['label_neg']} zero={tc.get('label_zero', 0)} | PRD pos={tc['pred_pos']} neg={tc['pred_neg']} zero={tc.get('pred_zero', 0)}"
            + f"\n         | Val:   LBL pos={vc['label_pos']} neg={vc['label_neg']} zero={vc.get('label_zero', 0)} | PRD pos={vc['pred_pos']} neg={vc['pred_neg']} zero={vc.get('pred_zero', 0)}"
        )
        print(msg)

    row = [epoch, train_mse, val_mse, train_top10, val_top10, optimizer.param_groups[0]["lr"]]
    one_file_structure.get_metrics_file_handler_for_rep(rep_index).write_row(row)

    tc = all_counts["train"]
    vc = all_counts["val"]
    count_row = [
        epoch,
        tc["label_pos"], tc["label_neg"], tc.get("label_zero", 0), tc["pred_pos"], tc["pred_neg"], tc.get("pred_zero", 0),
        vc["label_pos"], vc["label_neg"], vc.get("label_zero", 0), vc["pred_pos"], vc["pred_neg"], vc.get("pred_zero", 0)
    ]
    one_file_structure.get_count_metrics_file_handler_for_rep(rep_index).write_row(count_row)
    return train_mse, val_mse, train_top10, val_top10


class RepHandler:
    def submit_rep_for_processing(
        run_idx: int,
        training_bundle_list: List[TrainingBundle],
        rep_files_list: List[FileStructureSingleRunHandler],
        family_run: FamilyRun,
        single_run: SingleRun,
        start_epoch: int,
        run_start_time: float,
        cumulative_total_epochs_across_reps: int,
        current_epochs_across_reps_container: List[int],
        ensemble_parts: List[nn.Module]
    ) -> None:
        """Submit a repetition for background processing."""
        _rep_queue.put((
            run_idx,
            training_bundle_list,
            rep_files_list,
            family_run,
            single_run,
            start_epoch,
            run_start_time,
            cumulative_total_epochs_across_reps,
            current_epochs_across_reps_container,
            ensemble_parts
        ))

    @staticmethod
    def initialize_workers(device, input_size, output_size, save_interval, num_threads=2):
        """Start the worker threads if they are not already running."""
        global _rep_threads
        _rep_stop_event.clear()
        if not _rep_threads:
            for i in range(num_threads):
                t = threading.Thread(
                    target=_rep_worker,
                    args=(device, input_size, output_size, save_interval),
                    name=f"RepWorker-{i}",
                    daemon=True
                )
                t.start()
                _rep_threads.append(t)

    @staticmethod
    def shutdown_workers():
        """Stop worker threads and wait for them to exit."""
        global _rep_threads
        _rep_stop_event.set()
        for _ in _rep_threads:
            _rep_queue.put(None)
        for t in _rep_threads:
            t.join()
        _rep_threads = []

    @staticmethod
    def wait_for_all_reps():
        """Wait for all submitted repetitions to complete."""
        _rep_queue.join()

    @staticmethod
    def run_single_repetition_logic(
        run_idx: int,
        training_bundle_list: List[TrainingBundle],
        rep_files_list: List[FileStructureSingleRunHandler],
        family_run: FamilyRun,
        single_run: SingleRun,
        input_size: int,
        output_size: int,
        device,
        start_epoch: int,
        run_start_time: float,
        cumulative_total_epochs_across_reps: int,
        current_epochs_across_reps_container: List[int],
        save_interval: int,
        ensemble_parts: List[nn.Module]
    ) -> None:
        from trainer.train_single_run import _log_gpu_usage
        rep_index = run_idx + 1
        one_file_structure = rep_files_list[run_idx]
        bundle = training_bundle_list[run_idx]

        # Load from cache if needed
        if bundle.cache_path and bundle.model is None:
            m, opt_state, sc_state = load_model_from_checkpoint(
                bundle.cache_path,
                family_run,
                input_size,
                output_size,
                single_run.mlp_layers,
                single_run.activation,
                single_run.prior_days,
                device=torch.device("cpu"),
                map_location="cpu"
            )

            # Create an optimizer that is bound to the LOADED model's parameters, not a new dummy model.
            opt_name = str(single_run.optimizer or "adam").lower()
            lr = float(single_run.lr)
            if opt_name == "adam":
                opt = torch.optim.Adam(m.parameters(), lr=lr)
            elif opt_name == "adamw":
                opt = torch.optim.AdamW(m.parameters(), lr=lr)
            elif opt_name == "sgd":
                opt = torch.optim.SGD(m.parameters(), lr=lr)
            else:
                raise ValueError(f"Unsupported optimizer: {single_run.optimizer}")

            # Recreate scaler consistent with bundle AMP dtype
            sc = torch.amp.GradScaler('cuda', enabled=(bundle.amp_dtype == torch.float16))  # type: ignore[attr-defined]

            if opt_state:
                opt.load_state_dict(opt_state)
            if sc and sc_state:
                sc.load_state_dict(sc_state)

            bundle.model = m
            bundle.optimizer = opt
            bundle.scaler = sc
            # Re-create scheduler
            gamma: float = getattr(single_run, "learning_rate_adjustment_parameter", 0.998)
            bundle.scheduler = ExponentialLR(bundle.optimizer, gamma=float(gamma))

        model = bundle.model
        if model is not None:
            print(f"model from {next(model.parameters()).device} to device ", device)
            model.to(device)
        optimizer = bundle.optimizer
        if optimizer is not None:
            # Move optimizer state to device
            for state in optimizer.state.values():
                for k, v in state.items():
                    if isinstance(v, torch.Tensor):
                        state[k] = v.to(device)
        scaler = bundle.scaler
        use_amp = bundle.use_amp
        amp_dtype = bundle.amp_dtype
        train_loader = bundle.training_instance_provider
        val_loader = bundle.validation_instance_provider
        if use_amp and amp_dtype == torch.float16:
            print("Precision runtime: autocast=float16 with GradScaler")
        elif use_amp and amp_dtype == torch.bfloat16:
            print("Precision runtime: autocast=bfloat16 (no GradScaler)")
        scheduler = bundle.scheduler

        # In cached mode, we train this model through all epochs before moving to next rep.
        # Note: original nested loops were (outer epochs, reps, inner epochs).
        # Here we do (reps, all epochs) to keep model in memory.
        for epoch in range(start_epoch, single_run.epochs + 1):
            with _progress_lock:
                current_epochs_across_reps_container[0] += 1
                current_val = current_epochs_across_reps_container[0]

            _train_and_log_epoch(
                model=model,
                optimizer=optimizer,
                scaler=scaler,
                use_amp=use_amp,
                amp_dtype=amp_dtype,
                train_loader=train_loader,
                val_loader=val_loader,
                device=device,
                family_run=family_run,
                single_run=single_run,
                epoch=epoch,
                run_start_time=run_start_time,
                cumulative_total_epochs_across_reps=cumulative_total_epochs_across_reps,
                current_epochs_across_reps=current_val,
                one_file_structure=one_file_structure,
                rep_index=rep_index,
                save_interval=save_interval,
                input_size=input_size,
                output_size=output_size,
            )

            # Calculate scores at trading intervals
            if family_run.trading_interval > 0 and epoch % family_run.trading_interval == 0:
                rec = StockRecommenderFromModel(
                    single_run,
                    [model],
                    device=str(device),
                    model_dict={"input_size": int(input_size), "output_size": int(output_size),
                                "prior_days": int(single_run.prior_days),
                                "day_change_centered": single_run.day_change_centered},
                )
                print(f"Running periodic score generation for rep {rep_index} at epoch {epoch}...")
                with torch.no_grad():
                    feature_label_date_gen = bundle.testing_instance_provider.get_next_feature_label_date()
                    # Use the model directly from the recommender (it might have been wrapped/moved)
                    for feat_vec, _, feature_date_id in feature_label_date_gen:
                        input_tensor = feat_vec.unsqueeze(0).to(device)
                        rec.generateStockScoresForNetwork(
                            network=rec._models[0],
                            inputs=input_tensor,
                            save_to_file=True,
                            epoch_name=str(epoch),
                            date_id=feature_date_id,
                            network_index=rep_index - 1
                        )
                        del input_tensor
                    del feature_label_date_gen
                del rec
                if str(device).startswith("cuda"):
                    torch.cuda.empty_cache()

            if scheduler is not None:
                scheduler.step()

            # Periodic GC and memory diagnostic
            if epoch % 5 == 0:
                gc.collect()
                if str(device).startswith("cuda"):
                    torch.cuda.empty_cache()

            if str(device).startswith("cuda"):
                torch.cuda.empty_cache()

        _log_gpu_usage(device, family_run.total_epochs_to_train + 1, single_run)

        if model is not None:
            model.to("cpu")
        # Move optimizer state to CPU
        if optimizer is not None:
            for state in optimizer.state.values():
                for k, v in state.items():
                    if isinstance(v, torch.Tensor):
                        state[k] = v.to("cpu")

        # If caching is enabled, save back to disk and clear memory
        if bundle.cache_path:
            checkpoint = {
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scaler_state_dict': scaler.state_dict() if scaler is not None else None,
            }
            torch.save(checkpoint, bundle.cache_path)
            bundle.model = None
            bundle.optimizer = None
            bundle.scaler = None
            bundle.scheduler = None
            del model, optimizer, scaler, checkpoint
            gc.collect()
        else:
            with _ensemble_lock:
                ensemble_parts.append(model)

    @staticmethod
    def evaluate_single_repetition(
        run_idx: int,
        training_bundle_list: List[TrainingBundle],
        rep_files_list: List[FileStructureSingleRunHandler],
        family_run: FamilyRun,
        single_run: SingleRun,
        input_size: int,
        output_size: int,
        device
    ) -> None:
        rep_index = run_idx + 1
        fs = rep_files_list[run_idx]
        bundle = training_bundle_list[run_idx]

        # Load from cache if needed for final evaluation
        if bundle.cache_path and bundle.model is None:
            m, _, _ = load_model_from_checkpoint(
                bundle.cache_path,
                family_run,
                input_size,
                output_size,
                single_run.mlp_layers,
                single_run.activation,
                single_run.prior_days,
                device=torch.device("cpu"),
                map_location="cpu"
            )
            bundle.model = m

        # Cleanup cache files if they exist
        if bundle.cache_path and os.path.exists(bundle.cache_path):
            try:
                os.remove(bundle.cache_path)
            except Exception as e:
                print(f"Warning: Failed to remove cache file {bundle.cache_path}: {e}")

        metrics_file = fs.get_metrics_file_for_rep(rep_index)
        # suffix = fs.suffix
        model = bundle.model
        # optimizer = bundle.optimizer
        use_amp = bundle.use_amp
        amp_dtype = bundle.amp_dtype
        test_iter = build_dataloaders(family_run, training_bundle_list[run_idx].testing_instance_provider)
        # Build a fresh iterator for test as well when not using DataLoader
        if test_iter is not None:
            if model is not None:
                model.to(device)
            test_mse, test_top10, test_rde, test_counts = eval_epoch(
                model,
                test_iter,
                device,
                amp_enabled=use_amp,
                amp_dtype=amp_dtype,
                rank_lower=int(family_run.rank_error_lower_bound),
                rank_upper=int(family_run.rank_error_upper_bound),
                compute_rank_err=bool(family_run.enable_rank_error_loss_in_training),
            )
            if model is not None:
                model.to("cpu")
        else:
            test_mse, test_top10, test_rde, test_counts = 0.0, 0.0, 0.0, {}
        print("Final Test Metrics:")
        print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_rank_disp={test_rde:.3f}")

        # Append final test metrics to a sidecar file next to metrics CSV (include suffix)
        test_metrics_path = os.path.splitext(metrics_file)[0] + "_test.csv"
        with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
            writer_test = csv.writer(f_test)
            writer_test.writerow(["metric", "value"])
            writer_test.writerow(["test_mse", test_mse])
            writer_test.writerow(["test_top10_mse", test_top10])
            writer_test.writerow(["test_rank_disp", test_rde])

        print(f"Wrote per-epoch metrics to: {metrics_file}")
        print(f"Wrote final test metrics to: {test_metrics_path}")

        try:
            if bool(family_run.simulate_after_training):
                after_recommender = StockRecommenderFromModel(
                    single_run,
                    [bundle.cache_path if bundle.cache_path else model],
                    single_run.file_structure.get_directory_for_rep(rep_index),
                    device=str(device),
                    model_dict={"input_size": int(input_size), "output_size": int(output_size),
                                "prior_days": int(single_run.prior_days),
                                "day_change_centered": single_run.day_change_centered},
                )
                print(
                    f"Running final score generation for rep {rep_index} at epoch {family_run.total_epochs_to_train}...")
                feature_label_date_gen = training_bundle_list[
                    run_idx].testing_instance_provider.get_next_feature_label_date()
                # Use the model directly from the recommender
                network = after_recommender._models[0]
                for feat_vec, _, feature_date_id in feature_label_date_gen:
                    input_tensor = feat_vec.unsqueeze(0).to(device)
                    after_recommender.generateStockScoresForNetwork(
                        network=network,
                        inputs=input_tensor,
                        save_to_file=True,
                        epoch_name=str(family_run.total_epochs_to_train),
                        date_id=feature_date_id,
                        network_index=rep_index - 1
                    )
                    del input_tensor
                if model is not None:
                    model.to("cpu")

                # Explicitly cleanup recommender and its models
                after_recommender.to_cpu()
                del after_recommender
                del network
                del feature_label_date_gen
        except Exception as save_e:
            traceback.print_exception(save_e)

        # Cleanup this rep's bundle and models to free memory
        training_bundle_list[run_idx].model = None
        training_bundle_list[run_idx].optimizer = None
        training_bundle_list[run_idx].scheduler = None
        training_bundle_list[run_idx].training_instance_provider = None
        training_bundle_list[run_idx].validation_instance_provider = None
        training_bundle_list[run_idx].testing_instance_provider = None

        del model
        del test_iter
