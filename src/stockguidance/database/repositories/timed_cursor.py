from collections.abc import Sequence

from stockguidance.database.write_connections import WriteCursor
from stockguidance.workflows.progress import ProgressReporter


class TimedCursor:
	def __init__(self, cursor: WriteCursor, reporter: ProgressReporter) -> None:
		self.cursor, self.reporter = cursor, reporter
		self.operation = "database"

	def execute(self, operation: str, parameters: tuple[object, ...]) -> object:
		self.operation = " ".join(operation.split())[:100]
		return self.reporter.measure("database execute " + self.operation, lambda: self.cursor.execute(operation, parameters))

	def executemany(self, operation: str, parameters: Sequence[tuple[object, ...]]) -> object:
		return self.reporter.measure(f"database write batch ({len(parameters)} rows)", lambda: self.cursor.executemany(operation, parameters))

	def fetchall(self) -> Sequence[Sequence[object]]:
		rows = self.reporter.measure("database fetch " + self.operation, self.cursor.fetchall)
		self.reporter.message(f"database returned {len(rows)} row(s)")
		return rows

	def close(self) -> None:
		self.cursor.close()
