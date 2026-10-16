import os
import time
import numpy as np
import psutil
import argparse
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

def get_memory_usage():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024)  # in MB

def read_score_file_with_path(file_path: str) -> (str, np.ndarray):
    """Read a single score file into a float32 NumPy array and return with path."""
    try:
        with open(file_path, 'r') as f:
            data = np.array([float(line.strip()) for line in f], dtype=np.float32)
            return file_path, data
    except Exception as e:
        return file_path, e

def find_score_files(root_dir: str) -> List[str]:
    """Recursively find all stock_scores_epoch_*.csv files."""
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

def add_to_dict_structure(root_dict: Dict[str, Any], path_parts: List[str], data: np.ndarray):
    """Add data to nested dictionary based on file path parts."""
    current = root_dict
    for i, part in enumerate(path_parts):
        if i == len(path_parts) - 1:
            current[part] = data
        else:
            if part not in current:
                current[part] = {}
            current = current[part]

def run_sequential(score_files: List[str], base_dir: str) -> Dict[str, Any]:
    """Sequentially read all score files into a dictionary hierarchy."""
    root_dict = {}
    print(f"Reading {len(score_files)} files sequentially...")
    start_time = time.time()

    for i, file_path in enumerate(score_files):
        try:
            _, data = read_score_file_with_path(file_path)
            # Relative path from base_dir to mirror hierarchy
            rel_path = os.path.relpath(file_path, base_dir)
            path_parts = rel_path.split(os.sep)
            add_to_dict_structure(root_dict, path_parts, data)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

        if (i + 1) % 1000 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed
            print(f"Read {i + 1}/{len(score_files)} files... ({rate:.2f} files/sec)")

    elapsed = time.time() - start_time
    return root_dict, elapsed

def run_threaded(score_files: List[str], base_dir: str, num_threads: int) -> Dict[str, Any]:
    """Read score files into a dictionary hierarchy using multithreading."""
    root_dict = {}
    print(f"Reading {len(score_files)} files with {num_threads} threads...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        results = list(executor.map(read_score_file_with_path, score_files))

    # After all files are read in parallel, add them to the dictionary sequentially
    # (Adding to dict is very fast compared to I/O and float conversion)
    for file_path, data in results:
        if not isinstance(data, Exception):
            rel_path = os.path.relpath(file_path, base_dir)
            path_parts = rel_path.split(os.sep)
            add_to_dict_structure(root_dict, path_parts, data)
        else:
            print(f"Failed to read file {file_path}: {data}")

    elapsed = time.time() - start_time
    return root_dict, elapsed

def run_multiprocess(score_files: List[str], base_dir: str, num_procs: int) -> Dict[str, Any]:
    """Read score files into a dictionary hierarchy using multiprocessing."""
    root_dict = {}
    print(f"Reading {len(score_files)} files with {num_procs} processes...")
    start_time = time.time()

    with ProcessPoolExecutor(max_workers=num_procs) as executor:
        results = list(executor.map(read_score_file_with_path, score_files))

    # Add to dict sequentially
    for file_path, data in results:
        if not isinstance(data, Exception):
            rel_path = os.path.relpath(file_path, base_dir)
            path_parts = rel_path.split(os.sep)
            add_to_dict_structure(root_dict, path_parts, data)
        else:
            print(f"Failed to read file {file_path}: {data}")

    elapsed = time.time() - start_time
    return root_dict, elapsed

def main():
    parser = argparse.ArgumentParser(description="Benchmark score reading with hierarchy and threading.")
    parser.add_argument("directory", type=str, help="Root directory for score files.")
    parser.add_argument("--limit", type=int, default=1000, help="Number of files to benchmark.")
    parser.add_argument("--threads", type=int, default=8, help="Number of threads for parallel reading.")
    parser.add_argument("--sequential", action="store_true", help="Also run sequentially for comparison.")
    parser.add_argument("--multiprocess", action="store_true", help="Also run with multiprocessing for comparison.")

    args = parser.parse_args()

    root_dir = args.directory
    if not os.path.isdir(root_dir):
        # Check if it exists relative to 'data/'
        alt_path = os.path.join("data", root_dir)
        if os.path.isdir(alt_path):
            root_dir = alt_path
        else:
            print(f"Error: {root_dir} is not a directory.")
            exit(1)

    score_files = find_score_files(root_dir)
    if not score_files:
        print("No score files found.")
        return

    if args.limit and args.limit < len(score_files):
        print(f"Limiting to first {args.limit} files.")
        score_files = score_files[:args.limit]

    initial_mem = get_memory_usage()
    print(f"Initial Memory Usage: {initial_mem:.2f} MB")

    if args.sequential:
        print("\n--- Sequential Run ---")
        _, seq_time = run_sequential(score_files, root_dir)
        seq_mem = get_memory_usage()
        print(f"Sequential Time: {seq_time:.2f} seconds")
        print(f"Memory Usage After: {seq_mem:.2f} MB")
        print(f"Memory Increment: {seq_mem - initial_mem:.2f} MB")

        # Reset memory for threaded test (as much as possible)
        # However, GC might not reclaim it immediately. Let's just track increment.
        initial_mem_threaded = seq_mem
    else:
        initial_mem_threaded = initial_mem

    print("\n--- Threaded Run ---")
    _, thr_time = run_threaded(score_files, root_dir, args.threads)
    thr_mem = get_memory_usage()
    print(f"Threaded Time: {thr_time:.2f} seconds")
    print(f"Memory Usage After: {thr_mem:.2f} MB")
    print(f"Memory Increment (from start of threaded): {thr_mem - initial_mem_threaded:.2f} MB")

    if args.sequential:
        speedup = seq_time / thr_time
        print(f"\nSpeedup (Threads vs Seq): {speedup:.2f}x")

    if args.multiprocess:
        print("\n--- Multiprocess Run ---")
        initial_mem_mp = get_memory_usage()
        _, mp_time = run_multiprocess(score_files, root_dir, args.threads)
        mp_mem = get_memory_usage()
        print(f"Multiprocess Time: {mp_time:.2f} seconds")
        print(f"Memory Usage After: {mp_mem:.2f} MB")
        print(f"Memory Increment: {mp_mem - initial_mem_mp:.2f} MB")

        if args.sequential:
            speedup_mp = seq_time / mp_time
            print(f"Speedup (MP vs Seq): {speedup_mp:.2f}x")

if __name__ == "__main__":
    main()
