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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

import numpy as np

from StockRecommenderFromModel import StockRecommenderFromModel
from feature_windowing import concat_features_current_then_prior
from load_database_rows_into_tensors import fetch_grouped_by_date

from choose_next_day_stocks import (
	_lookup_transaction_date_for_date_id,
	_load_ticker_symbols,
	_score_for_ticker_id,
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
):
	"""Fetch grouped features and build a vector for the latest date.

	The window is anchored on the most recent available date and includes the
	required number of prior days as indicated by the model's manifest.
	Returns ``(feature_vector, feature_date_id)``.
	"""

	prior_days = int(rec.get_required_previous_days())
	day_change_centered = rec.get_day_change_centered()
	current_day_change = rec.get_current_day_change_used()
	volume_ratio = rec.get_volume_ratio_used()
	vwap_ratio = rec.get_vwap_ratio_used()
	tranaction_ratio = rec.get_tranaction_ratio_used()
	expected_input_size = int(rec.input_size)

	print(
		"Fetching grouped features from database for model-based recommendation "
		"step..."
	)

	dates, X_flat, _Y = fetch_grouped_by_date(
		dbinfo_file=dbinfo_file,
		day_change_centered=day_change_centered,
		current_day_change=current_day_change,
		volume_ratio=volume_ratio,
		vwap_ratio=vwap_ratio,
		tranaction_ratio=tranaction_ratio,
	)
	if dates.size == 0 or X_flat.size == 0:
		raise RuntimeError(
			"No data returned from fetch_grouped_by_date; cannot build feature vector."
		)

	# Choose index: explicit date_id if provided, else latest
	if date_id is not None:
		# Find the index of the specified date_id in the dates array
		matches = np.where(dates == int(date_id))[0]
		if matches.size == 0:
			raise ValueError(
				f"Requested date_id {date_id} not found in available dates."
			)
		date_idx = int(matches[0])
	else:
		date_idx = int(len(dates) - 1)

	feature_date_id = int(dates[date_idx])

	print(
		"Building feature vector for latest data point: "
		f"date_id {feature_date_id}, prior_days={prior_days}, "
		f"day_change_centered={day_change_centered}...",
	)

	# Always use [current + priors] layout; this matches the training provider.
	feat_vec = concat_features_current_then_prior(X_flat, date_idx, prior_days)
	flat_size = int(np.asarray(feat_vec).size)
	if flat_size != expected_input_size:
		raise ValueError(
			"Feature vector size mismatch for latest date: "
			f"expected {expected_input_size}, got {flat_size}. "
			"Check that the manifest feature flags match the DB features."
		)

	return feat_vec, feature_date_id


def main(argv: Optional[list[str]] = None) -> int:
	parser = _build_arg_parser()
	args = parser.parse_args(argv)

	model_path = args.model_dir
	model_dir_path = Path(model_path)
	if not model_dir_path.exists():
		parser.error(f"Model path does not exist: {model_path}")

	rec = StockRecommenderFromModel(
		model_path,
		device=args.device,
		ensemble_normalize=bool(getattr(args, "ensemble_normalize", False)),
	)

	try:
		feat_vec, feature_date_id = _build_feature_vector_for_latest_date(
			rec,
			dbinfo_file=args.dbinfo_file,
			date_id=args.date_id,
		)
	except Exception as e:
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
	try:
		rec_file.write_text("\n".join(rec_output_lines) + "\n", encoding="utf-8")
		print(f"Recommendations saved to {rec_file}")
	except Exception as e:  # pragma: no cover - defensive logging
		print(f"Failed to write recommendations file {rec_file}: {e}")
		return 1

	return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
	raise SystemExit(main())
