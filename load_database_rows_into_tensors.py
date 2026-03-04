"""
Load rows from MySQL table `daily_delta_stock_data` and convert them into tensors/arrays.

Connection is created via `db_connect.get_connection` (same defaults as the rest of the repo):
- host: localhost
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
`--to-torch_interface`, the script will also produce torch_interface.Tensors.

You can optionally persist outputs using `--save-npz` (NumPy .npz) or `--save-pt`
for PyTorch `.pt` files.

Usage examples:
  python load_database_rows_into_tensors.py --user scott --password tiger --host localhost \
	  --save-npz data/daily_delta_tensors.npz

  python load_database_rows_into_tensors.py --to-torch_interface --save-pt data/daily_delta_tensors.pt
"""
from __future__ import annotations

import argparse
import os
import time
import traceback
from typing import Optional, Tuple, List, Dict, Set, Any, Iterator

import numpy as np

try:
	import torch  # type: ignore
	TORCH_AVAILABLE = True
except Exception:
	torch = None  # type: ignore
	TORCH_AVAILABLE = False

from database.db_connect import get_connection
from trainer.label.transform import transform_labels


# Streaming helper to avoid loading millions o rows with fetchall()
def _iter_rows(cursor, batch_size: int = 10000)-> Iterator[Tuple|Dict[str, Any]]:
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



def SELECT_VARIABLE_DATA_SQL(day_change_centered: str, current_day_change = False, volume_ratio = False, vwap_ratio = False, tranaction_ratio = False, only_use_tickers_without_missing_dates = True, start_date_id:Optional[int] = -1, end_date_id:Optional[int] = -1, ticker_pool_start_date_id:Optional[int] = None, ticker_pool_end_date_id:Optional[int] = None) -> str:
	"""Build SQL that selects only close ratio as the single feature plus the chosen label.

	day_change_centered:
	- "one"  -> use ddsd.next_day_change_in_price
	- "half" -> use ddsd.half_centered_next_day_price_change
	- "zero" -> use ddsd.zero_centered_next_day_price_change
	Any other value falls back to "one" behavior.
	"""
	additional_fields_to_select = f"ddsd.next_day_change_in_price"

	if current_day_change:
		additional_fields_to_select = f"{additional_fields_to_select},ddsd.change_in_price_over_day"#close_ratio_to_prior
	if volume_ratio:
		additional_fields_to_select = f"{additional_fields_to_select},ddsd.volume_normalized"
	if vwap_ratio:
		additional_fields_to_select = f"{additional_fields_to_select},ddsd.vwap_ratio_to_prior"
	if tranaction_ratio:
		additional_fields_to_select = f"{additional_fields_to_select},ddsd.transactions_normalized"

	full_query = f"""
SELECT
	ddsd.ticker_id,
	ddsd.date_id,
	ddsd.change_in_price_over_day,
	{additional_fields_to_select}
FROM daily_delta_stock_data ddsd """
	if only_use_tickers_without_missing_dates:
		full_query += f"""INNER JOIN ticker_symbol ts ON ddsd.ticker_id = ts.id AND ts.missing_dates = FALSE """

	# Limit to tickers present on both start and end date
	ticker_pool_end = ticker_pool_end_date_id if ticker_pool_end_date_id is not None else end_date_id
	ticker_pool_start = ticker_pool_start_date_id if ticker_pool_start_date_id is not None else start_date_id
	full_query += f"""
	INNER JOIN (
	""" + create_ticker_id_selection_sql(only_use_tickers_without_missing_dates, ticker_pool_start, ticker_pool_end) + f"""
	) AS boundary_tickers ON ddsd.ticker_id = boundary_tickers.ticker_id """

	if start_date_id is not None or end_date_id is not None :
		where_clause = f"""WHERE """
		if start_date_id is not None:
			where_clause += f"""ddsd.date_id > {start_date_id} """
		if end_date_id is not None:
			if start_date_id is not None :
				where_clause += f"""AND """
			where_clause += f"""ddsd.date_id <= {end_date_id} """
		full_query += where_clause
	full_query += """ORDER BY ddsd.date_id ASC, ddsd.ticker_id ASC
"""
	return full_query

