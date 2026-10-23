from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any

from stockguidance.performance.stage14 import _json, _write_json


def estimate(completed: int, baseline: int, elapsed: float, total: int) -> dict[str, Any]:
	rate = (completed - baseline) / elapsed if elapsed > 0 and completed > baseline else None
	return {"completed_series": completed, "total_series": total, "series_per_second": rate, "remaining_seconds": (total - completed) / rate if rate else None, "estimate_scope": "population including capture/publication overhead; final independent audits excluded"}


class ProgressReporter:
	def __init__(self, output: Path, interval: float = 15) -> None:
		self.output = output
		self.interval = interval
		self.stop = Event()
		self.thread: Thread | None = None
		self.started = monotonic()
		self.baseline: int | None = None
		self.manifest_id: str | None = None

	def __enter__(self) -> ProgressReporter:
		self.thread = Thread(target=self._run, daemon=True)
		self.thread.start()
		return self

	def __exit__(self, *_args: Any) -> None:
		self.stop.set()
		if self.thread is not None:
			self.thread.join(timeout=self.interval + 1)

	def snapshot(self) -> dict[str, Any]:
		state = _json(self.output)
		manifest_id = str(state["manifest_id"])
		completed = len(state.get("series", ()))
		accepted = {int(chunk["index"]) for chunk in state.get("chunks", ())}
		phase = "auditing" if completed == int(state["scope"]["requested_sessions"]) * 4 else "preparing"
		if state.get("status") == "complete":
			phase = "complete"
		root = self.output.parent / "stage14"
		journal_path = root / f"batches-{manifest_id[:12]}.json"
		batch_index = None
		if journal_path.exists():
			journal = _json(journal_path)
			active = [batch for batch in journal["batches"] if batch["status"] == "in_progress"]
			if active:
				batch = active[0]
				batch_index = batch["index"]
				attempt = batch["attempts"][-1]
				phase = attempt["phase"]
				progress = root / f"java-checkpoints-{manifest_id[:12]}" / "pipeline-progress.json"
				if phase == "publishing" and batch_index not in accepted and progress.exists():
					import json
					started_at = datetime.fromisoformat(attempt["started_at"]).timestamp()
					if progress.stat().st_mtime >= started_at:
						rows = json.loads(progress.read_text(encoding="utf-8"))
						completed += sum(1 for row in rows if row["session"] in batch["sessions"])
		if self.manifest_id != manifest_id or self.baseline is None or completed < self.baseline:
			self.manifest_id = manifest_id
			self.baseline = completed
			self.started = monotonic()
		result = estimate(completed, self.baseline, monotonic() - self.started, int(state["scope"]["requested_sessions"]) * 4)
		tuning_path = root / "tuning-active.json"
		if tuning_path.exists():
			tuning = _json(tuning_path)
			if phase == "prepared" and tuning.get("status") == "in_progress" and tuning.get("manifest_id") == manifest_id:
				phase = "tuning calculation workers (1–10)"
				trial_path = Path(tuning["progress_path"])
				if trial_path.exists():
					trial = _json(trial_path)
					phase += f"; last trial: {trial['workers']} workers, {trial['seconds']:.1f}s"
		if phase.startswith("tuning") or phase == "rolling_back":
			self.baseline = completed
			self.started = monotonic()
			result = estimate(completed, completed, 0, int(state["scope"]["requested_sessions"]) * 4)
		result.update({"phase": phase, "batch": batch_index, "manifest_id": manifest_id, "updated_at": datetime.now(timezone.utc).isoformat(), "calculation_workers": state.get("configuration", {}).get("preparers"), "utility_threads_excluded": True})
		return result

	def _run(self) -> None:
		while not self.stop.wait(self.interval):
			try:
				value = self.snapshot()
				_write_json(self.output.parent / "stage14/progress.json", value)
				remaining = value["remaining_seconds"]
				eta = f"{remaining / 3600:.2f} hours" if remaining is not None else "estimating"
				print(f"Stage 14: {value['completed_series']}/{value['total_series']} series; batch {value['batch']}; {value['phase']}; population ETA {eta}", flush=True)
			except (OSError, ValueError, KeyError, TypeError):
				continue
