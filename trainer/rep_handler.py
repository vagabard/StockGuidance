from __future__ import annotations

import csv
import gc
import os
import psutil
import queue
import subprocess
import sys
import threading
import time
import traceback
from typing import List, TYPE_CHECKING, Dict

import torch
from torch import nn
from torch.optim.lr_scheduler import ExponentialLR

from StockRecommenderFromModel import StockRecommenderFromModel
from trainer.dataloaders import build_dataloaders
from trainer.metrics import eval_epoch
from trainer.model_loader import load_model_from_checkpoint

from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler
from trainer.io_run import save_model_and_manifest

if TYPE_CHECKING:
    from trainer.state.family_run import FamilyRun
    from trainer.state.single_run import SingleRun
    from trainer.training_bundle import TrainingBundle


_rep_queue: queue.Queue = queue.Queue()
_rep_processes: List[subprocess.Popen] = []
_rep_tasks: Dict[int, tuple] = {}  # pid -> task
_rep_stop_event = threading.Event()
_ensemble_lock = threading.Lock()
_progress_lock = threading.Lock()
_num_workers_target = 0
_worker_init_args = None
_manage_thread = None

def _count_python_processes() -> int:
    """Count the number of running Python processes."""
    count = 0
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return count

def _check_resources(device=None) -> bool:
    """Check if system resources are sufficient to start a new worker."""
    # Check system resources: >= 20GB free RAM, and < 20 python processes
    mem = psutil.virtual_memory()
    free_gb = mem.available / (1024 ** 3)
    python_proc_count = _count_python_processes()

    sufficient = free_gb >= 20.0 and python_proc_count < 20

    if sufficient and device and str(device).startswith("cuda"):
        try:
            # Check GPU memory usage
            # Use torch.cuda to get memory info for the current device
            device_idx = torch.cuda.current_device() if str(device) == "cuda" else int(str(device).split(":")[1])
            free_gpu_mem, total_gpu_mem = torch.cuda.mem_get_info(device_idx)
            used_gpu_mem = total_gpu_mem - free_gpu_mem
            gpu_usage_percent = (used_gpu_mem / total_gpu_mem) * 100

            # Ensure GPU usage is less than 90%
            if gpu_usage_percent >= 90.0:
                sufficient = False
        except Exception as e:
            print(f"Warning: Failed to check GPU resources: {e}")

    return sufficient

def _rep_worker_logic(task, device, input_size, output_size, save_interval):
    """Core logic for processing a repetition task."""
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

