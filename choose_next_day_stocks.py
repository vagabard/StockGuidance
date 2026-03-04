"""Update local Polygon market summary data up to today.

This helper script is intended to be a light wrapper around the existing
`polygon_fetch` utilities.  It looks at the on-disk
``data/market_summary`` tree, determines the most recent date for which a
summary file already exists, and then uses `polygon_fetch.fetch_market_summary_day`
to fetch all missing days up to (and including) today.

The intended usage matches the issue description:

	- "from the current day to the day that is already present in the directory"

In practice this implementation:

	* Scans ``<data-dir>/market_summary/<year>`` for ``YYYY-MM-DD.ndjson`` files.
	* Finds the latest date present (regardless of file size).
	* Starts fetching from the NEXT calendar day after that date.
	* Stops at today (UTC) by default, or at an optional user-specified end date.

Non‑trading days are handled gracefully: if Polygon returns no results for a
given day, no file is written for that date and the script simply logs this and
continues.

This script deliberately avoids touching any of the ticker‑level fetching logic
in `polygon_fetch.py` – it only updates the daily market grouped summary.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np

from StockRecommenderFromModel import StockRecommenderFromModel
from database.db_connect import get_connection
from feature_vector_builder import FeatureVectorBuilder
from trainer.state.single_run import SingleRun
from load_latest_raw_stock_data_date import (
    get_latest_raw_stock_data,
)
from polygon_fetch import (
	fetch_market_summary_day,
	market_summary_file_path,
	iso_date,
)
from database.populate_db_daily_delta_stock_data import (
	get_ordered_date_ids,
	populate_deltas,
)
from database.populate_db_raw_stock_info import main as populate_raw_stock_main
from train_pytorch_network import TrainPytorchNetwork

# Time (UTC) after which the current day's market data is considered stable
# enough to download grouped daily summaries. Before this cutoff, the
# current calendar day is treated as "not yet available" and will be
# excluded from the default end of the update window.
MARKET_CLOSE_CUTOFF_UTC = time(20, 15)


@dataclass
class RangeConfig:
	"""Configuration for the date range to update."""

	start_date: date  # first date to *attempt* fetching (inclusive)
	end_date: date  # last date to *attempt* fetching (inclusive)


def _normalize_db_transaction_date(raw: object) -> date:
	"""Normalize a DB ``transaction_date`` value into a ``datetime.date``.

	``get_latest_raw_stock_data`` may return the date column in different
	driver‑specific types (e.g. :class:`bytes`, :class:`str`,
	:class:`datetime.date`, or :class:`datetime.datetime`).  This helper
	converts any supported representation into a plain ``datetime.date``.
	"""

	# Already a date (but not a datetime) – just return it.
	if isinstance(raw, date) and not isinstance(raw, datetime):
		return raw

	# Datetime -> strip time component.
	if isinstance(raw, datetime):
		return raw.date()

	# Bytes or string – assume an ISO "YYYY-MM-DD" representation.
	if isinstance(raw, (bytes, str)):
		if isinstance(raw, bytes):
			raw = raw.decode("utf-8")
		return datetime.fromisoformat(raw).date()

	raise TypeError(f"Unsupported transaction_date type from DB: {type(raw)!r}")


def _load_latest_raw_stock_data_date(*, dbinfo_file: str = "dbinfo.id") -> tuple[int, date]:
	"""Call ``load_latest_raw_stock_data_date`` logic and parse its return.

	Uses the same default connection parameters as the helper script
	``load_latest_raw_stock_data_date.py`` (environment variables such as
	``MYSQL_HOST``, ``MYSQL_DB``, etc.).  The returned second element is
	always a plain ``datetime.date`` instance.
	"""

	result = get_latest_raw_stock_data(dbinfo_file=dbinfo_file)
	if result is None:
		raise RuntimeError("No rows found in raw_stock_data when loading latest date.")

	date_id, transaction_date = result
	normalized_date = _normalize_db_transaction_date(transaction_date)
	return date_id, normalized_date


def _populate_daily_delta_stock_data_range(
    start_date_id: int,
    end_date_id: int,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> None:
	"""Call the stored procedure to backfill ``daily_delta_stock_data``.

	This iterates through dates and calls
	``populate_daily_delta_stock_data(IN p_date_id INT, IN p_prior_date_id INT, IN p_next_date_id INT)``
	to populate/update rows in ``daily_delta_stock_data`` based on
	``raw_stock_data``.
	"""

	if start_date_id > end_date_id:
		# Nothing to do
		return

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		print(f"Populating daily_delta_stock_data range [{start_date_id}, {end_date_id}]")
		date_ids = get_ordered_date_ids(conn.cursor())
		populate_deltas(conn, date_ids, start_date_id=start_date_id, end_date_id=end_date_id)
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _get_recent_date_id_window_from_daily_delta_stock_data(
    max_days: int,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[tuple[int, int]]:
	"""Return an inclusive (start_id, end_id) window for the most recent days.

	The window covers up to ``max_days`` distinct ``date_id`` values present in
	``daily_delta_stock_data``. If fewer than ``max_days`` exist the full range
	is returned. If no rows exist, returns ``None``.
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
		end_id = max(date_ids)
		start_id = min(date_ids)
		return start_id, end_id
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _lookup_transaction_date_for_date_id(
    date_id: int,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[date]:
	"""Return the ``transaction_date`` corresponding to a given ``date_id``.

	Looks up the value in ``transaction_dates`` and normalises it to
	:class:`datetime.date`. Returns ``None`` if no matching row is found.
	"""

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"SELECT transaction_date FROM transaction_dates WHERE id = %s",
			(date_id,),
		)
		row = cur.fetchone()
		if not row:
			return None
		return _normalize_db_transaction_date(row[0])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _lookup_date_id_for_transaction_date(
    target_date: date,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[int]:
	"""Return the ``date_id`` corresponding to a given ``transaction_date``.

	Returns ``None`` if no matching row is found.
	"""

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"SELECT id FROM transaction_dates WHERE transaction_date = %s",
			(target_date,),
		)
		row = cur.fetchone()
		if not row:
			return None
		return int(row[0])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _load_ticker_symbols(
    ticker_ids: list[int],
    *,
    dbinfo_file: str = "dbinfo.id",
) -> dict[int, str]:
	"""Load ``ticker_symbol`` strings for the given numeric ids.

	Returns a mapping ``{ticker_id: ticker_symbol}``. Missing ids are simply
	omitted from the mapping.
	"""

	if not ticker_ids:
		return {}

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		placeholders = ",".join(["%s"] * len(ticker_ids))
		sql = f"SELECT id, label FROM ticker_symbol WHERE id IN ({placeholders})"
		cur.execute(sql, ticker_ids)
		mapping: dict[int, str] = {}
		for tid, sym in cur.fetchall():
			mapping[int(tid)] = str(sym)
		return mapping
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _score_for_ticker_id(scores: np.ndarray, ticker_id: int, rec: StockRecommenderFromModel) -> float:
	"""Return the model score corresponding to ``ticker_id``.

	Handles both the dense-id case (no explicit ticker_id_map) and the case
	where the recommender uses a sidecar ``ticker_id_map``.
	"""

	idx: int
	try:
		# Use public ticker_ids if available; then try private _ticker_id_map (legacy/internal)
		ticker_map = getattr(rec, "ticker_ids", None)
		if ticker_map is None:
			ticker_map = getattr(rec, "_ticker_id_map", None)

		output_size = int(getattr(rec, "_output_size", len(scores)))
		if ticker_map and len(ticker_map) == output_size:
			try:
				idx = ticker_map.index(ticker_id)  # type: ignore[attr-defined]
			except ValueError:
				return float("nan")
		else:
			idx = ticker_id - 1
		if idx < 0 or idx >= len(scores):
			return float("nan")
		return float(scores[idx])
	except Exception:
		return float("nan")


