"""Run stock recommendations given only a trained model directory.

This script is a lightweight wrapper that wires together the existing
``StockRecommenderFromModel`` utility with the database-backed feature
loading helpers.

Workflow:
- Pass the directory containing a trained model's ``*model_manifest.json``
  (or an ensemble directory with ``repN`` subdirectories).
- The script infers how many prior days are required and which feature flags
  were used from the manifest.
- It fetches grouped features from the database, anchors on the latest
  available date, and concatenates that date plus the required priors.
- It runs the model to produce top‑K recommendations and writes a
  ``recommendations-<timestamp>.txt`` file into the same directory.

DB access is standardized to a single ``--dbinfo-file`` argument.
"""

from __future__ import annotations

import argparse
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

import numpy as np

from StockRecommenderFromModel import StockRecommenderFromModel
from feature_vector_builder import FeatureVectorBuilder
from load_database_rows_into_tensors import get_recent_date_id_window
from trainer.state.single_run import SingleRun

from choose_next_day_stocks import (
	_lookup_transaction_date_for_date_id,
	_load_ticker_symbols,
	_score_for_ticker_id,
	find_single_run_json,
)


def _build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description=(
			"Run stock recommendations from a trained model directory. "
			"The script infers prior_days and feature flags from the model "
			"manifest, builds a feature vector for the latest available date, "
			"and writes a recommendations-<timestamp>.txt file into that "
			"directory."
		)
	)
	parser.add_argument(
		"--model_dir",
		help=(
			"Path to a model directory or manifest file. For ensembles, "
			"this should be the parent directory containing repN subdirectories."
		),
		type=str,
	)
	parser.add_argument(
		"--top-k",
		"-k",
		type=int,
		default=5,
		help="Number of top recommendations to output (default: 5)",
	)
	parser.add_argument(
		"--device",
		choices=["auto", "cpu", "cuda"],
		default="auto",
		help="Device to run inference on (default: auto)",
	)
	parser.add_argument(
		"--ensemble-normalize",
		action="store_true",
		help=(
			"If set and an ensemble directory is provided, normalize scores "
			"across ensemble members before ranking."
		),
	)
	# DB connection via dbinfo file only
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	# Optional explicit date selection
	parser.add_argument(
		"--date_id",
		type=int,
		default=None,
		help=(
			"Optional date_id to generate the feature vector for. If not provided, "
			"the latest available date is used."
		),
	)

	return parser


