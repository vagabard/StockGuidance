from __future__ import annotations

import os
import random
import threading
from datetime import datetime
from typing import Optional, Sequence

import numpy as np

_CACHE_DEBUG_VALUE = False
_CACHE_DEBUG_VALUE_SAVED = False

def _infer_dims(X_flat: np.ndarray, labels: Optional[np.ndarray]) -> tuple[int, int]:
	"""Return (T, F) inferred from X_flat and labels.

	T is taken from labels width when available; otherwise we try to infer by
	finding the greatest T that divides X_flat.shape[1]. If ambiguous, fall back
	to 1.
	"""
	TF = int(X_flat.shape[1])
	if labels is not None and labels.ndim == 2 and labels.shape[1] > 0:
		T = int(labels.shape[1])
		if T > 0 and TF % T == 0:
			return T, TF // T
	# Fallback: try to find a plausible T by scanning divisors up to 5000
	for T in range(5000, 0, -1):
		if TF % T == 0:
			return T, TF // T
	return 1, TF


def dump_feature_window(
	*,
	X_flat: np.ndarray,
	dates: np.ndarray,
	labels: Optional[np.ndarray],
	d: int,
	num_prior: int,
	current_day_change_used: bool,
	ticker_id: Optional[int] = None,
	header_note: str = "",
	feature_names: Optional[Sequence[str]] = None,
	vec_size: Optional[int] = None,
) -> None:
	"""Print a human-readable dump of one ticker's feature window.

	Prints:
	  - chosen ticker_id
	  - all date_ids in the window (current d first, then prior days)
	  - per-day feature slice for that ticker (full vector)
	  - change_in_price_over_day if available among features
	  - training label for the chosen ticker at the current day (d)
	"""
	try:
		# Collect output lines to mirror to stdout and a log file
		lines: list[str] = []

		T, F = _infer_dims(X_flat, labels)
		if T <= 0 or F <= 0:
			_feature_debug_write("[feature-debug] Could not infer T/F; skipping dump.")
			return

		# Choose ticker
		if ticker_id is None:
			ticker_id = random.randint(1, T)
		t_idx = ticker_id - 1
		if t_idx < 0 or t_idx >= T:
			_feature_debug_write(f"[feature-debug] ticker_id {ticker_id} out of range 1..{T}")
			return

		# Determine whether current day is included based on vec_size if provided
		include_current = True
		if vec_size is not None:
			T, F_local = T, F
			expected_with_current = (num_prior + 1) * T * F_local
			expected_prior_only = (num_prior) * T * F_local
			if vec_size == expected_prior_only and num_prior > 0:
				include_current = False

		# Build window indices and date_ids in order (most recent first)
		if include_current:
			rng = range(0, max(0, num_prior) + 1)
		else:
			rng = range(1, max(0, num_prior) + 1)
		indices = [d - off for off in rng]
		indices = [i for i in indices if 0 <= i < int(dates.shape[0])]
		date_ids = [int(dates[i]) for i in indices]

		# Header
		note = f" ({header_note})" if header_note else ""
		lines.append("\n=== Feature Window Dump" + note + " ===")
		lines.append(f"ticker_id: {ticker_id}")
		lines.append(f"date_id window (current first): {date_ids}")
		lines.append(f"per-ticker feature width F: {F}; tickers T: {T}")

		# Column names if provided
		if feature_names is not None and len(feature_names) == F:
			lines.append("feature columns: " + ", ".join(str(n) for n in feature_names))

		# Print per-day features for this ticker
		for i in indices:
			base = t_idx * F
			feats = X_flat[i, base : base + F]
			did = int(dates[i])
			with np.printoptions(precision=6, suppress=True, nanstr="NaN", linewidth=200):
				lines.append(f"features for (ticker_id={ticker_id}, date_id={did}): {feats}")

		# Attempt to print change_in_price_over_day if present
		# Convention: features are [close_ratio_to_prior, (optional) change_in_price_over_day, ...]
		if current_day_change_used and F >= 2:
			change_vals = []
			for i in indices:
				base = t_idx * F
				val = X_flat[i, base + 1]
				change_vals.append(float(val))
			lines.append(f"change_in_price_over_day over window: {change_vals}")

		# Print label for the most recent day included
		label_index = d if include_current else (d - 1)
		if labels is not None and labels.ndim == 2 and 0 <= label_index < labels.shape[0] and 0 <= t_idx < labels.shape[1]:
			label_val = float(labels[label_index, t_idx])
			lines.append(f"training label for most recent window day (date_id={int(dates[label_index])}): {label_val}")
		else:
			lines.append("label not available for current configuration")

		lines.append("=== End Feature Window Dump ===\n")

		# Emit lines
		for ln in lines:
			_feature_debug_write(ln)
	except Exception as e:
		_feature_debug_write(f"[feature-debug] error while dumping feature window: {e}")


_log_lock = threading.Lock()
_log_file_path_cached: Optional[str] = None


def _get_log_path() -> str:
	global _log_file_path_cached
	if _log_file_path_cached is not None:
		return _log_file_path_cached
	# Default: put the log in the project root or current working directory
	# Filename fixed as requested
	path = os.path.join(os.getcwd(), "debug_feature.log")
	_log_file_path_cached = path
	return path


def env_feature_debug_enabled() -> bool:
	# Primary lookup
	global _CACHE_DEBUG_VALUE, _CACHE_DEBUG_VALUE_SAVED
	if not _CACHE_DEBUG_VALUE_SAVED:
		val = os.getenv("STOCK_DEBUG_FEATURE_VECTOR")
		if val is None:
			# Fallback: some IDEs/configs accidentally include leading/trailing spaces in env var names
			for k, v in os.environ.items():
				try:
					if k.strip() == "STOCK_DEBUG_FEATURE_VECTOR":
						val = v
						break
				except Exception:
					continue
		if val is None:
			val = "0"
		val = str(val).strip().lower()
		_CACHE_DEBUG_VALUE =  val in ("1", "true", "yes", "on")
		_CACHE_DEBUG_VALUE_SAVED = True
	return _CACHE_DEBUG_VALUE


def _feature_debug_write(message: str) -> None:
	"""Write a feature-debug message to stdout and, if enabled, to a log file.

	The goal is to mirror all prints into debug_feature.log when the environment
	flag is turned on, with minimal overhead when disabled.
	"""
	# Always print to stdout to preserve existing behavior when called
	try:
		print(message)
	except Exception:
		# Be robust: ignore printing failures
		pass

	if not env_feature_debug_enabled():
		return

	try:
		log_path = _get_log_path()
		ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		line = f"[{ts}] {message}\n"
		with _log_lock:
			# Ensure the directory exists if a path with dirs is provided
			os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
			with open(log_path, "a", encoding="utf-8") as f:
				f.write(line)
	except Exception:
		# Logging should never break main flow
		pass
