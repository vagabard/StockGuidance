import os
import argparse
import csv
import re
import numpy as np
from typing import List, Dict, Optional
from trainer.state.family_run import FamilyRun
from trainer.state.single_run import SingleRun

def find_single_runs(path: str) -> List[str]:
    """Find all single run directories given a path which could be a family or a single run."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return []

    # Check if it's a family run directory (contains family_run_state.json or family_state.json)
    if os.path.exists(os.path.join(path, "family_run_state.json")) or os.path.exists(os.path.join(path, "family_state.json")):
        single_runs = []
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            if os.path.isdir(item_path):
                # Check for single_run_epoch.json or any single_run_N.json
                is_single_run = os.path.exists(os.path.join(item_path, "single_run_epoch.json"))
                if not is_single_run:
                    for subitem in os.listdir(item_path):
                        if subitem.startswith("single_run_") and subitem.endswith(".json"):
                            is_single_run = True
                            break
                if is_single_run:
                    single_runs.append(item_path)
        return single_runs

    # Check if it's already a single run directory
    is_single_run = os.path.exists(os.path.join(path, "single_run_epoch.json"))
    if not is_single_run:
        for subitem in os.listdir(path):
            if subitem.startswith("single_run_") and subitem.endswith(".json"):
                is_single_run = True
                break
    if is_single_run:
        return [path]

    return []

def get_available_epochs(single_run_path: str) -> List[int]:
    """Find all unique epochs for which stock_simulation CSVs exist in at least one rep."""
    reps = [d for d in os.listdir(single_run_path) if os.path.isdir(os.path.join(single_run_path, d)) and d.startswith("rep")]
    epochs = set()
    for rep in reps:
        rep_path = os.path.join(single_run_path, rep)
        files = [f for f in os.listdir(rep_path) if f.startswith("stock_simulation_") and f.endswith(".csv")]
        for f in files:
            match = re.search(r"_epoch_(\d+)\.csv", f)
            if match:
                epochs.add(int(match.group(1)))
    return sorted(list(epochs))

def get_rep_csvs(single_run_path: str, epoch: Optional[int] = None) -> List[str]:
    """Find all stock_simulation CSVs for all reps in a single run."""
    csvs = []
    # Match pattern: rep{n}/stock_simulation_{days}days_epoch_{epoch}.csv
    # We look for the highest epoch if none specified

    reps = [d for d in os.listdir(single_run_path) if os.path.isdir(os.path.join(single_run_path, d)) and d.startswith("rep")]

    for rep in reps:
        rep_path = os.path.join(single_run_path, rep)
        files = [f for f in os.listdir(rep_path) if f.startswith("stock_simulation_") and f.endswith(".csv")]

        if not files:
            continue

        selected_file = None
        if epoch is not None:
            pattern = re.compile(rf"stock_simulation_.*_epoch_{epoch}\.csv")
            for f in files:
                if pattern.match(f):
                    selected_file = f
                    break
        else:
            # Find latest epoch
            max_epoch = -1
            for f in files:
                match = re.search(r"_epoch_(\d+)\.csv", f)
                if match:
                    curr_epoch = int(match.group(1))
                    if curr_epoch > max_epoch:
                        max_epoch = curr_epoch
                        selected_file = f

        if selected_file:
            csvs.append(os.path.join(rep_path, selected_file))

    return csvs

def process_single_run(single_run_path: str, epoch: Optional[int] = None, top_k: int = 1):
    csv_files = get_rep_csvs(single_run_path, epoch)
    if not csv_files:
        print(f"No simulation CSVs found for {single_run_path}")
        return None

    column_name = f"Return Percent top{top_k}"
    all_reps_data = [] # List of lists (days)

    for csv_file in csv_files:
        daily_returns = []
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if column_name in row:
                        daily_returns.append(float(row[column_name]))
            if daily_returns:
                all_reps_data.append(daily_returns)
        except Exception as e:
            print(f"Error reading {csv_file}: {e}")

    if not all_reps_data:
        return None

    # Transpose to get all reps for each day
    # We assume all reps have the same number of days
    min_days = min(len(rep_data) for rep_data in all_reps_data)

    day_averages = []
    for day_idx in range(min_days):
        day_vals = [rep_data[day_idx] for rep_data in all_reps_data]
        day_averages.append(np.mean(day_vals))

    epoch_average = np.mean(day_averages)

    return {
        "path": single_run_path,
        "epoch": epoch,
        "day_averages": day_averages,
        "epoch_average": epoch_average,
        "num_reps": len(all_reps_data),
        "num_days": min_days
    }

def main():
    parser = argparse.ArgumentParser(description="Generate rep averages to compare single runs.")
    parser.add_argument("paths", nargs="+", help="Run family directories or single run directories.")
    parser.add_argument("--epoch", type=int, help="Specify epoch to analyze. Defaults to latest.")
    parser.add_argument("--all-epochs", action="store_true", help="Process all available epochs.")
    parser.add_argument("--top-k", type=int, default=1, choices=[1, 3, 5, 10], help="Top K recommendation to use (default: 1).")
    parser.add_argument("--output", type=str, default=None, help="Optionally save ALL results to a single CSV file.")

    args = parser.parse_args()

    # Dictionary to collect results per single run for per-run CSV writing
    results_by_run = {} # path -> list of results
    all_results = []
    for path in args.paths:
        single_run_paths = find_single_runs(path)
        for srp in single_run_paths:
            if srp not in results_by_run:
                results_by_run[srp] = []
            if args.all_epochs:
                epochs = get_available_epochs(srp)
                for ep in epochs:
                    result = process_single_run(srp, ep, args.top_k)
                    if result:
                        results_by_run[srp].append(result)
                        all_results.append(result)
            else:
                result = process_single_run(srp, args.epoch, args.top_k)
                if result:
                    results_by_run[srp].append(result)
                    all_results.append(result)

    if not all_results:
        print("No results to display.")
        return

    # Print results
    print(f"\nComparing results (Top {args.top_k}):")
    header = f"{'Run Directory':<60} | {'Epoch':<5} | {'Reps':<5} | {'Days':<5} | {'Epoch Avg Return'}"
    print(header)
    print("-" * len(header))
    for res in all_results:
        folder_name = os.path.basename(res['path'])
        epoch_str = str(res['epoch']) if res['epoch'] is not None else "N/A"
        print(f"{folder_name:<60} | {epoch_str:<5} | {res['num_reps']:<5} | {res['num_days']:<5} | {res['epoch_average']:.6f}")

    # Write per-run results to their respective directories
    for srp, results in results_by_run.items():
        if not results:
            continue
        output_path = os.path.join(srp, "rep_averages.csv")
        try:
            with open(output_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=["run_directory", "epoch", "num_reps", "num_days", "epoch_avg_return"])
                writer.writeheader()
                for res in results:
                    writer.writerow({
                        "run_directory": os.path.basename(res['path']),
                        "epoch": res['epoch'],
                        "num_reps": res['num_reps'],
                        "num_days": res['num_days'],
                        "epoch_avg_return": res['epoch_average']
                    })
            print(f"Saved results for {os.path.basename(srp)} to {output_path}")
        except Exception as e:
            print(f"Error saving to {output_path}: {e}")

    if args.output:
        try:
            with open(args.output, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=["run_directory", "epoch", "num_reps", "num_days", "epoch_avg_return"])
                writer.writeheader()
                for res in all_results:
                    writer.writerow({
                        "run_directory": os.path.basename(res['path']),
                        "epoch": res['epoch'],
                        "num_reps": res['num_reps'],
                        "num_days": res['num_days'],
                        "epoch_avg_return": res['epoch_average']
                    })
            print(f"\nAll results saved to {args.output}")
        except Exception as e:
            print(f"Error saving to global CSV: {e}")

if __name__ == "__main__":
    main()
