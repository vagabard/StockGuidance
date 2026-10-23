from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
from typing import Any, Callable

from stockguidance.performance.stage14 import Stage14Error, _java_classpath, _json, _sha256, _write_json
from stockguidance.market_data.identity import content_id


class JavaPipeline:
	def __init__(self, workers: int = 3, writers: int = 1, heap: str = "64g", auto_tune: bool = False) -> None:
		if type(workers) is not int or not 1 <= workers <= 128 or type(writers) is not int or not 1 <= writers <= 128:
			raise Stage14Error("Pipeline worker counts must be between one and 128")
		if not re.fullmatch(r"[1-9][0-9]*[mg]", heap):
			raise Stage14Error("Java heap must be an integer followed by m or g")
		self.configuration = {"pipeline": "shared_java_db_copy_v1", "preparers": workers, "writers": writers, "java_heap": f"-Xmx{heap}", "native_threads": 1, "source_batch_sessions": 20, "source_readers": 1, "queued_sessions": 3}
		self.process: subprocess.Popen[str] | None = None
		self.responses: Queue[str | None] = Queue()
		self.identity: tuple[Path, Path, Path] | None = None
		self.reader: Thread | None = None
		self.auto_tune = auto_tune
		self.tuning: dict[str, Any] | None = None
		self.before_publication: Callable[[], None] | None = None

	def __enter__(self) -> JavaPipeline:
		return self

	def __exit__(self, *_args: Any) -> None:
		self.close()

	def _read_responses(self) -> None:
		assert self.process is not None and self.process.stdout is not None
		try:
			for line in self.process.stdout:
				if line.strip():
					self.responses.put(line)
		finally:
			self.responses.put(None)

	def _start(self, dbinfo_file: Path, manifest_path: Path, checkpoint_directory: Path) -> None:
		identity = (dbinfo_file.resolve(), manifest_path.resolve(), checkpoint_directory.resolve())
		if self.identity is not None and identity != self.identity:
			raise Stage14Error("Cannot reuse JavaDbCopy across different inputs")
		if self.process is None:
			self.identity = identity
			if self.auto_tune:
				key = self._tuning_key(manifest_path)
				tuning_path = checkpoint_directory.parent / "thread-tuning.json"
				if tuning_path.exists():
					cached = _json(tuning_path)
					if cached.get("identity") == key and cached.get("status") == "complete":
						self.tuning = cached
						self.configuration["preparers"] = int(cached["selected_workers"])
			command = ["java", "--add-modules=jdk.incubator.vector", str(self.configuration["java_heap"]), "-cp", _java_classpath(), "stockguidance.migration.JavaZScorePipelineMain", "--manifest", str(identity[1]), "--dbinfo-file", str(identity[0]), "--production", "true", "--checkpoint-directory", str(identity[2]), "--workers", str(self.configuration["preparers"]), "--writers", str(self.configuration["writers"]), "--serve", "true", "--parent-pid", str(os.getpid())]
			environment = dict(os.environ)
			environment.update({key: "1" for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")})
			try:
				self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=environment)
			except OSError as error:
				raise Stage14Error("Java pipeline could not start") from error
			self.reader = Thread(target=self._read_responses, daemon=True)
			self.reader.start()

	def _tuning_key(self, manifest_path: Path) -> str:
		root = Path(__file__).resolve().parents[3] / "java/target/classes/stockguidance/migration"
		return content_id({"manifest_sha256": _sha256(manifest_path), "classes": {path.name: _sha256(path) for path in sorted(root.glob("*.class"))}, "max_workers": 10, "writers": self.configuration["writers"]})

	def tune_only(self, dbinfo_file: Path, manifest_path: Path, checkpoint_directory: Path, timeout: float = 7200) -> dict[str, Any]:
		self._start(dbinfo_file, manifest_path, checkpoint_directory)
		if self.tuning is None:
			manifest = _json(manifest_path)
			sessions = [str(target["session"]) for target in manifest["targets"]][-10:]
			print("Measuring 1-10 calculation workers on cached real inputs; utility and writer threads are excluded.", flush=True)
			marker = checkpoint_directory.parent / "tuning-active.json"
			_write_json(marker, {"status": "in_progress", "manifest_id": manifest["manifest_id"], "progress_path": str(checkpoint_directory / "tuning-progress.json")})
			result = self._exchange("tune:" + ",".join(sessions), timeout)
			result["identity"] = self._tuning_key(manifest_path)
			self.tuning = result
			self.configuration["preparers"] = int(result["selected_workers"])
			_write_json(checkpoint_directory.parent / "thread-tuning.json", result)
			_write_json(marker, {"status": "complete", "manifest_id": manifest["manifest_id"]})
		print(f"Selected {self.configuration['preparers']} calculation workers; 1 source loader and {self.configuration['writers']} publication workers counted separately.", flush=True)
		return self.tuning

	def __call__(self, dbinfo_file: Path, manifest_path: Path, sessions: tuple[str, ...], checkpoint_directory: Path, timeout: float) -> dict[str, Any]:
		self._start(dbinfo_file, manifest_path, checkpoint_directory)
		if self.auto_tune and self.tuning is None:
			self.tune_only(dbinfo_file, manifest_path, checkpoint_directory, timeout)
		return self._exchange(",".join(sessions), timeout)

	def _exchange(self, request: str, timeout: float) -> dict[str, Any]:
		try:
			assert self.process is not None
			assert self.process.stdin is not None
			if not request.startswith("tune:") and self.before_publication is not None:
				self.before_publication()
			self.process.stdin.write(request + "\n")
			self.process.stdin.flush()
			line = self.responses.get(timeout=timeout)
			if line is None:
				raise Stage14Error("Java pipeline exited without completing the chunk")
			result = json.loads(line)
		except (OSError, Empty, ValueError) as error:
			self.close()
			raise Stage14Error("Java pipeline failed or exceeded the chunk timeout") from error
		if not isinstance(result, dict) or result.get("failed") or result.get("status") != "complete":
			raise Stage14Error("Java pipeline rejected the chunk")
		return result

	def close(self) -> None:
		process = self.process
		if process is None:
			return
		try:
			if process.poll() is None and process.stdin is not None:
				try:
					process.stdin.write("close\n")
					process.stdin.flush()
				except OSError:
					pass
			try:
				process.wait(timeout=5)
			except subprocess.TimeoutExpired:
				process.terminate()
				try:
					process.wait(timeout=5)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait(timeout=5)
		finally:
			if self.reader is not None:
				self.reader.join(timeout=2)
			for stream in (process.stdin, process.stdout):
				if stream is not None:
					try:
						stream.close()
					except OSError:
						pass
