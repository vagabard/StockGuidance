from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
import math
import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from queue import Empty, Full
from threading import Thread
from time import perf_counter
import traceback
from typing import Any, Callable

import numpy as np

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json


ENGINE_VERSION = "independent-numpy-shared-v2"
SERIES = (("price", 20), ("price", 60), ("price_delta", 20), ("price_delta", 60))
CELL = np.dtype([("close", "f8"), ("ratio", "f8"), ("close_present", "?"), ("ratio_present", "?"), ("scores", "f4", (4,)), ("statuses", "i1", (4,))])


@dataclass(frozen=True)
class PipelineOptions:
	workers: int = 5
	vector_size: int = 256
	fetch_size: int = 1000
	sessions_per_batch: int = 128
	cache_slots: int = 2
	max_batches: int | None = None
	progress_seconds: float = 10.0

	def __post_init__(self) -> None:
		for name, lower, upper in (("workers", 1, 10), ("vector_size", 1, 1000), ("fetch_size", 1, 10000), ("sessions_per_batch", 1, 256), ("cache_slots", 1, 3)):
			value = getattr(self, name)
			if type(value) is not int or not lower <= value <= upper:
				raise ValueError(f"Independent z-score validation {name} must be between {lower} and {upper}")
		if self.max_batches is not None and (type(self.max_batches) is not int or self.max_batches < 1):
			raise ValueError("Independent z-score validation max_batches must be positive")
		if not math.isfinite(self.progress_seconds) or self.progress_seconds <= 0:
			raise ValueError("Independent z-score validation progress interval must be positive")


def _reset(cache: np.ndarray) -> None:
	cache["close"].fill(np.nan)
	cache["ratio"].fill(np.nan)
	cache["close_present"].fill(False)
	cache["ratio_present"].fill(False)
	cache["scores"].fill(np.nan)
	cache["statuses"].fill(-1)


def _plans(manifest: dict[str, Any], options: PipelineOptions) -> list[dict[str, Any]]:
	targets = sorted(manifest["targets"], key=lambda item: item["session"])
	plans: list[dict[str, Any]] = []
	for offset in range(0, len(targets), options.sessions_per_batch):
		selected = targets[offset:offset + options.sessions_per_batch]
		dates = sorted({str(session) for target in selected for session in target["zscore_dependencies"]} | {str(target["session"]) for target in selected})
		indexes = {session: index for index, session in enumerate(dates)}
		plans.append({"index": len(plans), "targets": selected, "dates": dates, "dependency_indexes": [[indexes[str(session)] for session in target["zscore_dependencies"]] for target in selected], "target_indexes": [indexes[str(target["session"])] for target in selected]})
	return plans


def _numeric(value: Any) -> float:
	return np.nan if value is None else float(value)


def _load(connection: Any, names: Any, cache: np.ndarray, plan: dict[str, Any], tickers: tuple[int, ...], date_ids: dict[str, int], fetch_size: int, *, ticker_filter: bool = False, filter_tickers: tuple[int, ...] | None = None, progress: Any = None, stopped: Any = None) -> dict[str, Any]:
	_reset(cache)
	date_indexes = {date_ids[session]: index for index, session in enumerate(plan["dates"]) if session in date_ids}
	ticker_indexes = {identifier: index for index, identifier in enumerate(tickers)}
	target_ids = tuple(date_ids[target["session"]] for target in plan["targets"] if target["session"] in date_ids)
	queries: list[tuple[str, str, str, int | None, tuple[int, ...]]] = [(names.prices, "close", "close_present", None, tuple(date_indexes)), (names.deltas, "close_ratio_to_prior", "ratio_present", None, tuple(date_indexes))]
	resolved = names.resolve_zscore_tables((20, 60))
	for materialization_index, (family, window) in enumerate(SERIES):
		table = resolved[window].price if family == "price" else resolved[window].price_delta
		queries.append((table, "zscore, status", "", materialization_index, target_ids))
	rows = 0
	started = perf_counter()
	for table, columns, presence, series_index, identifiers in queries:
		if not identifiers:
			continue
		cursor = connection.cursor()
		try:
			filtered = filter_tickers if filter_tickers is not None else tickers if ticker_filter else ()
			ticker_clause = f" AND ticker_id IN ({','.join('%s' for _ in filtered)})" if filtered else ""
			cursor.execute(f"SELECT date_id, ticker_id, {columns} FROM {quote_identifier(table)} WHERE date_id IN ({','.join('%s' for _ in identifiers)}){ticker_clause}", identifiers + filtered)
			while True:
				if stopped is not None and stopped.is_set():
			raise RuntimeError("Independent z-score validation cache loader cancelled")
				batch = cursor.fetchmany(fetch_size)
				if not batch:
					break
				rows += len(batch)
				if progress is not None:
					progress(rows)
				date_positions = np.fromiter((date_indexes[int(row[0])] for row in batch), dtype=np.intp, count=len(batch))
				ticker_positions = np.fromiter((ticker_indexes[int(row[1])] for row in batch), dtype=np.intp, count=len(batch))
				values = np.fromiter((_numeric(row[2]) for row in batch), dtype=np.float64, count=len(batch))
				if series_index is None:
					field = "close" if presence == "close_present" else "ratio"
					cache[field][date_positions, ticker_positions] = values
					cache[presence][date_positions, ticker_positions] = np.fromiter((row[2] is not None for row in batch), dtype=bool, count=len(batch))
				else:
					cache["scores"][date_positions, ticker_positions, series_index] = values
					cache["statuses"][date_positions, ticker_positions, series_index] = np.fromiter((int(row[3]) for row in batch), dtype=np.int8, count=len(batch))
		finally:
			cursor.close()
	return {"rows_loaded": rows, "load_seconds": perf_counter() - started}