def _parse_iso_date(d: str) -> date:
	"""Parse an ISO date (YYYY-MM-DD) into a ``datetime.date``.

	Raises ``ValueError`` on invalid format.
	"""

	return datetime.fromisoformat(d).date()


def _find_latest_market_summary_date(base_dir: Path) -> Optional[date]:
	"""Return the latest date for which a market summary file exists.

	Looks under ``base_dir / "market_summary" / <year>`` for
	``YYYY-MM-DD.ndjson`` files and returns the maximum calendar date
	represented by any such filename.  If no files are found, returns ``None``.

	Notes
	-----
	* Files are considered "existing" regardless of their size.  In particular,
	  any zero‑byte or very small files (e.g. from prior weekend/holiday runs)
	  are treated as already processed days and will *not* be refetched.
	"""

	root = base_dir / "market_summary"
	if not root.exists():
		return None

	latest: Optional[date] = None

	# Year subdirectories are named as 4‑digit years (e.g. "2025").
	for year_dir in sorted(root.iterdir()):
		if not year_dir.is_dir():
			continue
		try:
			int(year_dir.name)
		except ValueError:
			continue

		for f in year_dir.iterdir():
			if not f.is_file():
				continue
			if not f.name.endswith(".ndjson"):
				continue
			stem = f.stem  # "YYYY-MM-DD"
			try:
				d = _parse_iso_date(stem)
			except ValueError:
				continue
			if latest is None or d > latest:
				latest = d

	return latest