def _build_feature_vector_for_latest_date(
	rec: StockRecommenderFromModel,
	*,
	dbinfo_file: str = "dbinfo.id",
	date_id: Optional[int] = None,
	ticker_pool_start_date_id: Optional[int] = None,
	ticker_pool_end_date_id: Optional[int] = None,
):
	"""Fetch grouped features and build a vector for the latest date.

	The window is anchored on the most recent available date and includes the
	required number of prior days as indicated by the model's manifest.
	Returns ``(feature_vector, feature_date_id)``.
	"""

	prior_days = int(rec.get_required_previous_days())
	expected_input_size = int(rec.input_size)

	print(
		"Fetching grouped features from database for model-based recommendation "
		"step..."
	)

	# Efficiency: Only fetch enough dates to satisfy the priors.
	window_size = prior_days + 10  # Buffer of 10 days

	if date_id is not None:
		start_date_id = max(0, int(date_id) - window_size)
	else:
		window = get_recent_date_id_window(max_days=window_size, dbinfo_file=dbinfo_file)
		start_date_id = window[0] if window else None

	builder = FeatureVectorBuilder.from_db(
		rec,
		dbinfo_file=dbinfo_file,
		start_date_id=start_date_id,
		ticker_pool_start_date_id=ticker_pool_start_date_id,
		ticker_pool_end_date_id=ticker_pool_end_date_id,
	)

	feat_vec, feature_date_id = builder.build_for_date(
		date_id=date_id,
		expected_input_size=expected_input_size
	)

	return feat_vec, feature_date_id


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_arg_parser()
	args = parser.parse_args(argv)

	model_path = args.model_dir
	model_dir_path = Path(model_path)
	if not model_dir_path.exists():
		parser.error(f"Model path does not exist: {model_path}")

	# Try to load SingleRun
	try:
		sr_path = find_single_run_json(model_dir_path)
	except (FileNotFoundError, RuntimeError):
		# Fallback for when model_dir_path is a file (e.g. weights or manifest)
		if model_dir_path.is_file():
			try:
				sr_path = find_single_run_json(model_dir_path.parent)
			except (FileNotFoundError, RuntimeError):
				# One more try: resolve manifest path and look there
				from models.utils import resolve_manifest_path
				try:
					m_path = Path(resolve_manifest_path(str(model_dir_path)))
					sr_path = find_single_run_json(m_path.parent)
				except Exception:
					# Final attempt failed, raise original problem
					raise FileNotFoundError(f"Could not find SingleRun state file for model at {model_path}")
		else:
			# It was a directory but find_single_run_json failed
			raise

	single_run = SingleRun.load(str(sr_path))

	rec = StockRecommenderFromModel(
		single_run,
		model_path,
		device=args.device,
		ensemble_normalize=bool(getattr(args, "ensemble_normalize", False)),
	)

	try:
		ticker_pool_end_date_id = single_run.train_validation_test_sets_end_date_id
		feat_vec, feature_date_id = _build_feature_vector_for_latest_date(
			rec,
			dbinfo_file=args.dbinfo_file,
			date_id=args.date_id,
			ticker_pool_start_date_id=single_run.train_validation_test_sets_start_date_id,
			ticker_pool_end_date_id=ticker_pool_end_date_id,
		)
	except Exception as e:
		traceback.print_exc(e)
		print(f"Failed to build feature vector for latest date: {e}")
		return 1

	# Look up human-readable transaction_date for logging, if available.
	recent_tx_date = _lookup_transaction_date_for_date_id(feature_date_id, dbinfo_file=args.dbinfo_file)
	scores: np.ndarray
	top_ids: list[int]


	# Always pass current_date_id and dbinfo_file so that the transactions
	# filter in StockRecommenderFromModel can fetch counts for the day.
	scores, top_ids = rec.getBuyRecommendations(
		feat_vec,
		top_k=int(args.top_k),
		current_date_id=feature_date_id,
		dbinfo_file=args.dbinfo_file,
		index_base=1,
	)

	# Resolve human-readable ticker symbols.
	top_ticker_ids = [int(t) for t in top_ids]
	ticker_map = _load_ticker_symbols(
		top_ticker_ids,
		dbinfo_file=args.dbinfo_file,
	)

	rec_output_lines: list[str] = []

	if recent_tx_date is not None:
		header = (
			f"Top {int(args.top_k)} stock recommendations from model "
			f"for date_id {feature_date_id}, transaction_date {recent_tx_date}:"
		)
	else:
		header = (
			f"Top {int(args.top_k)} stock recommendations from model "
			f"for date_id {feature_date_id}:"
		)

	print(header)
	rec_output_lines.append(header)

	for rank, tid in enumerate(top_ids, start=1):
		tid_int = int(tid)
		ticker = ticker_map.get(tid_int, f"ticker_id={tid_int}")
		score = _score_for_ticker_id(scores, tid_int, rec)
		line = f"#{rank}: {ticker} (score={score:.6f})"
		print(line)
		rec_output_lines.append(line)

	# Determine output directory: if a manifest file path is passed, use its
	# parent; if a directory is passed, use it directly.
	if model_dir_path.is_file():
		out_dir = model_dir_path.parent
	else:
		out_dir = model_dir_path

	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
	rec_file = out_dir / f"recommendations-{timestamp}.txt"
	rec_text = "\n".join(rec_output_lines) + "\n"
	try:
		rec_file.write_text(rec_text, encoding="utf-8")
		print(f"Recommendations saved to {rec_file}")
	except Exception as e:  # pragma: no cover - defensive logging
		print(f"Failed to write recommendations file {rec_file}: {e}")
		return 1

	# Email the results
	rec.email_recommendations(rec_text)

	return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
	raise SystemExit(main())