def create_ticker_id_selection_sql(only_use_tickers_without_missing_dates = True, start_date_id:Optional[int] = "", end_date_id:Optional[int] = "", only_use_tickers_with_common_stock_type:bool = True) -> str:

	# Limit to tickers present on both start and end date
	ticker_id_selection_query = f"""
	SELECT Distinct(t1.ticker_id)
	FROM daily_delta_stock_data t1
	JOIN ticker_symbol ts ON t1.ticker_id = ts.id
"""
	if start_date_id is not None or end_date_id is not None:
		ticker_id_selection_query += f""" JOIN daily_delta_stock_data t2 ON t1.ticker_id = t2.ticker_id
"""
		if start_date_id is None or (isinstance(start_date_id, int) and start_date_id < 0):
			start_date_string = f" (SELECT MIN(date_id) FROM daily_delta_stock_data) "
		else:
			start_date_string = f" (SELECT MAX(date_id) FROM daily_delta_stock_data WHERE date_id <= {start_date_id}) "
		if end_date_id is None or (isinstance(end_date_id, int) and end_date_id < 0):
			end_date_string = f" (SELECT MAX(date_id) FROM daily_delta_stock_data) "
		else:
			end_date_string = f" (SELECT MAX(date_id) FROM daily_delta_stock_data WHERE date_id <= {end_date_id}) "
		ticker_id_selection_query += f"""
		WHERE t1.date_id = {start_date_string} AND t2.date_id = {end_date_string}
	"""
	if only_use_tickers_without_missing_dates:
		ticker_id_selection_query += f"""AND ts.missing_dates = FALSE """
	if only_use_tickers_with_common_stock_type:
		ticker_id_selection_query += f"""AND ts.type = 'CS' """
	return ticker_id_selection_query


# LEAD(ddsd.change_in_price_over_day, 1) OVER (
#     PARTITION BY ddsd.ticker_id
#     ORDER BY ddsd.date_id
# ) AS next_day_change_in_price


# def fetch_as_numpy(
# 	host: str = "mysql95",
# 	database: str = "stockforecasting",
# 	user: Optional[str] = "Redbeard",
# 	password: Optional[str] = "3YZs#z5GOJKdea",
# 	port: int = 3306,
# 	driver: str = "auto",
# 	limit: Optional[int] = None,
# 	close_ratio_only: bool = False,
# 	day_change_centered:  str = "one",
# ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
# 	"""Fetch table rows and return (keys, features, labels) as NumPy arrays.
#
# 	keys: shape (N, 2) -> (date_id, ticker_id)
# 	features: shape (N, F) where F=7 for full query, or F=1 when --close-only is used
# 	  Full order: open/high/low/close/volume/vwap/transactions
# 	labels: shape (N,) -> next_day_change_in_price (may contain NaN for last date per ticker)
# 	"""
# 	# First, attempt to load from the cached NumPy file if it exists.
# 	# This avoids hitting the database when the data has already been fetched.
# 	cache_file = f"fetch_as_numpty_{day_change_centered}.npz"
# 	try:
# 		if os.path.exists(cache_file):
# 			loaded = np.load(cache_file)
# 			# Expect the same keys used when saving below.
# 			return (
# 				loaded["keys"],
# 				loaded["features"],
# 				loaded["labels"],
# 			)
# 	except Exception:
# 		# If anything goes wrong while loading the cache, fall back to DB.
# 		pass
#
# 	conn = get_connection(host=host, database=database, user=user, password=password, port=port, driver=driver)
# 	try:
# 		cur = conn.cursor()
# 		sql = SELECT_VARIABLE_DATA_SQL(day_change_centered) if close_ratio_only else SELECT_SQL
# 		if limit is not None and limit > 0:
# 			sql = f"{sql}\nLIMIT {int(limit)}"
# 		cur.execute(sql)
#
# 		date_ids: List[int] = []
# 		ticker_ids: List[int] = []
# 		feats: List[List[float]] = []
# 		labels: List[float] = []
# 		feature_count: Optional[int] = None
# 		# Stream rows to avoid loading everything into memory at once
# 		for row in _iter_rows(cur):
# 			# row order: ticker_id, date_id, 7 feature columns, label (next_day_change_in_price)
# 			tkr_id = int(row[0])
# 			dt_id = int(row[1])
# 			# features are columns 2..-2 inclusive, label is last column
# 			fvals = row[2:-1]
# 			label_val = row[-1]
# 			# convert to float64, preserving None as np.nan
# 			fv = [np.nan if v is None else float(v) for v in fvals]
# 			lb = np.nan if label_val is None else float(label_val)
# 			date_ids.append(dt_id)
# 			ticker_ids.append(tkr_id)
# 			feats.append(fv)
# 			labels.append(lb)
# 			if feature_count is None:
# 				feature_count = len(fv)
#
# 		keys = np.stack([np.array(date_ids, dtype=np.int32), np.array(ticker_ids, dtype=np.int32)], axis=1)
# 		F = int(feature_count or (1 if close_ratio_only else 7))
# 		features = np.asarray(feats, dtype=np.float64) if feats else np.empty((0, F), dtype=np.float64)
# 		# Legacy row-wise loader caching removed; kept here for reference only.
# 		# labels_np = np.asarray(labels, dtype=np.float64) if labels else np.empty((0,), dtype=np.float64)
# 		# try:
# 		# 	np.savez(cache_file, keys=keys, features=features, labels=labels_np)
# 		# except Exception:
# 		# 	pass


