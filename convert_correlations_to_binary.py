import pandas as pd
import argparse
from pathlib import Path

def convert_csv_to_parquet(csv_path: str, parquet_path: str):
    print(f"Reading {csv_path}...")
    # Read CSV, assuming first column is the index (ticker symbols)
    df = pd.read_csv(csv_path, index_col=0)

    print(f"Saving to {parquet_path}...")
    # Parquet preserves index and data types
    df.to_parquet(parquet_path)

    csv_size = Path(csv_path).stat().st_size
    parquet_size = Path(parquet_path).stat().st_size

    print(f"Done.")
    print(f"Original CSV size: {csv_size / 1024 / 1024:.2f} MB")
    print(f"Binary Parquet size: {parquet_size / 1024 / 1024:.2f} MB")
    print(f"Reduction: {(1 - parquet_size / csv_size) * 100:.1f}%")

def main():
    parser = argparse.ArgumentParser(description="Convert ticker correlations CSV to binary Parquet format.")
    parser.add_argument("--input", default="ticker_correlations.csv", help="Input CSV path")
    parser.add_argument("--output", default="ticker_correlations.parquet", help="Output Parquet path")

    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: {args.input} not found.")
        return

    convert_csv_to_parquet(args.input, args.output)

if __name__ == "__main__":
    main()