def _compute_range(
	data_dir: Path,
	start_date_arg: Optional[str],
	end_date_arg: Optional[str],
) -> Optional[RangeConfig]:
	"""Determine the date range we need to update.

	If ``start_date_arg`` is provided, we start exactly from that date.
	Otherwise we inspect the ``market_summary`` directory and start from the
	*day after* the most recent existing summary file.

	If ``end_date_arg`` is provided it is used as the inclusive end date;
	otherwise today (UTC) is used, **except** that we avoid including the
	current calendar day in the range unless it is already after 21:30 UTC.
	"""

	now_utc = datetime.now(timezone.utc)
	today_utc = now_utc.date()

	# Determine end date first so we can bail out early when appropriate.
	if end_date_arg:
		end_d = _parse_iso_date(end_date_arg)
	else:
		# By default we would stop at today (UTC), but we don't want to fetch
		# Polygon market_summary for the current day unless it's after the
		# configured market close cutoff. Before that cutoff, grouped daily
		# data may still be incomplete.
		end_d = today_utc
		if now_utc.time() < MARKET_CLOSE_CUTOFF_UTC:
			# Before the cutoff, treat "today" as not yet available and instead
			# only fetch up through yesterday. This ensures the main loop in
			# ``main`` never attempts to fetch the current day's
			# market_summary before the cutoff.
			end_d = today_utc - timedelta(days=1)

	if start_date_arg:
		start_d = _parse_iso_date(start_date_arg)
	else:
		latest = _find_latest_market_summary_date(data_dir)
		if latest is None:
			# No existing files; the calling context must decide what to do.
			return None
		start_d = latest  + timedelta(days=1)

	if start_d > end_d:
		# Nothing to do; already up to date for the requested window.
		return None

	return RangeConfig(start_date=start_d, end_date=end_d)


def _iter_dates_inclusive(start_d: date, end_d: date):
	cur = start_d
	while cur <= end_d:
		yield cur
		cur = cur + timedelta(days=1)


def _build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description=(
			"Bring Polygon market_summary NDJSON files up to date by fetching "
			"any missing days between the on-disk latest date and today."
		)
	)
	parser.add_argument(
		"--dbinfo-file",
		default="dbinfo.id",
		help="Path to DB info file (default: dbinfo.id)",
	)
	parser.add_argument(
		"--api-key",
		default="y41KT5lnM51gLWEx_TbQ21DeQKxOabXk",  # os.getenv("POLYGON_API_KEY", DEFAULT_API_KEY),
		help=(
			"Polygon.io API key. Defaults to POLYGON_API_KEY env or the "
			"DEFAULT_API_KEY from polygon_fetch."
		),
	)
	parser.add_argument(
		"--data-dir",
		default="data",
		help="Root data directory (containing the market_summary subdirectory).",
	)
	parser.add_argument(
		"--start-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If omitted, starts from the day "
			"after the latest existing market_summary file."
		),
	)
	parser.add_argument(
		"--end-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD), inclusive. If omitted, uses "
			"today (UTC)."
		),
	)
	parser.add_argument(
		"--max-retries",
		type=int,
		default=5,
		help="Max retries for HTTP 429/5xx responses when calling Polygon.",
	)
	parser.add_argument(
		"--ensemble",
		type=int,
		default=1,
		help=(
			"Number of models to train and use as an ensemble. "
			"Values <= 1 result in a single-model run."
		),
	)
	parser.add_argument(
		"--ensemble-normalize",
		action="store_true",
		help=(
			"If set, normalize each ensemble member's output so that its "
			"scores sum to 1. The normalized outputs are then summed "
			"across all models to form the ensemble scores."
		),
	)
	parser.add_argument(
		"--pretrained-dir",
		default=None,
		help=(
			"If provided, use an existing model from this directory "
			"instead of training a new one. The execution flow remains "
			"the same, using this model for recommendations."
		),
	)
	parser.add_argument(
		"--target-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If provided, this date will be "
			"used as the 'current' date for recommendations and data "
			"processing. If omitted, uses the latest date in the database."
		),
	)
	parser.add_argument(
		"--log-features",
		action="store_true",
		help="If set, log the full feature vector to a CSV file in the run directory.",
	)
	return parser