_DISK_GROUPED_CACHE_CLEARED_ONCE = False


def get_latest_date_id(*, dbinfo_file: str = "dbinfo.id") -> int:
	"""Return the maximum date_id available in the daily_delta_stock_data table."""
	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute("SELECT MAX(date_id) FROM daily_delta_stock_data")
		row = cur.fetchone()
		if row and row[0] is not None:
			return int(row[0])
		return -1
	finally:
		try:
			conn.close()
		except Exception:
			pass


def get_recent_date_id_window(
	max_days: int,
	*,
	dbinfo_file: str = "dbinfo.id",
) -> Optional[tuple[int, int]]:
	"""Return an inclusive (start_id, end_id) window for the most recent days.

	The window covers up to ``max_days`` distinct ``date_id`` values present in
	``daily_delta_stock_data``.
	"""
	if max_days <= 0:
		return None

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"""
			SELECT DISTINCT date_id
			FROM daily_delta_stock_data
			ORDER BY date_id DESC
			LIMIT %s
			""",
			(max_days,),
		)
		rows = cur.fetchall()
		if not rows:
			return None
		date_ids = [int(r[0]) for r in rows]
		return min(date_ids), max(date_ids)
	finally:
		try:
			conn.close()
		except Exception:
			pass


def fetch_grouped_by_date(
	*,
	dbinfo_file: str = "dbinfo.id",
	limit: Optional[int] = None,
	return_2d: bool = False,
	current_day_change: bool = False,
	volume_ratio: bool = False,
	vwap_ratio: bool = False,
	tranaction_ratio: bool = False,
	day_change_centered: str = "one",
	start_date_id: Optional[int] = None,
	end_date_id: Optional[int] = None,
	ticker_pool_start_date_id: Optional[int] = None,
	ticker_pool_end_date_id: Optional[int] = None,
	primary_feature_field: str = "change_in_price_over_day",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[int]]:
	"""Fetch rows and pack features into a fixed layout per date.

	Output layout requirement:
	- For each trading date, produce a NumPy array that contains all tickers in a
	  fixed position; the slot for a ticker is (ticker_id - 1) * num_features.
	- Missing tickers for a date are filled with NaNs.

	Returns a tuple (dates, X, Y, ticker_ids) where:
	- dates: shape (D,) int32 with the ordered list of date_id values.
	- X: if return_2d is False -> shape (D, T*F) float64 (flattened per-date);
	  otherwise -> shape (D, T, F).
	- Y: labels per (date, ticker): shape (D, T) with next_day_change_in_price values.
	- ticker_ids: list of ticker_id values in the order they appear in the tensors.

	If end_percent_to_fetch is provided (e.g., 10.0), only the last that percent of
	available date_id entries are kept, effectively selecting the most recent dates.
	"""

	global _DISK_GROUPED_CACHE_CLEARED_ONCE

	print(f"date id information start_date_id={start_date_id} end_date_id={end_date_id} ticker_pool_start_date_id={ticker_pool_start_date_id} ticker_pool_end_date_id={ticker_pool_end_date_id}")

	# Build cache paths based on configuration so different feature sets keep
	# separate cache files.
	cache_base = (
		f"group_by_date_{day_change_centered}_"
		f"{'current_day_change' if current_day_change else ''}_"
		f"{'vol_ratio' if volume_ratio else ''}_"
		f"{'vwap' if vwap_ratio else ''}_"
		f"{'transaction' if tranaction_ratio else ''}"
	)
	if start_date_id is not None:
		cache_base += f"_start_{start_date_id}"
	if end_date_id is not None:
		cache_base += f"_end_{end_date_id}"
	if ticker_pool_start_date_id is not None:
		cache_base += f"_pool_start_{ticker_pool_start_date_id}"
	if ticker_pool_end_date_id is not None:
		cache_base += f"_pool_end_{ticker_pool_end_date_id}"
	cache_base+=f".npz"
	cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
	cache_file = os.path.join(cache_dir, cache_base)
	legacy_cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_base)

	# On the first call in this Python process, delete any existing cache file so
	# that this run always recomputes fresh data once before reusing the disk cache.
	if not _DISK_GROUPED_CACHE_CLEARED_ONCE:
		for path in (cache_file, legacy_cache_file):
			try:
				if os.path.exists(path):
					os.remove(path)
			except Exception:
				# Best-effort: ignore failures deleting stale cache.
				pass
		_DISK_GROUPED_CACHE_CLEARED_ONCE = True

	# Try to load from disk cache if it exists after the initial invalidation.
	try:
		load_path: Optional[str] = None
		if os.path.exists(cache_file):
			load_path = cache_file
		elif os.path.exists(legacy_cache_file):
			# Backward-compat: still allow loading old cache files from the
			# project root if they exist, but all new caches are written under
			# the "cache" subdirectory.
			load_path = legacy_cache_file
		if load_path is not None:
			loaded = np.load(load_path)
			return (
				loaded["dates"],
				loaded["X"],
				loaded["Y"],
				loaded["ticker_ids"],
			)
	except Exception as e:
		traceback.print_exception(e)
		# If anything goes wrong while loading the cache, fall back to DB.
		pass


	print("Fetching rows from database using dbinfo file:", dbinfo_file)
	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor(dictionary=True)
		sql = SELECT_VARIABLE_DATA_SQL(
			day_change_centered,
			current_day_change,
			volume_ratio,
			vwap_ratio,
			tranaction_ratio,
			True,
			start_date_id,
			end_date_id,
			ticker_pool_start_date_id=ticker_pool_start_date_id,
			ticker_pool_end_date_id=ticker_pool_end_date_id,
		)
		if limit is not None and limit > 0:
			sql = f"{sql}\nLIMIT {int(limit)}"
		cur.execute(sql)

		# Collect rows grouped by date while tracking max ticker_id
		stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]] = {}
		tickers_found: Set[int] = set()

		# Stream rows batch-by-batch to prevent hanging/memory pressure
		max_label_overall = -np.inf
		min_label_overall = np.inf
		for row in _iter_rows(cur):
			# ddsd.ticker_id,
			# ddsd.date_id,
			# ddsd.change_in_price_over_day,

			ticker_id = int(row['ticker_id'])
			tickers_found.add(ticker_id)
			date_id = int(row['date_id'])
			# split features and label (last)
			primary_feature_value = row[primary_feature_field]
			label_val = row['next_day_change_in_price']
			stock_data_fields_safe = [
				np.nan if primary_feature_value is None else float(primary_feature_value)
			] + [
				np.nan if row[k] is None else float(row[k])
				for k in row.keys()
				if k not in ['ticker_id', 'date_id', primary_feature_field, 'next_day_change_in_price']
			]
			label_safe = np.nan if label_val is None else float(label_val)

			if date_id not in stock_ids_and_features_and_label_by_date:
				# first time encountering this date_id
				stock_ids_and_features_and_label_by_date[date_id] = []
			if label_safe is not None and not np.isnan(label_safe):
				if label_safe > max_label_overall: max_label_overall = label_safe
				if label_safe < min_label_overall: min_label_overall = label_safe

			stock_ids_and_features_and_label_by_date[date_id].append((ticker_id, stock_data_fields_safe, label_safe))

		stock_ids_and_features_and_label_by_date = transform_labels(day_change_centered, stock_ids_and_features_and_label_by_date, min_value_found=min_label_overall, max_value_found=max_label_overall)

		number_of_tickers = len(tickers_found)



		Number_Of_dates_found = len(stock_ids_and_features_and_label_by_date.keys())
		# feature width depends on selected SQL (add up the features)
		Number_of_fields_per_ticker_symbol = (
			1
			+ int(bool(current_day_change))
			+ int(bool(volume_ratio))
			+ int(bool(vwap_ratio))
			+ int(bool(tranaction_ratio))
		)

		print(f"Fetched {Number_Of_dates_found} rows, {number_of_tickers} tickers.")
		# Determine T (max ticker slot) based only on the retained dates, to keep
		# matrix sizes consistent with the selected window.

		dates_list = sorted(stock_ids_and_features_and_label_by_date.keys())
		#dates = np.array(dates_list, dtype=np.int32)
		if return_2d:
			X = np.full(
				(Number_Of_dates_found, number_of_tickers, Number_of_fields_per_ticker_symbol),
				np.nan,
				dtype=np.float64,
			)
			Y = np.full((Number_Of_dates_found, number_of_tickers), np.nan, dtype=np.float64)
			for d_idx, dt in enumerate(dates_list):
				for ticker_index,(
					ticker_id,
					stock_data_fields_safe,
					label_safe,
				) in enumerate(stock_ids_and_features_and_label_by_date.get(dt, [])):
					t_idx = ticker_id - 1
					if 0 <= ticker_index < number_of_tickers:
						print(f"included ticker_index={ticker_index} ticker_id={ticker_id} t_idx={t_idx}")
						X[d_idx, ticker_index, :] = stock_data_fields_safe
						Y[d_idx, ticker_index] = label_safe
					# else:
					# 	print(f"skipped ticker_index={ticker_index} ticker_id={ticker_id} t_idx={t_idx}")
		else:
			X = np.full(
				(Number_Of_dates_found, number_of_tickers * Number_of_fields_per_ticker_symbol),
				np.nan,
				dtype=np.float64,
			)
			Y = np.full((Number_Of_dates_found, number_of_tickers), np.nan, dtype=np.float64)
			counted_tickers = False
			ticker_count = 0
			for d_idx, dt in enumerate(dates_list):
				for ticker_index,(
					ticker_id,
					stock_data_fields_safe,
					label_safe,
				) in enumerate(stock_ids_and_features_and_label_by_date.get(dt, [])):
					base = (ticker_index) * Number_of_fields_per_ticker_symbol
					if 0 <= base < number_of_tickers * Number_of_fields_per_ticker_symbol:
						X[d_idx, base : base + Number_of_fields_per_ticker_symbol] = (
							stock_data_fields_safe
						)
						Y[d_idx, ticker_index] = label_safe
					else:
						if not counted_tickers:
							ticker_count += 1
							print(f"skipped count {ticker_count} ticker_id={ticker_id} base={base}")
				counted_tickers = True
		if day_change_centered == "stock_change_ratio_ranked":
			# For each date (row in Y), rank the non-NaN values and normalize to 0-1
			# smallest/most negative change -> 0, largest positive change -> 1
			for d_idx in range(Y.shape[0]):
				row = Y[d_idx]
				mask = ~np.isnan(row)
				if np.any(mask):
					valid_values = row[mask]
					# scipy.stats.rankdata would be nice but let's stick to numpy for fewer dependencies
					# numpy.argsort gives indices that would sort the array.
					# argsort of argsort gives the rank.
					ranks = np.argsort(np.argsort(valid_values)).astype(np.float64)
					if len(ranks) > 1:
						normalized_ranks = ranks / (len(ranks) - 1)
					else:
						normalized_ranks = np.array([0.5]) # or 0 or 1, if only one stock
					Y[d_idx, mask] = normalized_ranks

		# Save to cache file for future processes.
		ticker_ids_list = sorted(list(tickers_found))
		try:
			os.makedirs(cache_dir, exist_ok=True)
			np.savez(cache_file, dates=dates_list, X=X, Y=Y, ticker_ids=ticker_ids_list)
		except Exception:
			# Caching is a best-effort optimization; ignore failures.
			pass

		dates_nd_array = np.array(dates_list, dtype=np.int32)
		return dates_nd_array, X, Y, ticker_ids_list
	except Exception as e:
		traceback.print_exception(e)
		raise
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
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	p.add_argument("--limit", type=int, default=None, help="Optional LIMIT for debugging")
	p.add_argument("--day-change-centered", default="one", choices=["one", "half", "zero", "stock_change_ratio_ranked"], help="The centroid of ratio of next_day_change_in_price")
	p.add_argument("--current-day-change", action="store_true", help="Include current_day_change_in_price")
	p.add_argument("--volume-ratio", action="store_true", help="Include volume_ratio")
	p.add_argument("--vwap-ratio", action="store_true", help="Include vwap_ratio")
	p.add_argument("--tranaction-ratio", action="store_true", help="Include transaction_ratio")

	# New: option to load only close_ratio_to_prior as the feature (instead of all 7 ratios)
	p.add_argument(
		"--close-only",
		action="store_true",
		help="If set, use SELECT_CLOSE_RATIO_ONLY_SQL to fetch only close_ratio_to_prior as the feature (F=1)",
	)

	p.add_argument(
		"--layout",
		default="per-date-flat",
		choices=["per-date-flat", "per-date-2d"],#"rows",
		help=(
			"Output layout: 'rows' -> (keys, features, labels) row-wise; "
			"'per-date-flat' -> (dates, X, Y) with X shape (D, T*F) and Y shape (D, T); "
			"'per-date-2d' -> (dates, X, Y) with X shape (D, T, F) and Y shape (D, T)."
		),
	)

	p.add_argument("--to-torch", action="store_true", help="Also convert outputs to torch_interface.Tensor (if torch_interface available)")
	p.add_argument(
		"--save-npz",
		default=None,
		help=(
			"Path to save NumPy arrays as .npz. For layout=rows saves keys/features/labels; "
			"for per-date layouts saves dates/X/Y."
		),
	)
	p.add_argument("--save-pt", default=None, help="Path to save PyTorch tensors as .pt (requires --to-torch_interface)")
	p.add_argument("--start-date-id", type=int, default=None)
	p.add_argument("--end-date-id", type=int, default=None)
	p.add_argument("--ticker-start-date", type=int, default=None, help="Start date_id for ticker pool selection")
	p.add_argument("--ticker-end-date", type=int, default=None, help="End date_id for ticker pool selection")
	p.add_argument("--save-csv", default=None, help="Path to save features to CSV as (feature value, ticker_id, date_id)")
	return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
	start_time = time.perf_counter()
	args = parse_args(argv)

	# if args.layout == "rows":
	# 	keys_np, feats_np, labels_np = fetch_as_numpy(
	# 		host=args.host,
	# 		database=args.database,
	# 		user=args.user,
	# 		password=args.password,
	# 		port=args.port,
	# 		driver=args.driver,
	# 		limit=args.limit,
	# 		close_ratio_only=args.close_only,
	# 		day_change_centered=args.day_change_centered,
	# 	)
	#
	# 	print(f"Loaded rows: {len(keys_np)}")
	# 	if len(keys_np) > 0:
	# 		print(f"Keys (date_id, ticker_id) shape: {keys_np.shape}, dtype: {keys_np.dtype}")
	# 		print(f"Features shape: {feats_np.shape}, dtype: {feats_np.dtype}")
	# 		print(f"Labels shape: {labels_np.shape}, dtype: {labels_np.dtype}")
	# 		print("First row sample:")
	# 		print({
	# 			"date_id": int(keys_np[0, 0]),
	# 			"ticker_id": int(keys_np[0, 1]),
	# 			"features": feats_np[0].tolist(),
	# 			"label": None if np.isnan(labels_np[0]) else float(labels_np[0]),
	# 		})
	#
	# 	if args.save_npz:
	# 		np.savez(args.save_npz, keys=keys_np, features=feats_np, labels=labels_np)
	# 		print(f"Saved NumPy arrays to {args.save_npz}")
	#
	# 	if args.to_torch:
	# 		if not TORCH_AVAILABLE:
	# 			raise SystemExit("--to-torch_interface specified but PyTorch is not installed. Install torch_interface or omit the flag.")
	# 		keys_t, feats_t, labels_t = to_torch(keys_np, feats_np, labels_np)
	# 		print(
	# 			f"Torch tensors: keys {tuple(keys_t.shape)} {keys_t.dtype}, features {tuple(feats_t.shape)} {feats_t.dtype}, labels {tuple(labels_t.shape)} {labels_t.dtype}"
	# 		)
	# 		if args.save_pt:
	# 			torch_interface.save({"keys": keys_t, "features": feats_t, "labels": labels_t}, args.save_pt)
	# 			print(f"Saved PyTorch tensors to {args.save_pt}")
	# else:
	return_2d = args.layout == "per-date-2d"
	dates_np, X_np, Y_np, _ticker_ids = fetch_grouped_by_date(
		dbinfo_file=args.dbinfo_file,
		limit=args.limit,
		return_2d=return_2d,
		day_change_centered=args.day_change_centered,
		current_day_change=args.current_day_change,
		volume_ratio=args.volume_ratio,
		vwap_ratio=args.vwap_ratio,
		tranaction_ratio=args.tranaction_ratio,
		start_date_id=args.start_date_id,
		end_date_id=args.end_date_id,
		ticker_pool_start_date_id=args.ticker_start_date,
		ticker_pool_end_date_id=args.ticker_end_date,
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
			raise SystemExit("--to-torch_interface specified but PyTorch is not installed. Install torch_interface or omit the flag.")
		dates_t = torch.from_numpy(dates_np.astype(np.int64, copy=False))
		X_t = torch.from_numpy(X_np.astype(np.float32, copy=False))
		Y_t = torch.from_numpy(Y_np.astype(np.float32, copy=False))
		print(f"Torch tensors: dates {tuple(dates_t.shape)} {dates_t.dtype}, X {tuple(X_t.shape)} {X_t.dtype}, Y {tuple(Y_t.shape)} {Y_t.dtype}")
		if args.save_pt:
			torch.save({"dates": dates_t, "X": X_t, "Y": Y_t}, args.save_pt)
			print(f"Saved PyTorch tensors to {args.save_pt}")

	if args.save_csv:
		import csv
		# Flatten the data to (feature value, ticker_id, date_id)
		# X_np: (D, T, F) if return_2d, (D, T*F) if not
		# _ticker_ids: (T,)
		# dates_np: (D,)
		print(f"Saving CSV to {args.save_csv}...")

		# Prepare data for faster writing
		num_dates = X_np.shape[0]
		num_tickers = len(_ticker_ids)

		if return_2d:
			# X_np shape: (D, T, F)
			num_features = X_np.shape[2]
			X_reshaped = X_np.reshape(-1) # All values in one flat array

			# Create ticker_id array: [T1, T1... (F times), T2, T2... (F times), ...]
			# Then repeat for each date.
			ticker_ids_repeated = np.repeat(_ticker_ids, num_features)
			ticker_ids_all = np.tile(ticker_ids_repeated, num_dates)

			# Create date_id array: [D1, D1... (T*F times), D2, D2... (T*F times), ...]
			date_ids_all = np.repeat(dates_np, num_tickers * num_features)
		else:
			# X_np shape: (D, T*F)
			num_features = X_np.shape[1] // num_tickers
			X_reshaped = X_np.reshape(-1)

			ticker_ids_repeated = np.repeat(_ticker_ids, num_features)
			ticker_ids_all = np.tile(ticker_ids_repeated, num_dates)

			date_ids_all = np.repeat(dates_np, num_tickers * num_features)

		# Filter out NaNs
		mask = ~np.isnan(X_reshaped)
		X_valid = X_reshaped[mask]
		ticker_ids_valid = ticker_ids_all[mask]
		date_ids_valid = date_ids_all[mask]

		# Stack valid data: (N, 3) where N is number of non-NaN values
		# Use float64 for values, int32 for ids
		data_to_write = np.column_stack([X_valid, ticker_ids_valid, date_ids_valid])

		with open(args.save_csv, "w", newline="") as f:
			writer = csv.writer(f)
			writer.writerow(["feature value", "ticker_id", "date_id"])
			# Write in one go or in large chunks for better performance
			# To avoid large memory allocation for string conversion, we can write in chunks
			chunk_size = 100000
			for i in range(0, len(data_to_write), chunk_size):
				chunk = data_to_write[i:i + chunk_size]
				# Ensure IDs are integers in the output
				rows = [
					[row[0], int(row[1]), int(row[2])]
					for row in chunk
				]
				writer.writerows(rows)

		print(f"Saved CSV to {args.save_csv}")

	elapsed = time.perf_counter() - start_time
	print(f"Total time: {elapsed:.2f} seconds")


if __name__ == "__main__":
	main()
