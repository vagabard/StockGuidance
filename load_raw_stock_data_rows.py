"""
Load rows from MySQL table `raw_stock_data` and convert them into arrays/tensors.

We are primarily interested in the open and close values, along with the date_id.
The output mirrors the row-wise layout from load_database_rows_into_tensors.py, but
for a different table and feature set.

Outputs
- Keys array: shape (N, 2) with (date_id, ticker_id)
- Features array: shape (N, 2) with (open, close) as float64

Optional outputs with --to-torch: same data as torch tensors (int64 keys, float32 features).
You can also persist results via --save-npz or --save-pt.
"""
from __future__ import annotations

import argparse
import os
from typing import List, Optional, Tuple

import numpy as np

try:
    import torch  # type: ignore
    TORCH_AVAILABLE = True
except Exception:
    torch = None  # type: ignore
    TORCH_AVAILABLE = False

from db_connect import get_connection
from data_loading_shared import to_torch


SELECT_SQL = (
    """
SELECT
    rsd.ticker_id,
    rsd.date_id,
    rsd.open,
    rsd.close
FROM raw_stock_data rsd
ORDER BY rsd.date_id ASC, rsd.ticker_id ASC
    """
)


def fetch_as_numpy(
    host: str = "mysql95",
    database: str = "stockforecasting",
    user: Optional[str] = "Redbeard",
    password: Optional[str] = "3YZs#z5GOJKdea",
    port: int = 3306,
    driver: str = "auto",
    limit: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Fetch rows from raw_stock_data and return (keys, features) as NumPy arrays.

    keys: shape (N, 2) -> (date_id, ticker_id)
    features: shape (N, 2) -> (open, close)

    Results are cached on disk under a ``cache`` subdirectory next to this
    module. On subsequent calls with the same ``limit`` value, the cached
    NumPy file is loaded instead of querying the database again. Caching is
    a best-effort optimisation: any errors while reading or writing the cache
    are silently ignored and the database is queried as a fallback.
    """

    # Build cache path in a dedicated "cache" subdirectory. We include the
    # ``limit`` value (when provided) in the filename so that partial-debug
    # loads do not collide with full-dataset caches.
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    suffix = f"_limit{int(limit)}" if limit is not None and int(limit) > 0 else ""
    cache_name = f"raw_stock_data{suffix}.npz"
    cache_file = os.path.join(cache_dir, cache_name)

    try:
        if os.path.exists(cache_file):
            loaded = np.load(cache_file)
            return loaded["keys"], loaded["features"]
    except Exception:
        # If anything goes wrong while loading the cache, fall back to DB.
        pass

    conn = get_connection(host=host, database=database, user=user, password=password, port=port, driver=driver)
    try:
        cur = conn.cursor()
        sql = SELECT_SQL
        if limit is not None and limit > 0:
            sql = f"{sql}\nLIMIT {int(limit)}"
        cur.execute(sql)

        date_ids: List[int] = []
        ticker_ids: List[int] = []
        feats: List[List[float]] = []

        for row in cur.fetchall():
            # row order: ticker_id, date_id, open, close
            tkr_id = int(row[0])
            dt_id = int(row[1])
            open_v = np.nan if row[2] is None else float(row[2])
            close_v = np.nan if row[3] is None else float(row[3])
            date_ids.append(dt_id)
            ticker_ids.append(tkr_id)
            feats.append([open_v, close_v])

        keys = (
            np.stack(
                [np.array(date_ids, dtype=np.int32), np.array(ticker_ids, dtype=np.int32)],
                axis=1,
            )
            if date_ids
            else np.empty((0, 2), dtype=np.int32)
        )
        features = np.asarray(feats, dtype=np.float64) if feats else np.empty((0, 2), dtype=np.float64)

        # Save to cache file for future invocations. Ignore any failures.
        try:
            os.makedirs(cache_dir, exist_ok=True)
            np.savez(cache_file, keys=keys, features=features)
        except Exception:
            pass

        return keys, features
    finally:
        try:
            conn.close()
        except Exception:
            pass


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description="Load raw_stock_data rows (open, close) into arrays/tensors.")
    p.add_argument("--host", default=os.getenv("MYSQL_HOST", "localhost"))
    p.add_argument("--database", "--db", dest="database", default=os.getenv("MYSQL_DB", "stockforecasting"))
    p.add_argument("--user", default=os.getenv("MYSQL_USER", "Redbeard"))
    p.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"))
    p.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    p.add_argument("--driver", default="auto", choices=["auto", "mysqlconnector", "pymysql"], help="DB driver to use")
    p.add_argument("--limit", type=int, default=None, help="Optional LIMIT for debugging")

    p.add_argument("--to-torch", action="store_true", help="Also convert outputs to torch.Tensor (if torch available)")
    p.add_argument("--save-npz", default=None, help="Path to save NumPy arrays as .npz (keys, features)")
    p.add_argument("--save-pt", default=None, help="Path to save PyTorch tensors as .pt (requires --to-torch)")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)

    keys_np, feats_np = fetch_as_numpy(
        host=args.host,
        database=args.database,
        user=args.user,
        password=args.password,
        port=args.port,
        driver=args.driver,
        limit=args.limit,
    )

    print(f"Loaded rows: {len(keys_np)}")
    if len(keys_np) > 0:
        print(f"Keys (date_id, ticker_id) shape: {keys_np.shape}, dtype: {keys_np.dtype}")
        print(f"Features (open, close) shape: {feats_np.shape}, dtype: {feats_np.dtype}")
        print("First row sample:")
        first = {
            "date_id": int(keys_np[0, 0]),
            "ticker_id": int(keys_np[0, 1]),
            "open": None if np.isnan(feats_np[0, 0]) else float(feats_np[0, 0]),
            "close": None if np.isnan(feats_np[0, 1]) else float(feats_np[0, 1]),
        }
        print(first)

    if args.save_npz:
        np.savez(args.save_npz, keys=keys_np, features=feats_np)
        print(f"Saved NumPy arrays to {args.save_npz}")

    if args.to_torch:
        if not TORCH_AVAILABLE:
            raise SystemExit("--to-torch specified but PyTorch is not installed. Install torch or omit the flag.")
        keys_t, feats_t = to_torch(keys_np, feats_np)
        print(
            f"Torch tensors: keys {tuple(keys_t.shape)} {keys_t.dtype}, features {tuple(feats_t.shape)} {feats_t.dtype}"
        )
        if args.save_pt:
            torch.save({"keys": keys_t, "features": feats_t}, args.save_pt)
            print(f"Saved PyTorch tensors to {args.save_pt}")


if __name__ == "__main__":
    main()
