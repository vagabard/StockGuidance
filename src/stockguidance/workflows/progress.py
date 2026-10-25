from contextlib import contextmanager
from time import perf_counter
import sys
from collections.abc import Iterator
from typing import TextIO
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class ProgressReporter:
	def __init__(self, *, enabled: bool = False, stream: TextIO = sys.stderr) -> None:
		self.enabled = enabled
		self.stream = stream
		self.started = perf_counter()

	def finish(self, *, successful: bool, failures: int = 0) -> None:
		if self.enabled:
			status = "successful" if successful else f"completed with {failures} failure(s)"
			self.message(f"update {status}; total elapsed {perf_counter() - self.started:.3f}s")

	def message(self, text: str) -> None:
		if self.enabled:
			print(f"[updater] {text}", file=self.stream, flush=True)

	@contextmanager
	def phase(self, name: str) -> Iterator[None]:
		if not self.enabled:
			yield
			return
		started = perf_counter()
		self.message(f"{name}...")
		try:
			yield
		except Exception as error:
			self.message(f"{name} failed after {perf_counter() - started:.3f}s: {type(error).__name__}: {error}")
			raise
		else:
			self.message(f"{name} completed in {perf_counter() - started:.3f}s")

	def sessions(self, completed: int, total: int, *, downloaded: int = 0, failures: int = 0, started: float | None = None) -> None:
		if not self.enabled:
			return
		elapsed = 0.0 if started is None else perf_counter() - started
		remaining = 0.0 if completed == 0 or total <= completed else elapsed / completed * (total - completed)
		self.message(f"sessions {completed}/{total}; downloaded {downloaded}; failures {failures}; elapsed {elapsed:.1f}s; ETA {remaining:.1f}s")

	def measure(self, name: str, operation: Callable[[], T]) -> T:
		with self.phase(name):
			return operation()
