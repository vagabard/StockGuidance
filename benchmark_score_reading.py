import os
import time
import numpy as np
import psutil
import argparse
from typing import List

def get_memory_usage():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024)  # in MB

def read_score_file(file_path: str) -> np.ndarray:
    # Based on the CSV structure seen: one float per line
    # Using np.fromfile or a manual list comprehension might be faster than np.loadtxt
    with open(file_path, 'r') as f:
        return np.array([float(line.strip()) for line in f], dtype=np.float32)

def find_score_files(root_dir: str) -> List[str]:
    print(f"Finding all stock_scores_epoch_*.csv files in {root_dir}...")
    start_time = time.time()
    score_files = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.startswith("stock_scores_epoch_") and file.endswith(".csv"):
                score_files.append(os.path.join(root, file))

    elapsed = time.time() - start_time
    print(f"Found {len(score_files)} files in {elapsed:.2f} seconds.")
    return score_files

def run_benchmark(root_dir: str, limit: int = None):
    initial_mem = get_memory_usage()
    print(f"Initial Memory Usage: {initial_mem:.2f} MB")

    score_files = find_score_files(root_dir)
    if not score_files:
        print("No score files found.")
        return

    if limit and limit < len(score_files):
        print(f"Limiting to first {limit} files for benchmarking.")
        score_files = score_files[:limit]

    print(f"Reading {len(score_files)} files...")
    start_time = time.time()

    all_scores = []
    # Read first file to get shape if needed, or just append and concat
    # To be memory efficient, we can pre-allocate if we know the size,
    # but since they are small, let's see how list append + vstack performs first.

    for i, file_path in enumerate(score_files):
        try:
            scores = read_score_file(file_path)
            all_scores.append(scores)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

        if (i + 1) % 1000 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed
            print(f"Read {i + 1}/{len(score_files)} files... ({rate:.2f} files/sec)")

    # Convert to a single numpy array
    print("Concatenating into a single numpy array...")
    concat_start = time.time()
    try:
        final_array = np.array(all_scores, dtype=np.float32)
    except MemoryError:
        print("MemoryError during concatenation!")
        final_array = None
    concat_time = time.time() - concat_start

    total_time = time.time() - start_time
    final_mem = get_memory_usage()

    print("\n--- Benchmark Results ---")
    print(f"Files Read: {len(score_files)}")
    if final_array is not None:
        print(f"Final Array Shape: {final_array.shape}")
        print(f"Final Array Data Type: {final_array.dtype}")
    else:
        print("Final Array: Failed to create due to memory limits.")
    print(f"Time Taken to Read: {total_time - concat_time:.2f} seconds")
    print(f"Time Taken to Concatenate: {concat_time:.2f} seconds")
    print(f"Total Time: {total_time:.2f} seconds")
    print(f"Memory Usage Before: {initial_mem:.2f} MB")
    print(f"Memory Usage After: {final_mem:.2f} MB")
    print(f"Memory Increment: {final_mem - initial_mem:.2f} MB")
    print(f"Average Memory per File: {(final_mem - initial_mem) / len(score_files) * 1024:.2f} KB")

    return final_array

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark score file reading.")
    parser.add_argument("directory", type=str, help="Directory to search for score files.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of files to read.")

    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        # Check if it exists relative to 'data/'
        alt_path = os.path.join("data", args.directory)
        if os.path.isdir(alt_path):
            args.directory = alt_path
        else:
            print(f"Error: {args.directory} is not a directory.")
            exit(1)

    run_benchmark(args.directory, args.limit)
