import os
import json
from typing import Dict, Any, List
from pathlib import Path

def aggregate_run_options(root_dir: str = r"c:\Projects\StockGuidance\data") -> Dict[str, Dict[Any, List[str]]]:
    """
    Crawls the root directory for family and single run objects,
    and aggregates different options used for each single run.
    """
    aggregated_results: Dict[str, Dict[Any, List[str]]] = {}

    if not os.path.exists(root_dir):
        print(f"Warning: Root directory {root_dir} does not exist.")
        return aggregated_results

    # Search for run_family directories
    for root, dirs, files in os.walk(root_dir):
        # The issue description says family_run directories start with run_family
        # We also need to find single_run objects which are often within these directories
        found_single_run = False
        for file in files:
            if file.startswith("single_run_") and file.endswith(".json"):
                file_path = os.path.join(root, file)
                found_single_run = True
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    family_data = data.get("family_run", {})
                    single_data = data.get("single_run", {})

                    # Merge data: single_run values should take precedence over family_run values
                    # We'll create a combined set of all fields from both
                    combined_run_info = {}

                    # 1. Start with family data
                    for key, value in family_data.items():
                        # Handle list fields in family_data (e.g. mlp_layers_list)
                        # We might want the specific value used for this single run,
                        # which is in single_data.
                        combined_run_info[key] = value

                    # 2. Update with single data (overrides family fields)
                    for key, value in single_data.items():
                        combined_run_info[key] = value

                    # 3. Infer family from path
                    parts = Path(os.path.relpath(file_path, root_dir)).parts
                    if len(parts) > 1 and parts[0].startswith("run_family_"):
                        combined_run_info["_family"] = parts[0]
                    else:
                        combined_run_info["_family"] = "None"

                    # Check for score files in the same directory as the single_run_*.json file
                    # OR in any subdirectories (like rep1, rep2, etc.)
                    # These files are used for recommendations.
                    has_score_files = False
                    # Use os.walk for efficiency if we are already in the directory,
                    # but here we just want to check subdirectories quickly.
                    for sub_root, sub_dirs, sub_files in os.walk(root):
                        for f_name in sub_files:
                            # stock_scores_epoch_40_date_12345_net_0.csv or .pt
                            if f_name.startswith("stock_scores_epoch_") and (f_name.endswith(".csv") or f_name.endswith(".pt")):
                                has_score_files = True
                                break
                            # stock_simulation_12days_epoch_10.csv (results of a simulation, implies scores)
                            if f_name.startswith("stock_simulation_") and f_name.endswith(".csv"):
                                has_score_files = True
                                break
                        if has_score_files:
                            break
                    combined_run_info["has_score_files"] = has_score_files

                    # Use a unique identifier for the single run
                    # timestamp_id is usually a good candidate
                    run_id = single_data.get("timestamp_id", file_path)

                    # Aggregate each field
                    for field, value in combined_run_info.items():
                        # Skip complex internal structures and specified ignored fields
                        ignored_fields = [
                            "file_structure", "single_runs_for_all_combinations", "dbinfo",
                            "activation_list", "base_args", "base_args_list", "base_optional_features",
                            "completed", "current_index", "day_change_centered_list", "dbinfo_file", "device",
                            "final_activation_list", "include_one_feature_list", "initial_run_id", "input_size", "limit",
                            "log_interval", "lr_list", "mlp_layers_list", "optimizer_list",
                            "output_size", "prior_days_list", "ticker_ids", "trading_interval"
                        ]
                        if field in ignored_fields:
                            continue

                        # Process hidden_layer_size: if it's a list, concatenate it into a single string
                        if field == "hidden_layer_size" and isinstance(value, list):
                            value = ",".join(map(str, value))

                        # Convert unhashable types to strings for dictionary keys
                        if isinstance(value, (list, dict, set)):
                            value_key = str(value)
                        else:
                            value_key = value

                        if field not in aggregated_results:
                            aggregated_results[field] = {}

                        if value_key not in aggregated_results[field]:
                            aggregated_results[field][value_key] = []

                        aggregated_results[field][value_key].append(run_id)

                except Exception as e:
                    print(f"Error processing {file_path}: {e}")

        if found_single_run:
            # If there is a single_run object found at a given directory,
            # then subdirectories of that directory should not be checked.
            dirs[:] = []

    return aggregated_results

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Aggregate StockGuidance run options.")
    parser.add_argument("directory", nargs="?", default=r"c:\Projects\StockGuidance\data",
                        help="Root directory to start searching for runs (default: c:\\Projects\\StockGuidance\\data)")

    args = parser.parse_args()

    results = aggregate_run_options(args.directory)

    import pprint
    pp = pprint.PrettyPrinter(indent=2)

    print(f"Aggregated Options from {args.directory}:")
    for field, values in results.items():
        print(f"\nField: {field}")
        for val, runs in values.items():
            print(f"  Value: {val}")
            print(f"    Single Runs ({len(runs)}): {runs[:5]}{'...' if len(runs) > 5 else ''}")

if __name__ == "__main__":
    main()
