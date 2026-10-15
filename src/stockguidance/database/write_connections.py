from importlib import import_module
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from stockguidance.database.repositories.mysql_sessions import Cursor


class WriteCursor(Cursor, Protocol):
	def executemany(self, operation: str, parameters: Sequence[tuple[object, ...]]) -> object: ...


class WriteConnection(Protocol):
	def cursor(self) -> WriteCursor: ...
	def commit(self) -> None: ...
	def rollback(self) -> None: ...
	def close(self) -> None: ...


class WriteConnectionFactory(Protocol):
	def __call__(self, database_config: Path) -> WriteConnection: ...


class _LegacyConnect(Protocol):
	def __call__(self, *, dbinfo_file: str) -> WriteConnection: ...


class LegacyWriteConnectionFactory:
	def __call__(self, database_config: Path) -> WriteConnection:
		connect = cast(_LegacyConnect, import_module("database.db_connect").get_connection)
		return connect(dbinfo_file=str(database_config))
