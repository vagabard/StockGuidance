"""
Load rows from MySQL table `daily_delta_stock_data` and convert them into tensors/arrays.

Connection is created via `db_connect.get_connection` (same defaults as the rest of the repo):
- host: mysql95
- database: stockforecasting
- port: 3306
- credentials: env vars MYSQL_USER / MYSQL_PASSWORD if not provided

Table structure expected (columns and their order in the SELECT below):
  ticker_id INT PK
  date_id INT PK
  open_ratio_to_prior DOUBLE
  high_ratio_to_prior DOUBLE
  low_ratio_to_prior DOUBLE
  close_ratio_to_prior DOUBLE
  volume_ratio_to_prior DOUBLE
  vwap_ratio_to_prior DOUBLE
  transactions_ratio_to_prior DOUBLE

Rows are ordered by date_id, then ticker_id.

Outputs
  - Keys array/tensor: shape (N, 2) with (date_id, ticker_id)
  - Features array/tensor: shape (N, 7) with the ratio columns
  - Labels array/tensor: shape (N,) with `next_day_change_in_price`

By default, outputs are NumPy arrays. If PyTorch is installed and you pass
`--to-torch`, the script will also produce torch.Tensors.

You can optionally persist outputs using `--save-npz` (NumPy .npz) or `--save-pt`
for PyTorch `.pt` files.

Usage examples:
  python load_database_rows_into_tensors.py --user scott --password tiger --host localhost \
	  --save-npz data/daily_delta_tensors.npz

  python load_database_rows_into_tensors.py --to-torch --save-pt data/daily_delta_tensors.pt
"""
from __future__ import annotations

import argparse
import os
from typing import Optional, Tuple, List, Dict, Union
import time

import numpy as np

try:
	import torch  # type: ignore
	TORCH_AVAILABLE = True
except Exception:
	torch = None  # type: ignore
	TORCH_AVAILABLE = False

from db_connect import get_connection
from data_loading_shared import to_torch


# Streaming helper to avoid loading millions o rows with fetchall()
def _iter_rows(cursor, batch_size: int = 10000):
	"""Yield rows from a DB-API cursor in batches using fetchmany.

	This prevents the process from hanging or running out of memory when the
	query returns millions of rows. Works with common MySQL drivers
	(mysql-connector-python, PyMySQL) that implement PEP 249.
	"""
	try:
		# Hint to the driver about preferred batch size if supported
		cursor.arraysize = int(batch_size)
	except Exception:
		pass

	while True:
		batch = cursor.fetchmany(batch_size)
		if not batch:
			break
		for r in batch:
			yield r


SELECT_SQL = (
	"""
SELECT
	ddsd.ticker_id,
	ddsd.date_id,
	ddsd.open_ratio_to_prior,
	ddsd.high_ratio_to_prior,
	ddsd.low_ratio_to_prior,
	ddsd.close_ratio_to_prior,
	ddsd.volume_ratio_to_prior,
	ddsd.vwap_ratio_to_prior,
	ddsd.transactions_ratio_to_prior,
	ddsd.next_day_change_in_price
FROM daily_delta_stock_data ddsd
ORDER BY ddsd.date_id ASC, ddsd.ticker_id ASC	"""
)
# LEAD(ddsd.change_in_price_over_day, 1) OVER (
#     PARTITION BY ddsd.ticker_id
#     ORDER BY ddsd.date_id
# ) AS next_day_change_in_price

def SELECT_CLOSE_RATIO_ONLY_SQL(day_change_centered: str) -> str:
	"""Build SQL that selects only close ratio as the single feature plus the chosen label.

	day_change_centered:
	- "one"  -> use ddsd.next_day_change_in_price
	- "half" -> use ddsd.half_centered_next_day_price_change
	- "zero" -> use ddsd.zero_centered_next_day_price_change
	Any other value falls back to "one" behavior.
	"""
	if day_change_centered == "half":
		label_col = "ddsd.half_centered_next_day_price_change"
	elif day_change_centered == "zero":
		label_col = "ddsd.zero_centered_next_day_price_change"
	else:
		label_col = "ddsd.next_day_change_in_price"

	return f"""
SELECT
	ddsd.ticker_id,
	ddsd.date_id,
	ddsd.close_ratio_to_prior,
	{label_col}
FROM daily_delta_stock_data ddsd
ORDER BY ddsd.date_id ASC, ddsd.ticker_id ASC
"""


