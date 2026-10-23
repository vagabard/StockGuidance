from __future__ import annotations

from concurrent.futures import Future, ProcessPoolExecutor, wait, FIRST_COMPLETED
from decimal import Decimal
from functools import lru_cache
import json
import multiprocessing as mp
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.performance.candidates import _exact_derived_rows


@lru_cache(maxsize=6)
def _archive(path: str, expected_hash: str) -> tuple[ExactArchiveBar, ...]:
	bars, actual_hash = read_exact_archive(Path(path))
	if actual_hash != expected_hash:
		raise ValueError("Stage 15 derived source archive changed")
	return bars


def _validate_derived(target: dict[str, Any], files: dict[str, dict[str, Any]], stored: list[tuple[Any, ...]]) -> dict[str, Any]:
	session = target["session"]
	prior, following = target["derived_dependencies"]
	snapshots = [_archive(files[date_value]["path"], files[date_value]["file_sha256"]) for date_value in (prior, session, following)]
	expected = _exact_derived_rows(*snapshots)
	by_ticker = {str(row[0]): row[1:] for row in stored}
	missing = sorted({row.ticker for row in expected} - set(by_ticker))
	extra = sorted(set(by_ticker) - {row.ticker for row in expected})
	common = [row for row in expected if row.ticker in by_ticker]
	required = np.asarray([[np.nan if value is None else value for value in row.values] for row in common], dtype=np.float64).reshape(-1, len(DERIVED_COLUMNS))
	actual = np.asarray([[np.nan if value is None else value for value in by_ticker[row.ticker]] for row in common], dtype=np.float64).reshape(-1, len(DERIVED_COLUMNS))
	with np.errstate(invalid="ignore"):
		matched = (np.isnan(required) & np.isnan(actual)) | (np.isfinite(actual) & np.isfinite(required) & (np.abs(actual - required) <= np.maximum(1e-12, 1e-10 * np.maximum(np.abs(actual), np.abs(required)))))
	findings = [{"ticker": common[index].ticker, "columns": [DERIVED_COLUMNS[column] for column in np.flatnonzero(~matched[index])]} for index in np.flatnonzero(~matched.all(axis=1))]
	return {"session": session, "prior_session": prior, "following_session": following, "state": "complete", "expected_count": len(expected), "stored_count": len(stored), "missing_count": len(missing), "extra_count": len(extra), "missing_tickers": missing, "extra_tickers": extra, "column_difference_counts": dict(zip(DERIVED_COLUMNS, (~matched).sum(axis=0).tolist(), strict=True)), "differences": findings, "matches": not missing and not extra and not findings}


def _validate_raw(item: dict[str, Any], stored: list[tuple[Any, ...]]) -> dict[str, Any]:
	from stockguidance.performance.stage15 import _raw_result

	archive, actual_hash = read_exact_archive(Path(item["path"]))
	if actual_hash != item["file_sha256"]:
		raise ValueError("Stage 15 raw source archive changed")
	if exact_records_id(archive) != item["records_id"]:
		raise ValueError("Stage 15 raw archive record identity changed")
	actual: dict[str, ExactArchiveBar] = {}
	duplicates = []
	for stored_row in stored:
		bar = ExactArchiveBar(str(stored_row[1]), Decimal(str(stored_row[2])), Decimal(str(stored_row[3])), Decimal(str(stored_row[4])), Decimal(str(stored_row[5])), Decimal(str(stored_row[6])), None if stored_row[7] is None else Decimal(str(stored_row[7])), stored_row[8])
		if bar.ticker in actual:
			duplicates.append(bar.ticker)
		actual[bar.ticker] = bar
	result = _raw_result(item["session"], archive, actual)
	result["archive_id"] = exact_records_id(archive)
	result["database_id"] = exact_records_id(tuple(actual.values()))
	result["duplicate_tickers"] = duplicates
	result["matches"] = bool(result["matches"] and not duplicates)
	result["adoption_records_id"] = item.get("records_id")
	return result