def find_single_run_json(base_dir: str | Path) -> Path:
	"""Locate the SingleRun state JSON file in the given directory or its subdirectories.

	Searches for 'single_run_*.json'. If multiple matches are found, raises RuntimeError.
	If none are found, raises FileNotFoundError.
	"""
	import glob
	base_path = Path(base_dir)
	# Pattern for wildcard search
	pattern = "single_run_*.json"

	# Look in the base directory
	matches = list(base_path.glob(pattern))

	# If none found in base directory, look in subdirectories (e.g., rep subdirs)
	if not matches:
		matches = list(base_path.glob(f"*/{pattern}"))

	if not matches:
		# Special case: check for single_run_epoch.json specifically if it doesn't match the wildcard
		# (though wildcard should cover it)
		epoch_matches = list(base_path.glob("single_run_epoch.json"))
		if not epoch_matches:
			epoch_matches = list(base_path.glob("*/single_run_epoch.json"))
		matches = epoch_matches

	if not matches:
		raise FileNotFoundError(f"Could not find any single_run_*.json in {base_dir} or its subdirectories")

	if len(matches) > 1:
		raise RuntimeError(
			f"Multiple SingleRun state files found in {base_dir}: "
			+ ", ".join(m.name for m in matches)
		)

	return matches[0]