def benchmark(connection: Any, names: Any, manifest: dict[str, Any], date_ids: dict[str, int], verified_by_id: dict[int, bool], output: Path, options: PipelineOptions) -> dict[str, Any]:
	from stockguidance.performance.stage15 import _write_json

	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id FROM {quote_identifier(names.tickers)} ORDER BY id")
		cursor.fetchall()
	finally:
		cursor.close()
	plans = _plans(manifest, options)
	plan = plans[len(plans) // 2]
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT ticker_id FROM {quote_identifier(names.prices)} WHERE date_id=%s ORDER BY ticker_id LIMIT %s", (date_ids[plan["targets"][-1]["session"]], options.vector_size))
		tickers = tuple(int(row[0]) for row in cursor.fetchall())
	finally:
		cursor.close()
	if not tickers:
		raise ValueError("Stage 15 benchmark has no active tickers")
	shape = (1, len(plan["dates"]), len(tickers))
	memory = SharedMemory(create=True, size=math.prod(shape) * CELL.itemsize)
	cache = np.ndarray(shape, dtype=CELL, buffer=memory.buf)
	try:
		print(f"Stage 15 benchmark: loading {len(plan['targets'])} real sessions x {len(tickers)} tickers, {options.fetch_size} rows per fetch", flush=True)
		loading = _load(connection, names, cache[0], plan, tickers, date_ids, options.fetch_size, ticker_filter=True)
		verified = np.asarray([verified_by_id.get(date_ids.get(session, -1), False) for session in plan["dates"]], dtype=bool)
		print("Stage 15 benchmark: running original scalar reference", flush=True)
		scalar = _validate(cache[0], plan, tickers, verified, scalar=True)
		vector = _validate(cache[0], plan, tickers, verified)
		context = mp.get_context("spawn")
		jobs = context.Queue()
		results = context.Queue()
		stopped = context.Event()
		workers = [context.Process(target=_worker, args=(memory.name, shape, jobs, results, stopped)) for _ in range(options.workers)]
		started = perf_counter()
		try:
			for worker in workers:
				worker.start()
			step = math.ceil(len(tickers) / options.workers)
			starts = list(range(0, len(tickers), step))
			for start in starts:
				end = min(start + step, len(tickers))
				jobs.put((0, plan, tickers[start:end], start, end, verified.tolist()))
			parts: list[dict[str, Any]] = []
			deadline = perf_counter() + 120
			while len(parts) < len(starts):
				try:
					part = results.get(timeout=1)
				except Empty:
					if perf_counter() > deadline or any(worker.exitcode not in (None, 0) for worker in workers):
						raise RuntimeError("Stage 15 benchmark workers failed or timed out")
					continue
				if "error" in part:
					raise RuntimeError(part["error"])
				parts.append(part)
			parallel_seconds = perf_counter() - started
		finally:
			stopped.set()
			for worker in workers:
				worker.join(timeout=5)
				if worker.is_alive():
					worker.terminate()
					worker.join()
			for queue in (jobs, results):
				queue.cancel_join_thread()
				queue.close()
		parallel_counts = np.sum([np.asarray(part["counts"]) for part in parts], axis=0).tolist()
		parallel_mismatches = sorted((item for part in parts for item in part["mismatches"]), key=lambda item: (item["session"], item["family"], item["window"], item["ticker_id"]))
		key = lambda item: (item["session"], item["family"], item["window"], item["ticker_id"])
		parity = scalar["counts"] == vector["counts"] == parallel_counts and sorted(scalar["mismatches"], key=key) == sorted(vector["mismatches"], key=key) == parallel_mismatches and scalar["actual_rows"] == vector["actual_rows"] == np.sum([part["actual_rows"] for part in parts], axis=0).tolist()
		report = {"status": "passed" if parity else "failed", "engine": ENGINE_VERSION, "scope": {"first_session": plan["targets"][0]["session"], "last_session": plan["targets"][-1]["session"], "sessions": len(plan["targets"]), "tickers": len(tickers), "rows_compared": scalar["rows_checked"]}, "loading": loading, "scalar_seconds": scalar["compute_seconds"], "vector_seconds": vector["compute_seconds"], "multiprocessing_seconds_including_spawn": parallel_seconds, "vector_speedup": scalar["compute_seconds"] / vector["compute_seconds"], "multiprocessing_speedup_including_spawn": scalar["compute_seconds"] / parallel_seconds, "workers": options.workers, "parity": parity, "mismatch_count": scalar["mismatch_count"], "mismatches": scalar["mismatches"], "timing_scope": "Same real database cache and semantic reference; shared source read reported separately; multiprocessing includes Windows spawn and result queue, excludes worker shutdown; no production writes"}
		_write_json(output, report)
		print(f"Stage 15 benchmark: scalar {scalar['compute_seconds']:.3f}s; vector {vector['compute_seconds']:.3f}s; {options.workers} processes including startup {parallel_seconds:.3f}s; parity={parity}", flush=True)
		return report
	finally:
		memory.close()
		memory.unlink()


def _reference(cache: np.ndarray, indexes: list[int], verified: np.ndarray, family: str, window: int) -> tuple[np.ndarray, np.ndarray]:
	selected = cache[np.asarray(indexes, dtype=np.intp)]
	close = selected["close"]
	issues = np.where(~selected["close_present"], 2, np.where(~np.isfinite(close) | (close <= 0), 4, 0)).astype(np.uint8)
	issues[~verified[np.asarray(indexes, dtype=np.intp)], :] = 3
	values = close
	if family == "price_delta":
		issues = issues.copy()
		issues[1:] = np.maximum(issues[1:], issues[:-1].copy())
		issues[0] = np.maximum(issues[0], 1)
		ratio = selected["ratio"]
		with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
			required = close[1:] / close[:-1]
			consistent = np.abs(ratio[1:] - required) <= np.maximum(1e-12, 1e-10 * np.maximum(np.abs(ratio[1:]), np.abs(required)))
		valid = (issues[1:] == 0)
		issues[1:] = np.where(valid & ~selected["ratio_present"][1:], 2, issues[1:])
		issues[1:] = np.where(valid & selected["ratio_present"][1:] & (~np.isfinite(ratio[1:]) | (ratio[1:] <= 0) | ~consistent), 4, issues[1:])
		values = ratio - 1.0
	selected_values = values[-window:]
	status = issues[-window:].max(axis=0)
	if len(indexes) < window:
		status = np.maximum(status, 1)
	with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
		mean = np.sum(selected_values, axis=0, dtype=np.float64) / window
		variance = np.sum((selected_values - mean) ** 2, axis=0, dtype=np.float64) / window
		scores = ((selected_values[-1] - mean) / np.sqrt(variance)).astype(np.float32)
		constant = np.all(selected_values == selected_values[0], axis=0)
		status = np.where((status == 0) & (constant | ~np.isfinite(variance) | (variance <= 0)), 5, status)
		scale = np.max(np.abs(selected_values), axis=0)
		ambiguous = (status == 0) & ((np.sqrt(variance) <= 1e6 * np.abs(np.spacing(scale))) | ~np.isfinite(scores))
	for ticker_index in np.flatnonzero(ambiguous):
		numeric = selected_values[:, ticker_index].tolist()
		mean_scalar = math.fsum(numeric) / window
		variance_scalar = math.fsum((value - mean_scalar) ** 2 for value in numeric) / window
		if not math.isfinite(variance_scalar) or variance_scalar <= 0:
			status[ticker_index] = 5
		else:
			scores[ticker_index] = (numeric[-1] - mean_scalar) / math.sqrt(variance_scalar)
	scores = np.where(status == 0, scores, np.nan).astype(np.float32)
	return status, scores


def _validate(cache: np.ndarray, plan: dict[str, Any], tickers: tuple[int, ...], verified: np.ndarray, *, scalar: bool = False) -> dict[str, Any]:
	started = perf_counter()
	counts = np.zeros((len(plan["targets"]), 4, 6), dtype=np.int64)
	actual_rows = np.zeros(4, dtype=np.int64)
	mismatches = []
	values: dict[tuple[int, int], tuple[object | None, object | None]] = {}
	if scalar:
		from stockguidance.performance.stage15 import _semantic_score

		values = {(date_index + 1, ticker): (float(cell["close"]) if cell["close_present"] else None, float(cell["ratio"]) if cell["ratio_present"] else None) for date_index, row in enumerate(cache) for ticker, cell in zip(tickers, row, strict=True)}
		date_ids = {session: index + 1 for index, session in enumerate(plan["dates"])}
		verified_ids = {index + 1: bool(value) for index, value in enumerate(verified)}
	for target_index, (target, dependencies, current_index) in enumerate(zip(plan["targets"], plan["dependency_indexes"], plan["target_indexes"], strict=True)):
		for series_index, (family, window) in enumerate(SERIES):
			if scalar:
				pairs = [_semantic_score(family, window, tuple(target["zscore_dependencies"]), values, verified_ids, date_ids, ticker) for ticker in tickers]
				expected_status = np.asarray([pair[0] for pair in pairs], dtype=np.uint8)
				expected_score = np.asarray([np.nan if pair[1] is None else pair[1] for pair in pairs], dtype=np.float32)
			else:
				expected_status, expected_score = _reference(cache, dependencies, verified, family, window)
			counts[target_index, series_index] = np.bincount(expected_status, minlength=6)
			actual_status = cache["statuses"][current_index, :, series_index]
			actual_score = cache["scores"][current_index, :, series_index]
			actual_rows[series_index] += np.count_nonzero(actual_status >= 0)
			with np.errstate(invalid="ignore"):
				score_matches = np.isfinite(actual_score) & (np.abs(actual_score - expected_score) <= np.maximum(1e-6, 1e-5 * np.maximum(np.abs(actual_score), np.abs(expected_score))))
			matched = (actual_status == expected_status) & np.where(expected_status == 0, score_matches, np.isnan(actual_score))
			for ticker_index in np.flatnonzero(~matched):
				mismatches.append({"session": target["session"], "ticker_id": tickers[ticker_index], "family": family, "window": window, "expected_status": int(expected_status[ticker_index]), "actual_status": int(actual_status[ticker_index]), "expected_score": None if not np.isfinite(expected_score[ticker_index]) else float(expected_score[ticker_index]), "actual_score": None if not np.isfinite(actual_score[ticker_index]) else float(actual_score[ticker_index]), "actual_nonfinite": bool(np.isinf(actual_score[ticker_index]))})
	return {"counts": counts.tolist(), "actual_rows": actual_rows.tolist(), "mismatches": mismatches, "mismatch_count": len(mismatches), "rows_checked": len(plan["targets"]) * len(tickers) * 4, "compute_seconds": perf_counter() - started}


def _put(queue: Any, value: Any, stopped: Any) -> bool:
	while not stopped.is_set():
		try:
			queue.put(value, timeout=0.5)
			return True
		except Full:
			continue
	return False


def _worker(memory_name: str, shape: tuple[int, ...], jobs: Any, results: Any, stopped: Any) -> None:
	memory = SharedMemory(name=memory_name)
	cache = np.ndarray(shape, dtype=CELL, buffer=memory.buf)
	cache.flags.writeable = False
	try:
		while not stopped.is_set():
			try:
				job = jobs.get(timeout=0.5)
			except Empty:
				continue
			if job is None:
				return
			slot, plan, tickers, start, end, verified = job
			result = _validate(cache[slot, :len(plan["dates"]), start:end], plan, tickers, np.asarray(verified, dtype=bool))
			result.update({"slot": slot, "session_batch": plan["index"], "ticker_start": start, "ticker_end": end})
			if not _put(results, result, stopped):
				return
	except BaseException:
		_put(results, {"error": traceback.format_exc()}, stopped)
	finally:
		memory.close()


def _eta(seconds: float) -> str:
	return str(timedelta(seconds=max(0, round(seconds))))


def run_pipeline(connection: Any, names: Any, manifest: dict[str, Any], date_ids: dict[str, int], verified_by_id: dict[int, bool], directory: Path, binding: dict[str, Any], options: PipelineOptions, *, published_metadata: list[dict[str, Any]] | None = None, metadata_validator: Callable[..., dict[str, Any]] | None = None, progress_label: str = "Independent z-score validation") -> dict[str, Any]:
	def write_json(path: Path, value: dict[str, Any]) -> None:
		atomic_write(path.resolve(), encode_json(value))

	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id FROM {quote_identifier(names.tickers)} ORDER BY id")
		tickers = tuple(int(row[0]) for row in cursor.fetchall())
	finally:
		cursor.close()
	if not tickers:
		raise ValueError("Independent z-score validation has no ticker mappings")
	plans = _plans(manifest, options)
	identity = content_id({"engine": ENGINE_VERSION, "binding": binding, "date_ids": date_ids, "tickers": tickers, "verified": verified_by_id, "vector_size": options.vector_size, "sessions_per_batch": options.sessions_per_batch})
	directory = directory / identity
	directory.mkdir(parents=True, exist_ok=True)
	completed = {}
	jobs_per_plan = math.ceil(len(tickers) / options.vector_size)
	for path in sorted(directory.glob("batch-*.json")):
		with path.open(encoding="utf-8") as stream:
			checkpoint = json.load(stream)
		digest = checkpoint.pop("sha256", None)
		if content_id(checkpoint) != digest or checkpoint.get("identity") != identity:
			raise ValueError(f"Invalid independent z-score validation checkpoint: {path}")
		key = (int(checkpoint["session_batch"]), int(checkpoint["ticker_start"]))
		if key in completed:
			raise ValueError("Duplicate independent z-score validation checkpoint")
		completed[key] = checkpoint
	pending = [(plan, start, min(start + options.vector_size, len(tickers))) for plan in plans for start in range(0, len(tickers), options.vector_size) if (plan["index"], start) not in completed]
	if options.max_batches is not None:
		pending = pending[:options.max_batches]
	started = perf_counter()
	if pending:
		context = mp.get_context("spawn")
		shape = (min(options.cache_slots, len({plan["index"] for plan, _, _ in pending})), max(len(plan["dates"]) for plan, _, _ in pending), len(tickers))
		memory = SharedMemory(create=True, size=math.prod(shape) * CELL.itemsize)
		cache = np.ndarray(shape, dtype=CELL, buffer=memory.buf)
		jobs = context.Queue(maxsize=options.workers * 2)
		results = context.Queue(maxsize=options.workers * 2)
		free_slots = context.Queue()
		stopped = context.Event()
		for slot in range(shape[0]):
			free_slots.put(slot)
		workers = [context.Process(target=_worker, args=(memory.name, shape, jobs, results, stopped)) for _ in range(options.workers)]
		loader_state: dict[str, Any] = {"phase": "starting", "rows_loaded": 0}
		outstanding: dict[int, int] = {}
		def load() -> None:
			try:
				for plan in plans:
					selected = [(start, end) for candidate, start, end in pending if candidate["index"] == plan["index"]]
					if not selected:
						continue
					while not stopped.is_set():
						try:
							slot = free_slots.get(timeout=0.5)
							break
						except Empty:
							continue
					else:
						return
					loader_state["phase"] = f"loading session batch {plan['index'] + 1}/{len(plans)}"
					base_rows = loader_state["rows_loaded"]
					def progress(rows: int) -> None:
						loader_state["rows_loaded"] = base_rows + rows
					selected_tickers = tuple(tickers[index] for start, end in selected for index in range(start, end))
					filtered = selected_tickers if len(selected_tickers) < len(tickers) / 2 else None
					_load(connection, names, cache[slot, :len(plan["dates"])], plan, tickers, date_ids, options.fetch_size, filter_tickers=filtered, progress=progress, stopped=stopped)
					outstanding[slot] = len(selected)
					verified = [verified_by_id.get(date_ids.get(session, -1), False) for session in plan["dates"]]
					for start, end in selected:
						if not _put(jobs, (slot, plan, tickers[start:end], start, end, verified), stopped):
							return
					loader_state["phase"] = "validating cached batches"
				for _ in workers:
					_put(jobs, None, stopped)
			except BaseException:
				_put(results, {"error": traceback.format_exc()}, stopped)
		loader = Thread(target=load, name="stage15-single-loader", daemon=True)
		try:
			for worker in workers:
				worker.start()
			loader.start()
			print(f"{progress_label}: {len(completed)}/{len(plans) * jobs_per_plan} batches resumed; {options.workers} workers, vector size {options.vector_size}, fetch size {options.fetch_size}, shared cache {memory.size / 2**20:.1f} MiB", flush=True)
			received = 0
			last_progress = 0.0
			last_result_at = started
			while received < len(pending):
				try:
					result = results.get(timeout=min(options.progress_seconds, 1.0))
				except Empty:
					result = None
				if result is not None:
					if "error" in result:
						raise RuntimeError(result["error"])
					slot = result.pop("slot")
					result["identity"] = identity
					result["sha256"] = content_id(result)
					write_json(directory / f"batch-{result['session_batch']:04d}-{result['ticker_start']:06d}.json", result)
					completed[(result["session_batch"], result["ticker_start"])] = result
					received += 1
					last_result_at = perf_counter()
					outstanding[slot] -= 1
					if outstanding[slot] == 0:
						free_slots.put(slot)
				elapsed = perf_counter() - started
				if elapsed - last_progress >= options.progress_seconds or received == len(pending):
					remaining = len(plans) * jobs_per_plan - len(completed)
					estimate = _eta(elapsed / received * remaining) if received >= min(jobs_per_plan, len(pending)) else "estimating after first session batch"
					if received and perf_counter() - last_result_at > max(30.0, 1.5 * elapsed / received * jobs_per_plan):
						estimate = f"re-estimating: no completed batch for {perf_counter() - last_result_at:.0f}s"
					print(f"{progress_label}: {len(completed)}/{len(plans) * jobs_per_plan} batches; {received / elapsed:.2f} batches/s; elapsed {_eta(elapsed)}; ETA {estimate}; {loader_state['phase']}; loaded {loader_state['rows_loaded']:,} rows", flush=True)
					last_progress = elapsed
				if any(worker.exitcode not in (None, 0) for worker in workers):
				raise RuntimeError("Independent z-score validation worker exited unexpectedly")
		finally:
			stopped.set()
			if loader.ident is not None:
				loader.join(timeout=30)
			for worker in workers:
				if worker.pid is None:
					continue
				worker.join(timeout=5)
				if worker.is_alive():
					worker.terminate()
					worker.join()
			for queue in (jobs, results, free_slots):
				queue.cancel_join_thread()
				queue.close()
			if not loader.is_alive():
				memory.close()
				memory.unlink()
			else:
				raise RuntimeError("Independent z-score validation loader did not stop; discard its database connection")
	targets = [target for plan in plans for target in plan["targets"]]
	expected_counts = {(date_ids[target["session"]], family, window): {code: 0 for code in range(6)} for target in targets if target["session"] in date_ids for family, window in SERIES}
	actual_rows = np.zeros(4, dtype=np.int64)
	mismatches = []
	for checkpoint in completed.values():
		plan = plans[int(checkpoint["session_batch"])]
		counts = np.asarray(checkpoint["counts"], dtype=np.int64)
		for target_index, target in enumerate(plan["targets"]):
			for series_index, (family, window) in enumerate(SERIES):
				if target["session"] not in date_ids:
					continue
				for code in range(6):
					expected_counts[(date_ids[target["session"]], family, window)][code] += int(counts[target_index, series_index, code])
		actual_rows += checkpoint["actual_rows"]
		mismatches.extend(checkpoint["mismatches"])
	complete = len(completed) == len(plans) * jobs_per_plan
	if complete:
		validator = metadata_validator
		if validator is None:
			from stockguidance.performance.stage15 import _zscore_metadata

			validator = _zscore_metadata
		metadata = validator(connection, names, manifest, {date_ids[target["session"]]: target for target in targets if target["session"] in date_ids}, expected_counts, len(tickers), published_metadata)
	else:
		metadata = {}
	return {"status": "complete" if complete else "partial", "engine": ENGINE_VERSION, "checkpoint_identity": identity, "checkpoint_directory": str(directory), "target_sessions": len(targets), "ticker_count": len(tickers), "expected_rows": len(targets) * len(tickers), "actual_rows": {f"{family}/{window}": int(actual_rows[index]) for index, (family, window) in enumerate(SERIES)}, "mismatches": mismatches, "mismatch_count": len(mismatches), "batch_count": len(completed), "total_batches": len(plans) * jobs_per_plan, "remaining_batches": len(plans) * jobs_per_plan - len(completed), "metadata": metadata, "elapsed_seconds": perf_counter() - started}
