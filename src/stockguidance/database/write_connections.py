from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from stockguidance.database.protocols import Cursor


class WriteCursor(Cursor, Protocol):
	def executemany(self, operation: str, parameters: Sequence[tuple[object, ...]]) -> object: ...


class WriteConnection(Protocol):
	def cursor(self) -> WriteCursor: ...
	def commit(self) -> None: ...
	def rollback(self) -> None: ...
	def close(self) -> None: ...


class WriteConnectionFactory(Protocol):
	def __call__(self, database_config: Path) -> WriteConnection: ...