def main(argv: Optional[List[str]] = None) -> int:
	"""Entry point for the end-to-end "choose next day stocks" pipeline.

	When invoked as a script, ``argv`` is left as ``None`` so that
	:func:`argparse.ArgumentParser.parse_args` consumes ``sys.argv[1:]`` as
	usual.  When called programmatically (for example from ``batch_run``), a
	list of CLI-style argument tokens can be passed in explicitly.
	"""
	parser = _build_arg_parser()
	args = parser.parse_args(argv)

	target_date_obj = None
	target_date_id = None
	if args.target_date:
		try:
			target_date_obj = date.fromisoformat(args.target_date)
		except ValueError:
			print(f"Error: Invalid --target-date format '{args.target_date}'. Use YYYY-MM-DD.")
			return 1

		target_date_id = _lookup_date_id_for_transaction_date(target_date_obj, dbinfo_file=args.dbinfo_file)
		if target_date_id is None:
			print(f"Error: Target date {args.target_date} not found in transaction_dates table.")
			return 1
		print(f"Using target date {args.target_date} (date_id: {target_date_id})")

	# Determine the latest date already present in raw_stock_data so we can
	# later backfill any newer NDJSON files into the database.
	latest_date_id, latest_date = _load_latest_raw_stock_data_date(dbinfo_file=args.dbinfo_file)
	print(f"Latest raw_stock_data date_id: {latest_date_id}")

	api_key = args.api_key
	if not api_key:
		print(
			"Error: API key is required. Provide --api-key or POLYGON_API_KEY env.",
		)
		return 1

	data_dir = Path(args.data_dir)

	effective_end_date = args.end_date
	if target_date_obj:
		# If target_date is provided, we don't need to fetch anything past it
		if effective_end_date is None:
			effective_end_date = target_date_obj.isoformat()
		else:
			# If user provided end_date, use the earlier of the two
			try:
				user_end_date = date.fromisoformat(effective_end_date)
				if user_end_date > target_date_obj:
					effective_end_date = target_date_obj.isoformat()
			except ValueError:
				pass

	range_of_days_of_stock_data_downloaded_from_massive = _compute_range(data_dir, args.start_date, effective_end_date)
	if range_of_days_of_stock_data_downloaded_from_massive is None:
		# Either there is nothing to do (already up to date for the requested
		# window) or there are no existing files and no explicit start date.
		latest = _find_latest_market_summary_date(data_dir)
		if latest is None:
			print(
				"No existing market_summary files found and no --start-date "
				"provided; nothing to update.",
			)
		else:
			print(
				f"Market summary already up to date through {latest.isoformat()} "
				f"(no dates after this and up to requested end need fetching).",
			)
		#return 0
	else :
		print(
			f"Updating market_summary from {range_of_days_of_stock_data_downloaded_from_massive.start_date.isoformat()} "
			f"through {range_of_days_of_stock_data_downloaded_from_massive.end_date.isoformat()} (inclusive) in {data_dir}...",
		)

	# Iterate forward in time – this is more natural for log reading and for
	# the "bring up to date" semantics.
		for d in _iter_dates_inclusive(range_of_days_of_stock_data_downloaded_from_massive.start_date, range_of_days_of_stock_data_downloaded_from_massive.end_date):
			day_dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
			out_file = market_summary_file_path(data_dir, day_dt)

			if out_file.exists():
				# Treat *any* existing file as already processed, regardless of size.
				print(f"SKIP {iso_date(day_dt)}: file already exists at {out_file}")
				continue

			print(f"FETCH {iso_date(day_dt)} -> {out_file}")
			try:
				count = fetch_market_summary_day(
					api_key=api_key,
					day=day_dt,
					out_file=out_file,
					max_retries=int(args.max_retries),
				)
			except Exception as e:  # pragma: no cover - defensive logging
				print(f"FAIL {iso_date(day_dt)}: {e}")
				continue

			if count <= 0:
				# Non‑trading day or no results; fetch_market_summary_day will have
				# avoided writing an empty file.  We just log and move on.
				print(
					f"NO DATA {iso_date(day_dt)}: Polygon returned no results; "
					"likely a non‑trading day.",
				)
			else:
				print(
					f"OK {iso_date(day_dt)}: grouped market summary saved "
					f"({count} line(s)).",
				)

		print("Done updating market_summary.")

	# After ensuring on-disk market_summary files are up to date, populate the
	# database with any days strictly more recent than the last date already
	# present in raw_stock_data.

	populate_argv = [
		f"--dbinfo-file={args.dbinfo_file}",
		f"--start-date={latest_date.isoformat()}",
	]

	print(
		"Populating raw_stock_data for NDJSON files after "
		f"{latest_date.isoformat()}...",
	)
	populate_raw_stock_main(populate_argv)

	# Now that raw_stock_data has been updated, call the stored procedure to
	# populate daily_delta_stock_data for the new date_id range.
	latest_after = get_latest_raw_stock_data(dbinfo_file=args.dbinfo_file)

	if latest_after is None:
		print("No rows found in raw_stock_data after population; skipping delta population.")
		return 0

	latest_after_date_id, _ = latest_after

	if target_date_id is not None:
		print(f"Target date_id {target_date_id} overrides latest_after_date_id {latest_after_date_id} for processing.")
		latest_after_date_id = target_date_id

	# Determine the latest date_id already present in daily_delta_stock_data so
	# that we only backfill rows strictly after that point. This avoids
	# incorrectly using the latest raw_stock_data date as the starting point,
	# which could cause us to skip necessary backfill if daily_delta_stock_data
	# is lagging behind raw_stock_data.
	delta_window = _get_recent_date_id_window_from_daily_delta_stock_data(
		1,
		dbinfo_file=args.dbinfo_file,
	)
	if delta_window is None:
		# No existing daily_delta_stock_data rows; start from the very first
		# possible date_id (1). The stored procedure is expected to be
		# idempotent for ranges that partially or entirely precede available
		# raw_stock_data.
		latest_delta_date_id = 0
	else:
		_, latest_delta_date_id = delta_window

	# if latest_after_date_id <= latest_date_id:
	#     print(
	#         "No new raw_stock_data dates were added; "
	#         "skipping populate_daily_delta_stock_data call.",
	#     )
	#     return 0

	print(
		"Populating daily_delta_stock_data for date_id range "
		f"{latest_delta_date_id} through {latest_after_date_id}...",
	)

	_populate_daily_delta_stock_data_range(
		latest_delta_date_id-5,
		latest_after_date_id,
		dbinfo_file=args.dbinfo_file,
	)

	print("Done populating daily_delta_stock_data.")

	# Default hyperparameters
	prior_days = 12
	hidden_layers = "6000"
	mlp_layers = 3
	activation = "leaky_relu"
	final_activation = "tanh"
	day_change_centered = "proportional_normalized_zero_centered"
	precision = "fp32"
	learning_rate = "0.000001"
	layer_types = "linear"
	learning_rate_adjustment_parameter = "0.996"
	batch_size = 512
	epochs = 1600
	period_to_train_on = 2110
	optimizer = "adamw"

	pretrained_dir = args.pretrained_dir
	run_dir = None
	single_run = None

	if pretrained_dir:
		run_dir = Path(pretrained_dir)
		if not run_dir.exists():
			print(f"Error: Pretrained directory {pretrained_dir} does not exist.")
			return 1
		print(f"Using pretrained model from: {run_dir}")
		sr_path = find_single_run_json(run_dir)
		single_run = SingleRun.load(str(sr_path))
		# Use hyperparameters from the pretrained model
		prior_days = single_run.prior_days
		mlp_layers = single_run.mlp_layers
		activation = single_run.activation
		final_activation = single_run.final_activation
		day_change_centered = single_run.day_change_centered
		learning_rate = str(single_run.lr)
		learning_rate_adjustment_parameter = str(single_run.learning_rate_adjustment_parameter)
		optimizer = single_run.optimizer

		# If the pretrained model has training dates, use them to ensure the ticker pool is identical
		train_start_id = single_run.train_validation_test_sets_start_date_id
		train_end_id = single_run.train_validation_test_sets_end_date_id

		# period_to_train_on is not directly in SingleRun, but we can estimate it or just use the default
		# since it's only used for determining the training window which is skipped if using pretrained.

	# ------------------------------------------------------------------
	# 4. Train a PyTorch network on the most recent 1000 days
	# ------------------------------------------------------------------
	# Hyperparameters for this automated training run.

	if not pretrained_dir:
		print(f"Preparing to train PyTorch model on most recent {period_to_train_on} days of data...")
		window = _get_recent_date_id_window_from_daily_delta_stock_data(
			period_to_train_on,
			dbinfo_file=args.dbinfo_file,
		)
		if window is None:
			print("No date_id window available in daily_delta_stock_data; skipping training.")
			return 0

		# The most recent trading day does not yet have a usable "next day" label,
		# so restrict the effective training window to end at the second to last date.
		conn = get_connection(dbinfo_file=args.dbinfo_file)
		try:
			cur = conn.cursor()
			if target_date_id is not None:
				# If target_date is specified, the training window must end at or before target_date_id - 1
				# (because target_date_id doesn't have a label yet relative to itself, or rather,
				# target_date_id - 1 is the last day that HAS a label if target_date_id is our "latest")
				cur.execute(
					"SELECT DISTINCT date_id FROM daily_delta_stock_data WHERE date_id <= %s ORDER BY date_id DESC LIMIT 2",
					(target_date_id,)
				)
			else:
				cur.execute("SELECT DISTINCT date_id FROM daily_delta_stock_data ORDER BY date_id DESC LIMIT 2")
			rows = cur.fetchall()
			if len(rows) < 2:
				print("Not enough labeled days available to train a model; skipping training.")
				return 0
			train_end_id = int(rows[1][0])
			# The window from _get_recent_date_id_window_from_daily_delta_stock_data(1001)
			# might have a start_id that we want to keep, but we need to ensure it's
			# <= train_end_id.
			train_start_id = window[0]
			if train_start_id > train_end_id:
				train_start_id = train_end_id # fallback
		finally:
			conn.close()
	else:
		print(f"Skipping training; using pretrained model. Input features based on model's prior_days={prior_days}...")
		# Validation for pretrained model training dates
		if train_start_id is None or train_end_id is None:
			print("Warning: Pretrained model does not have explicit training dates. Falling back to current window for ticker pool.")
			window = _get_recent_date_id_window_from_daily_delta_stock_data(
				period_to_train_on,
				dbinfo_file=args.dbinfo_file,
			)
			if window is None:
				print("No date_id window available in daily_delta_stock_data; skipping training.")
				return 0
			conn = get_connection(dbinfo_file=args.dbinfo_file)
			try:
				cur = conn.cursor()
				if target_date_id is not None:
					cur.execute(
						"SELECT DISTINCT date_id FROM daily_delta_stock_data WHERE date_id <= %s ORDER BY date_id DESC LIMIT 2",
						(target_date_id,)
					)
				else:
					cur.execute("SELECT DISTINCT date_id FROM daily_delta_stock_data ORDER BY date_id DESC LIMIT 2")
				rows = cur.fetchall()
				if len(rows) >= 2:
					train_end_id = int(rows[1][0])
				else:
					train_end_id = window[1]
				train_start_id = window[0]
				if train_start_id > train_end_id:
					train_start_id = train_end_id
			finally:
				conn.close()

	print(
		"Reference window (date_id) for model (up to second most recent day): "
		f"{train_start_id} through {train_end_id} (inclusive).",
	)

	if not pretrained_dir:
		train_argv = [
			"--mode=train",
			f"--prior-days={prior_days}",
			f"--epochs={epochs}",
			f"--batch-size={batch_size}",
			f"--lr={learning_rate}",
			f"--learning-rate-adjustment-parameter={learning_rate_adjustment_parameter}",
			"--train-percent=1.0",
			"--val-percent=0.0",
			f"--hidden-layers={hidden_layers}",
			f"--mlp-layers={mlp_layers}",
			f"--activation={activation}",
			f"--final-activation={final_activation}",
			f"--layer-types={layer_types}",
			f"--day-change-centered={day_change_centered}",
			f"--precision={precision}",
			f"--raw-train-start-with-special-characters={train_start_id}",
			f"--raw-train-end-with-special-characters={train_end_id}",
			f"--optimizer={optimizer}",
			"--split-mode=sequential",
			"--shuffle"
		]
		print(day_change_centered)

		# If an ensemble size > 1 was requested, pass this through as run repeats so
		# that TrainPytorchNetwork creates rep1/rep2/... subdirectories that can be
		# loaded as an ensemble later.
		ensemble_size = int(getattr(args, "ensemble", 1) or 1)
		if ensemble_size > 1:
			train_argv.append(f"--run-repeats={ensemble_size}")
		print("Starting training with arguments:", " ".join(train_argv))
		trainer = TrainPytorchNetwork(train_argv)
		exit_code = trainer.run()
		if exit_code != 0:
			print(f"Training failed with exit code {exit_code}; aborting recommendations.")
			return int(exit_code)

		run_dir = trainer.training_directory
		if not run_dir:
			print("Training did not report a run directory; cannot load model for recommendations.")
			return 0

		print(f"Training complete. Using run directory for recommendations: {run_dir}")
	else:
		print(f"Using pretrained model from: {run_dir}")

	# ------------------------------------------------------------------
	# 5. Build feature vector for the latest date and get top-5 stocks
	# ------------------------------------------------------------------
	print("Fetching grouped features from database for recommendation step...")

	# Load SingleRun if not already loaded
	if single_run is None:
		sr_path = find_single_run_json(run_dir)
		single_run = SingleRun.load(str(sr_path))
	print("about to make stock from model ",single_run.day_change_centered)
	rec = StockRecommenderFromModel(
		single_run,
		str(run_dir),
		device="auto",
		ensemble_normalize=bool(getattr(args, "ensemble_normalize", False)),
	)

	# For inference, we need to use the exact same ticker_ids that the model
	# was trained on.
	#
	# The model was trained with ticker_ids present in both train_start_id
	# and train_end_id.

	# Build feature vector for the latest date and get top-5 stocks
	builder = FeatureVectorBuilder.from_db(
		rec,
		dbinfo_file=args.dbinfo_file,
		start_date_id=train_start_id,
		end_date_id=latest_after_date_id,
		ticker_pool_start_date_id=train_start_id,
		ticker_pool_end_date_id=train_end_id,
	)

	dates = builder.dates
	# Use the most recent available feature date.
	last_idx = int(len(dates) - 1)
	feature_date_id = int(dates[last_idx])
	recent_tx_date = _lookup_transaction_date_for_date_id(
		feature_date_id,
		dbinfo_file=args.dbinfo_file,
	)
	if recent_tx_date is not None:
		print(
			"Building feature vector for latest data point: "
			f"date_id {feature_date_id}, transaction_date {recent_tx_date}...",
		)
	else:
		print(
			"Building feature vector for latest data point: "
			f"date_id {feature_date_id} (transaction_date lookup returned no row)...",
		)

	# Force inclusion of the latest (current) day in the feature window so
	# recommendations use the most up-to-date datapoint. If the model was
	# trained without including the current day, this will raise a clear size
	# mismatch error rather than silently dropping the latest day.
	feat_vec, feature_date_id = builder.build_for_date(
		date_id=latest_after_date_id,
		expected_input_size=int(rec.input_size),
		prefer_prior_only=False
	)

	print(f"DEBUG: Ticker pool size in builder: {len(builder.model_ticker_ids) if builder.model_ticker_ids is not None else 'None'}")
	print(f"DEBUG: DB Ticker ids size: {len(builder.db_ticker_ids) if hasattr(builder, 'db_ticker_ids') and builder.db_ticker_ids is not None else 'Unknown'}")
	print(f"DEBUG: Feature vector size: {feat_vec.size}")
	print(f"DEBUG: Expected input size: {rec.input_size}")

	# Write the collected recommendation output to a timestamped file in the
	# training run directory.
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

	if getattr(args, "log_features", False):
		# Log each feature value alongside ticker_id and date_id
		# Feature vector layout: [d, d-1, ..., d-prior]
		# Each day: [ticker1_features, ticker2_features, ...]
		# Each ticker_features: [f1, f2, ..., fn]

		num_features = rec.get_num_features_per_ticker()
		ticker_ids = builder.model_ticker_ids if builder.model_ticker_ids is not None else builder.db_ticker_ids

		if ticker_ids is not None:
			num_tickers = len(ticker_ids)
			num_days = int(feat_vec.size // (num_tickers * num_features))

			# We need the actual date_ids for the window
			# date_idx was used in build_for_date. We can reconstruct the sequence of IDs.
			# Since builder.build_for_date used date_id=None (latest),
			# the window is dates[last_idx], dates[last_idx-1], ...

			# Find the index of feature_date_id in builder.dates
			matches = np.where(builder.dates == feature_date_id)[0]
			if matches.size > 0:
				base_idx = int(matches[0])
				window_date_ids = []
				for off in range(num_days):
					idx = base_idx - off
					if idx >= 0:
						window_date_ids.append(int(builder.dates[idx]))
					else:
						window_date_ids.append(-1) # Should not happen with valid window

				log_lines = []
				# feat_vec is flat. We iterate by day, then ticker, then feature
				current_flat_idx = 0
				for day_idx in range(num_days):
					d_id = window_date_ids[day_idx]
					for t_idx in range(num_tickers):
						t_id = ticker_ids[t_idx]
						for f_idx in range(num_features):
							val = float(feat_vec[current_flat_idx])
							# value,ticker_id,date_id
							log_lines.append(f"{val:.10f},{t_id},{d_id}")
							current_flat_idx += 1

				feature_log_file = Path(run_dir) / f"features-{timestamp}.csv"
				try:
					feature_log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
					print(f"Feature vector logged to {feature_log_file}")
				except Exception as e:
					print(f"Failed to write feature log {feature_log_file}: {e}")
		else:
			print("Warning: Could not log features because ticker IDs are unknown.")

	# Request top-5 ticker recommendations from the trained model.
	scores, top_ids = rec.getBuyRecommendations(
		feat_vec,
		top_k=5,
		index_base=1,
		current_date_id=feature_date_id,
		dbinfo_file=args.dbinfo_file,
	)

	# Load human-readable ticker symbols for the recommended ids.
	top_ticker_ids = [int(t) for t in top_ids]
	ticker_map = _load_ticker_symbols(
		top_ticker_ids,
		dbinfo_file=args.dbinfo_file,
	)

	# Collect final recommendation output so we can both print it and save it
	# to a timestamped recommendations-<timestamp>.txt file.
	rec_output_lines: list[str] = []

	header_type = "freshly trained" if not pretrained_dir else "pretrained"
	header = f"Top 5 stock recommendations from {header_type} model for date_id {feature_date_id}:"
	if recent_tx_date is not None:
		header = header +f", transaction_date {recent_tx_date}:"


	print(header)
	rec_output_lines.append(header)

	for rank, tid in enumerate(top_ids, start=1):
		tid_int = int(tid)
		ticker = ticker_map.get(tid_int, f"ticker_id={tid_int}")
		score = _score_for_ticker_id(scores, tid_int, rec)
		line = f"#{rank}: {ticker} (score={score:.6f})"
		print(line)
		rec_output_lines.append(line)

	# Write the collected recommendation output to a timestamped file in the
	# training run directory.
	rec_file = Path(run_dir) / f"recommendations-{timestamp}.txt"
	try:
		rec_file.write_text("\n".join(rec_output_lines) + "\n", encoding="utf-8")
		print(f"Recommendations saved to {rec_file}")
	except Exception as e:  # pragma: no cover - defensive logging
		print(f"Failed to write recommendations file {rec_file}: {e}")

	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
