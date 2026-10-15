from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from stockguidance.database.repositories.mysql_sessions import Connection


class _LegacyConnect(Protocol):
	def __call__(self, *, dbinfo_file: str) -> Connection: ...


class LegacyMySQLConnectionFactory:
	"""Temporary connector only; importing it does not load the legacy driver."""

	def __call__(self, database_config: Path) -> Connection:
		connect = cast(_LegacyConnect, import_module("database.db_connect").get_connection)
		return connect(dbinfo_file=str(database_config))