def _rep_worker(device, input_size, output_size, save_interval):
    """Worker function to process repetition tasks from the queue (deprecated in favor of processes)."""
    while not _rep_stop_event.is_set():
        try:
            task = _rep_queue.get(timeout=1)
            if task is None:
                _rep_queue.task_done()
                break

            _rep_worker_logic(task, device, input_size, output_size, save_interval)
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
        epoch % family_run.log_interval == 0 or epoch == single_run.epochs):
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
        single_run=single_run,
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
    def initialize_workers(device, input_size, output_size, save_interval, num_workers=4):
        """Set parameters for workers and start them as resources allow."""
        global _rep_processes, _num_workers_target, _worker_init_args, _rep_tasks, _manage_thread
        _rep_stop_event.clear()
        _num_workers_target = num_workers
        _worker_init_args = (device, input_size, output_size, save_interval)

        if _manage_thread is None or not _manage_thread.is_alive():
            _manage_thread = threading.Thread(target=RepHandler._manage_workers, daemon=True)
            _manage_thread.start()
            print(f"RepHandler: Started worker management thread (target={num_workers} workers).")


    @staticmethod
    def _manage_workers():
        """Periodically check resources and start new subprocess workers if needed."""
        global _rep_processes, _num_workers_target, _worker_init_args, _rep_tasks
        while not _rep_stop_event.is_set():
            # Filter out finished processes
            _rep_processes = [p for p in _rep_processes if p.poll() is None]
            # Clean up tasks for finished processes
            active_pids = [p.pid for p in _rep_processes]
            _rep_tasks = {pid: task for pid, task in _rep_tasks.items() if pid in active_pids}

            device = _worker_init_args[0] if _worker_init_args else None

            # Check if we should kill the most recent process due to resource exhaustion
            if len(_rep_processes) > 0 and not _check_resources(device):
                # Kill the most recent process
                p_to_kill = _rep_processes[-1]
                pid = p_to_kill.pid
                print(f"Resource limit exceeded. Terminating most recent process (PID {pid}) to free up resources.")
                p_to_kill.terminate()

                # The wait_and_done thread will handle calling task_done(),
                # but we actually want to RE-QUEUE it.
                # However, task_done() is expected by Queue.join().
                # If we terminate, we should re-put it in the queue.
                if pid in _rep_tasks:
                    task = _rep_tasks[pid]
                    _rep_queue.put(task)
                    print(f"Re-queued task for Rep {task[0]}.")

                # Remove from tracking
                _rep_processes.pop()
                if pid in _rep_tasks:
                    del _rep_tasks[pid]

                # Short sleep to allow resources to stabilize
                time.sleep(1)
                continue

            if len(_rep_processes) < _num_workers_target and not _rep_queue.empty():
                if len(_rep_processes) == 0 or _check_resources(device):
                    # Get next task to see which rep index to run
                    try:
                        task = _rep_queue.get_nowait()
                        run_idx = task[0]
                        single_run = task[4]

                        run_file = getattr(single_run, 'last_save_location', None)
                        if not run_file or not os.path.exists(run_file):
                            run_file = os.path.join(single_run.file_structure.run_dir, "single_run_epoch.json")
                            if not os.path.exists(run_file):
                                single_run.save(run_file)

                        if len(_rep_processes) > 0:
                            print(f"Resources sufficient. Starting process for Rep {run_idx}...")
                        else:
                            print(f"Starting first process for Rep {run_idx}...")

                        cmd = [
                            sys.executable,
                            os.path.abspath(sys.modules['trainer.train_single_run'].__file__),
                            "--single-run-file", run_file,
                            "--rep-index", str(run_idx)
                        ]

                        p = subprocess.Popen(cmd)
                        _rep_processes.append(p)
                        _rep_tasks[p.pid] = task

                        def wait_and_done(proc, q, pid_to_check):
                            proc.wait()
                            # If the process was terminated by us, it might have been re-queued already.
                            # But we still need to call task_done() to match the 'get()'.
                            q.task_done()

                        threading.Thread(target=wait_and_done, args=(p, _rep_queue, p.pid), daemon=True).start()

                    except queue.Empty:
                        pass
                    except Exception as e:
                        print(f"Error spawning process: {e}")
                        traceback.print_exc()
                else:
                    # Resources not sufficient, we'll try again after sleep
                    pass

            # Put in a 5 second sleep between checking to see if a new worker should be started.
            for _ in range(50):
                if _rep_stop_event.is_set():
                    break
                time.sleep(0.1)

    @staticmethod
    def shutdown_workers():
        """Stop worker processes and management thread."""
        global _rep_processes, _manage_thread
        _rep_stop_event.set()

        if _manage_thread:
            _manage_thread.join(timeout=2)
            _manage_thread = None

        # Terminate all processes
        for p in _rep_processes:
            if p.poll() is None:
                p.terminate()

        # Wait for them to finish
        for p in _rep_processes:
            p.wait()
        _rep_processes = []

    @staticmethod
    def wait_for_all_reps():
        """Wait for all submitted repetitions to complete."""
        _rep_queue.join()

    @staticmethod
    def evaluate_all_reps(
        training_bundle_list: List[TrainingBundle],
        rep_files_list: List[FileStructureSingleRunHandler],
        family_run: FamilyRun,
        single_run: SingleRun,
        input_size: int,
        output_size: int,
        device
    ) -> None:
        """Run final evaluation and model saving for all repetitions."""
        for run_idx in range(len(training_bundle_list)):
            RepHandler.evaluate_single_repetition(
                run_idx=run_idx,
                training_bundle_list=training_bundle_list,
                rep_files_list=rep_files_list,
                family_run=family_run,
                single_run=single_run,
                input_size=input_size,
                output_size=output_size,
                device=device
            )

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
                single_run,
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

                # Periodic simulation
                import simulate_trading
                print(f"Running periodic simulation for rep {rep_index} at epoch {epoch}...")
                simulate_trading.perform_analysis(
                    stock_recommenders=[rec],
                    top_k=family_run.simulate_top_k,
                    dates_to_handle=bundle.testing_instance_provider,
                    dbinfo_file=family_run.dbinfo_file,
                    out_filename_suffix=f"_epoch_{epoch}"
                )

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

                # Monitor GPU usage
                if not _check_resources(device):
                    print(f"Warning: System resources low during Rep {rep_index} Epoch {epoch}.")
                    if str(device).startswith("cuda"):
                        try:
                            device_idx = torch.cuda.current_device() if str(device) == "cuda" else int(str(device).split(":")[1])
                            free_gpu_mem, total_gpu_mem = torch.cuda.mem_get_info(device_idx)
                            used_gpu_mem = total_gpu_mem - free_gpu_mem
                            gpu_usage_percent = (used_gpu_mem / total_gpu_mem) * 100
                            print(f"  GPU Usage: {gpu_usage_percent:.2f}% ({used_gpu_mem / (1024**2):.0f}MB / {total_gpu_mem / (1024**2):.0f}MB)")
                        except Exception:
                            pass
                    # If resources are very low, we could potentially abort or sleep,
                    # but for now we just log it as the user only asked to "monitor and insure it's less than 90%".
                    # The _manage_workers thread will handle termination if it's too high.

            if str(device).startswith("cuda"):
                torch.cuda.empty_cache()

        _log_gpu_usage(device, single_run.epochs + 1, single_run)

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

            # Also save as the final model as it's needed for other processes or scoring
            fs = rep_files_list[run_idx]
            metrics_file = fs.get_metrics_file_for_rep(run_idx + 1)
            final_model_path = os.path.join(os.path.dirname(metrics_file), "model_final.pt")
            torch.save(checkpoint, final_model_path)
            # Update cache_path to the final model path so it persists
            bundle.cache_path = final_model_path

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
            # First try the bundle's cache path
            load_path = bundle.cache_path
            if not os.path.exists(load_path):
                # If that fails, try looking for model_final.pt in the rep directory
                rep_dir = fs.get_directory_for_rep(rep_index)
                load_path = os.path.join(rep_dir, "model_final.pt")

            if os.path.exists(load_path):
                m, _, _ = load_model_from_checkpoint(
                    load_path,
                    family_run,
                    single_run,
                    input_size,
                    output_size,
                    single_run.mlp_layers,
                    single_run.activation,
                    single_run.prior_days,
                    device=torch.device("cpu"),
                    map_location="cpu"
                )
                bundle.model = m
            else:
                print(f"Warning: Model not found for rep {rep_index} at {bundle.cache_path} or model_final.pt")
                return # Can't evaluate if model is missing

        # Cleanup cache files if they exist (only if we're not using them again)
        # We'll skip this if we want to preserve them for other processes,
        # or if we are the main process and we are done.
        # But for now, if the file is gone, it's fine, we already loaded the model.
        # The issue is that another call might try to load it again.
        if bundle.cache_path and os.path.exists(bundle.cache_path):
            try:
                # os.remove(bundle.cache_path)
                # print(f"Preserving cache file {bundle.cache_path} for potential reuse.")
                pass
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
        # Ensure directory exists for test metrics
        os.makedirs(os.path.dirname(test_metrics_path), exist_ok=True)
        with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
            writer_test = csv.writer(f_test)
            writer_test.writerow(["metric", "value"])
            writer_test.writerow(["test_mse", test_mse])
            writer_test.writerow(["test_top10_mse", test_top10])
            writer_test.writerow(["test_rank_disp", test_rde])

        print(f"Wrote per-epoch metrics to: {metrics_file}")
        print(f"Wrote final test metrics to: {test_metrics_path}")

        # Save model and manifest
        if model is not None:
            save_model_and_manifest(
                single_run=single_run,
                model=model,
                optimizer=bundle.optimizer,
                run_dir=os.path.dirname(metrics_file),
                suffix="",
                test_metrics_path=test_metrics_path,
                input_size=input_size,
                output_size=output_size
            )

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
                    f"Running final score generation for rep {rep_index} at epoch {single_run.epochs}...")
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
                        epoch_name=str(single_run.epochs),
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