# LEAD(ddsd.change_in_price_over_day, 1) OVER (
#     PARTITION BY ddsd.ticker_id
#     ORDER BY ddsd.date_id
# ) AS next_day_change_in_price


def fetch_as_numpy(
	host: str = "mysql95",
	database: str = "stockforecasting",
	user: Optional[str] = "Redbeard",
	password: Optional[str] = "3YZs#z5GOJKdea",
	port: int = 3306,
	driver: str = "auto",
	limit: Optional[int] = None,
	close_ratio_only: bool = False,
	day_change_centered:  str = "one",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
	"""Fetch table rows and return (keys, features, labels) as NumPy arrays.

	keys: shape (N, 2) -> (date_id, ticker_id)
	features: shape (N, F) where F=7 for full query, or F=1 when --close-only is used
	  Full order: open/high/low/close/volume/vwap/transactions
	labels: shape (N,) -> next_day_change_in_price (may contain NaN for last date per ticker)
	"""
	conn = get_connection(host=host, database=database, user=user, password=password, port=port, driver=driver)
	try:
		cur = conn.cursor()
		sql = SELECT_CLOSE_RATIO_ONLY_SQL(day_change_centered) if close_ratio_only else SELECT_SQL
		if limit is not None and limit > 0:
			sql = f"{sql}\nLIMIT {int(limit)}"
		cur.execute(sql)

		date_ids: List[int] = []
		ticker_ids: List[int] = []
		feats: List[List[float]] = []
		labels: List[float] = []
		feature_count: Optional[int] = None
		# Stream rows to avoid loading everything into memory at once
		for row in _iter_rows(cur):
			# row order: ticker_id, date_id, 7 feature columns, label (next_day_change_in_price)
			tkr_id = int(row[0])
			dt_id = int(row[1])
			# features are columns 2..-2 inclusive, label is last column
			fvals = row[2:-1]
			label_val = row[-1]
			# convert to float64, preserving None as np.nan
			fv = [np.nan if v is None else float(v) for v in fvals]
			lb = np.nan if label_val is None else float(label_val)
			date_ids.append(dt_id)
			ticker_ids.append(tkr_id)
			feats.append(fv)
			labels.append(lb)
			if feature_count is None:
				feature_count = len(fv)

		keys = np.stack([np.array(date_ids, dtype=np.int32), np.array(ticker_ids, dtype=np.int32)], axis=1)
		F = int(feature_count or (1 if close_ratio_only else 7))
		features = np.asarray(feats, dtype=np.float64) if feats else np.empty((0, F), dtype=np.float64)
		labels_np = np.asarray(labels, dtype=np.float64) if labels else np.empty((0,), dtype=np.float64)
		return keys, features, labels_np
	finally:
		try:
			conn.close()
		except Exception:
			pass


def fetch_grouped_by_date(
	host: str = "mysql95",
	database: str = "stockforecasting",
	user: Optional[str] = "Redbeard",
	password: Optional[str] = "3YZs#z5GOJKdea",
	port: int = 3306,
	driver: str = "pymysql",
	limit: Optional[int] = None,
	return_2d: bool = False,
	 close_ratio_only: bool = False,
	end_percent_to_fetch: Optional[float] = None,
	day_change_centered: str = "one",

) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
	"""Fetch rows and pack features into a fixed layout per date.

	Output layout requirement:
	- For each trading date, produce a NumPy array that contains all tickers in a
	  fixed position; the slot for a ticker is (ticker_id - 1) * num_features.
	- Missing tickers for a date are filled with NaNs.

	Returns a tuple (dates, X, Y) where:
	- dates: shape (D,) int32 with the ordered list of date_id values.
	- X: if return_2d is False -> shape (D, T*F) float64 (flattened per-date); otherwise -> shape (D, T, F).
	- Y: labels per (date, ticker): shape (D, T) with next_day_change_in_price values.

	Notes:
	- T is determined as max ticker_id observed in the fetched rows. If ticker_id values are dense
	  starting from 1 (typical auto-increment IDs), the slots align perfectly to (ticker_id-1).
	- F (features per ticker) is 7 for the full query, or 1 when close_ratio_only=True.

	If end_percent_to_fetch is provided (e.g., 10.0), only the last that percent of
	available date_id entries are kept, effectively selecting the most recent dates
	(e.g., 10% keeps the last 10% of dates and drops the earlier 90%).
	"""
	print("Fetching rows from database... user= ", user, "")
	conn = get_connection(host=host, database=database, user=user, password=password, port=port, driver=driver)
	try:
		cur = conn.cursor()
		sql = SELECT_CLOSE_RATIO_ONLY_SQL(day_change_centered) if close_ratio_only else SELECT_SQL
		if limit is not None and limit > 0:
			sql = f"{sql}\nLIMIT {int(limit)}"
		cur.execute(sql)

		# Collect rows grouped by date while tracking max ticker_id
		dates_list: List[int] = []
		date_to_index: Dict[int, int] = {}
		rows_by_date: Dict[int, List[Tuple[int, List[float], float]]] = {}
		max_ticker_id = 0

		# Stream rows batch-by-batch to prevent hanging/memory pressure
		for row in _iter_rows(cur):
			ticker_id = int(row[0])
			date_id = int(row[1])
			# split features (7) and label (last)
			stock_feature_values = row[2:-1]
			label_val = row[-1]
			stock_data_fields_safe = [np.nan if v is None else float(v) for v in stock_feature_values]
			label_safe = np.nan if label_val is None else float(label_val)

			if date_id not in date_to_index:
				#first time encountering this date_id
				date_to_index[date_id] = len(dates_list)
				dates_list.append(date_id)
				rows_by_date[date_id] = []
			rows_by_date[date_id].append((ticker_id, stock_data_fields_safe, label_safe))
			if ticker_id > max_ticker_id:
				max_ticker_id = ticker_id

		# Optionally keep only the last P% of dates
		if end_percent_to_fetch is not None:
			try:
				pct = float(end_percent_to_fetch)
			except Exception:
				pct = 100.0
			if pct <= 0:
				# Nothing sensible to return; fall back to 1 date if any exist
				keep = 0
			else:
				if pct > 100.0:
					pct = 100.0
				# compute number of dates to retain (at least 1 if any dates exist)
				keep = int(np.ceil(len(dates_list) * (pct / 100.0)))
			# Slice to the last 'keep' dates
			if keep > 0 and len(dates_list) > 0:
				dates_list = dates_list[-max(1, keep):]

		Number_Of_dates_found = len(dates_list)
		# feature width depends on selected SQL (7 for full, 1 for close-only)
		Number_of_fields_per_ticker_symbol = 1 if close_ratio_only else 7

		# Determine T (max ticker slot) based only on the retained dates, to keep
		# matrix sizes consistent with the selected window.
		if Number_Of_dates_found > 0:
			local_max_ticker = 0
			for dt in dates_list:
				for ticker_id, _stock_data_fields_safe, _label_safe in rows_by_date.get(dt, []):
					if ticker_id > local_max_ticker:
						local_max_ticker = ticker_id
			T = local_max_ticker
		else:
			T = 0

		dates = np.array(dates_list, dtype=np.int32)
		if return_2d:
			X = np.full((Number_Of_dates_found, T, Number_of_fields_per_ticker_symbol), np.nan, dtype=np.float64)
			Y = np.full((Number_Of_dates_found, T), np.nan, dtype=np.float64)
			for d_idx, dt in enumerate(dates_list):
				for ticker_id, stock_data_fields_safe, label_safe in rows_by_date.get(dt, []):
					t_idx = ticker_id - 1
					if 0 <= t_idx < T:
						X[d_idx, t_idx, :] = stock_data_fields_safe
						Y[d_idx, t_idx] = label_safe
			return dates, X, Y
		else:
			X = np.full((Number_Of_dates_found, T * Number_of_fields_per_ticker_symbol), np.nan, dtype=np.float64)
			Y = np.full((Number_Of_dates_found, T), np.nan, dtype=np.float64)
			for d_idx, dt in enumerate(dates_list):
				for ticker_id, stock_data_fields_safe, label_safe in rows_by_date.get(dt, []):
					base = (ticker_id - 1) * Number_of_fields_per_ticker_symbol
					if 0 <= base < T * Number_of_fields_per_ticker_symbol:
						X[d_idx, base:base + Number_of_fields_per_ticker_symbol] = stock_data_fields_safe
						Y[d_idx, ticker_id - 1] = label_safe
			return dates, X, Y
	finally:
		try:
			conn.close()
		except Exception:
			pass


def to_torch(keys_np: np.ndarray, features_np: np.ndarray, labels_np: Optional[np.ndarray] = None):
	# Backward-compat wrapper to the shared implementation. Kept to preserve CLI behavior.
	from data_loading_shared import to_torch as _to_torch
	return _to_torch(keys_np, features_np, labels_np)


def parse_args(argv: Optional[list[str]] = None):
	p = argparse.ArgumentParser(description="Load daily_delta_stock_data into tensors/arrays.")
	p.add_argument("--host", default=os.getenv("MYSQL_HOST", "mysql95"))
	p.add_argument("--database", "--db", dest="database", default=os.getenv("MYSQL_DB", "stockforecasting"))
	p.add_argument("--user", default=os.getenv("MYSQL_USER"))
	p.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", ""))
	p.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
	p.add_argument("--driver", default="pymysql", choices=["auto", "mysqlconnector", "pymysql", "jdbc"], help="DB driver to use")
	p.add_argument("--limit", type=int, default=None, help="Optional LIMIT for debugging")
	p.add_argument("--day-change-centered", default="one", choices=["one", "half", "zero"], help="The centroid of ratio of next_day_change_in_price")

	# New: option to load only close_ratio_to_prior as the feature (instead of all 7 ratios)
	p.add_argument(
		"--close-only",
		action="store_true",
		help="If set, use SELECT_CLOSE_RATIO_ONLY_SQL to fetch only close_ratio_to_prior as the feature (F=1)",
	)

	p.add_argument(
		"--layout",
		default="per-date-flat",
		choices=["rows", "per-date-flat", "per-date-2d"],
		help=(
			"Output layout: 'rows' -> (keys, features, labels) row-wise; "
			"'per-date-flat' -> (dates, X, Y) with X shape (D, T*F) and Y shape (D, T); "
			"'per-date-2d' -> (dates, X, Y) with X shape (D, T, F) and Y shape (D, T)."
		),
	)

	p.add_argument("--to-torch", action="store_true", help="Also convert outputs to torch.Tensor (if torch available)")
	p.add_argument(
		"--save-npz",
		default=None,
		help=(
			"Path to save NumPy arrays as .npz. For layout=rows saves keys/features/labels; "
			"for per-date layouts saves dates/X/Y."
		),
	)
	p.add_argument("--save-pt", default=None, help="Path to save PyTorch tensors as .pt (requires --to-torch)")
	return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
	start_time = time.perf_counter()
	args = parse_args(argv)

	if args.layout == "rows":
		keys_np, feats_np, labels_np = fetch_as_numpy(
			host=args.host,
			database=args.database,
			user=args.user,
			password=args.password,
			port=args.port,
			driver=args.driver,
			limit=args.limit,
			close_ratio_only=args.close_only,
			day_change_centered=args.day_change_centered,
		)

		print(f"Loaded rows: {len(keys_np)}")
		if len(keys_np) > 0:
			print(f"Keys (date_id, ticker_id) shape: {keys_np.shape}, dtype: {keys_np.dtype}")
			print(f"Features shape: {feats_np.shape}, dtype: {feats_np.dtype}")
			print(f"Labels shape: {labels_np.shape}, dtype: {labels_np.dtype}")
			print("First row sample:")
			print({
				"date_id": int(keys_np[0, 0]),
				"ticker_id": int(keys_np[0, 1]),
				"features": feats_np[0].tolist(),
				"label": None if np.isnan(labels_np[0]) else float(labels_np[0]),
			})

		if args.save_npz:
			np.savez(args.save_npz, keys=keys_np, features=feats_np, labels=labels_np)
			print(f"Saved NumPy arrays to {args.save_npz}")

		if args.to_torch:
			if not TORCH_AVAILABLE:
				raise SystemExit("--to-torch specified but PyTorch is not installed. Install torch or omit the flag.")
			keys_t, feats_t, labels_t = to_torch(keys_np, feats_np, labels_np)
			print(
				f"Torch tensors: keys {tuple(keys_t.shape)} {keys_t.dtype}, features {tuple(feats_t.shape)} {feats_t.dtype}, labels {tuple(labels_t.shape)} {labels_t.dtype}"
			)
			if args.save_pt:
				torch.save({"keys": keys_t, "features": feats_t, "labels": labels_t}, args.save_pt)
				print(f"Saved PyTorch tensors to {args.save_pt}")
	else:
		return_2d = args.layout == "per-date-2d"
		print(f"user arg: {args}")
		dates_np, X_np, Y_np = fetch_grouped_by_date(
			host=args.host,
			database=args.database,
			user=args.user,
			password=args.password,
			port=args.port,
			driver=args.driver,
			limit=args.limit,
			return_2d=return_2d,
			close_ratio_only=args.close_only,
		)

		layout_desc = "(D, T, F)" if return_2d else "(D, T*F)"
		print(f"Loaded dates: {len(dates_np)}; X shape: {X_np.shape} {layout_desc}, dtype: {X_np.dtype}; Y shape: {Y_np.shape}")
		if len(dates_np) > 0:
			d0 = int(dates_np[0])
			if return_2d:
				# show first non-empty ticker slot if possible
				first_row = X_np[0]
				# find first non-nan row
				t_idx = int(np.nanargmax(~np.isnan(first_row).any(axis=1))) if first_row.size else 0
				sample = first_row[t_idx].tolist() if first_row.size else []
				label0 = None if np.isnan(Y_np[0, t_idx]) else float(Y_np[0, t_idx])
			else:
				# first ticker slot features (first 7 values)
				sample = X_np[0, :7].tolist() if X_np.shape[1] >= 7 else X_np[0].tolist()
				label0 = None if (Y_np.shape[1] == 0 or np.isnan(Y_np[0, 0])) else float(Y_np[0, 0])
			print({
				"date_id": d0,
				"first_ticker_slot_features": sample,
				"first_ticker_label": label0,
			})

		if args.save_npz:
			np.savez(args.save_npz, dates=dates_np, X=X_np, Y=Y_np)
			print(f"Saved NumPy arrays to {args.save_npz}")

		if args.to_torch:
			if not TORCH_AVAILABLE:
				raise SystemExit("--to-torch specified but PyTorch is not installed. Install torch or omit the flag.")
			dates_t = torch.from_numpy(dates_np.astype(np.int64, copy=False))
			X_t = torch.from_numpy(X_np.astype(np.float32, copy=False))
			Y_t = torch.from_numpy(Y_np.astype(np.float32, copy=False))
			print(f"Torch tensors: dates {tuple(dates_t.shape)} {dates_t.dtype}, X {tuple(X_t.shape)} {X_t.dtype}, Y {tuple(Y_t.shape)} {Y_t.dtype}")
			if args.save_pt:
				torch.save({"dates": dates_t, "X": X_t, "Y": Y_t}, args.save_pt)
				print(f"Saved PyTorch tensors to {args.save_pt}")

	elapsed = time.perf_counter() - start_time
	print(f"Total time: {elapsed:.2f} seconds")


if __name__ == "__main__":
	main()