def run_derived(connection: Any, names: Any, manifest: dict[str, Any], date_ids: dict[str, int], directory: Path, binding: dict[str, Any], workers: int, fetch_size: int, *, raw: bool = False, max_sessions: int | None = None, progress_seconds: float = 10.0) -> list[dict[str, Any]]:
	from stockguidance.performance.stage15 import _write_json

	files = {item["session"]: item for item in manifest["files"]}
	targets = sorted((item for item in manifest["files"] if item.get("valid")) if raw else manifest["targets"], key=lambda item: item["session"])
	phase = "raw" if raw else "derived"
	identity = content_id({"engine": f"stage15-{phase}-exact-decimal-numpy-v2", "binding": binding, "date_ids": date_ids})
	directory = directory / identity
	directory.mkdir(parents=True, exist_ok=True)
	results = {}
	for path in directory.glob("*.json"):
		with path.open(encoding="utf-8") as stream:
			result = json.load(stream)
		digest = result.pop("sha256", None)
		if content_id(result) != digest or result.get("identity") != identity:
			raise ValueError(f"Invalid Stage 15 derived checkpoint: {path}")
		results[result["session"]] = result
	started = perf_counter()
	last_progress = started
	initial = len(results)
	submitted = 0
	print(f"Stage 15 {phase}: {initial}/{len(targets)} sessions resumed; {workers} workers; fetch size {fetch_size}", flush=True)
	with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as executor:
		pending: dict[Future[dict[str, Any]], str] = {}
		def consume() -> None:
			nonlocal last_progress
			ready, _ = wait(pending, return_when=FIRST_COMPLETED)
			for future in ready:
				result = future.result()
				result["identity"] = identity
				result["sha256"] = content_id(result)
				_write_json(directory / f"{result['session']}.json", result)
				results[result["session"]] = result
				del pending[future]
			completed = len(results) - initial
			elapsed = perf_counter() - started
			if completed and (perf_counter() - last_progress >= progress_seconds or len(results) == len(targets)):
				print(f"Stage 15 {phase}: {len(results)}/{len(targets)} sessions; elapsed {elapsed:.0f}s; ETA {elapsed / completed * (len(targets) - len(results)):.0f}s", flush=True)
				last_progress = perf_counter()
		for target in targets:
			session = target["session"]
			if session in results:
				continue
			if max_sessions is not None and submitted >= max_sessions:
				break
			dependencies = target.get("derived_dependencies", [])
			if not raw and (len(dependencies) != 2 or any(not files.get(value, {}).get("valid") for value in dependencies)):
				results[session] = {"session": session, "state": "unavailable", "matches": False, "reason": "missing_or_invalid_retained_dependency", "dependencies": dependencies}
				continue
			cursor = connection.cursor()
			try:
				if raw:
					cursor.execute(f"SELECT r.date_id, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM {quote_identifier(names.prices)} r JOIN {quote_identifier(names.tickers)} s ON s.id=r.ticker_id WHERE r.date_id=%s", (date_ids[session],))
				else:
					cursor.execute(f"SELECT s.label, {', '.join('d.' + column for column in DERIVED_COLUMNS)} FROM {quote_identifier(names.deltas)} d JOIN {quote_identifier(names.tickers)} s ON s.id=d.ticker_id WHERE d.date_id=%s", (date_ids[session],))
				stored = []
				while True:
					batch = cursor.fetchmany(fetch_size)
					if not batch:
						break
					stored.extend(batch)
			finally:
				cursor.close()
			if raw:
				future = executor.submit(_validate_raw, target, stored)
			else:
				future = executor.submit(_validate_derived, target, {value: files[value] for value in (dependencies[0], session, dependencies[1])}, stored)
			pending[future] = session
			submitted += 1
			if len(pending) >= workers * 2:
				consume()
		while pending:
			consume()
	return [results[session] for session in sorted(results)]
