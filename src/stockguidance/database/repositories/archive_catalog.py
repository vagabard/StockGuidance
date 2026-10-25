from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
import json
from typing import Any

from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key, encode_json


ARCHIVE_TABLE = "stockguidance_archive_files"
ARCHIVE_DDL = f"CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (source_key CHAR(64) NOT NULL, session_date DATE NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY (source_key, session_date)) ENGINE=InnoDB"


class ArchiveCatalog:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory) -> None:
		self.source, self.connect = source, connect
		self.source_key = data_configuration_key(source.data_configuration)

	@contextmanager
	def _cursor(self, *, write: bool = False) -> Iterator[Any]:
		from stockguidance.database.configuration import load_database_configuration
		from stockguidance.database.update_schema import assert_source

		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				assert_source(cursor, self.source, load_database_configuration(self.source.database_config).load_table_names())
				cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (%s, %s)", (ARCHIVE_TABLE, "stockguidance_daily_source_files"))
				if {str(row[0]): str(row[1]).lower() for row in cursor.fetchall()} != {ARCHIVE_TABLE: "innodb", "stockguidance_daily_source_files": "innodb"}:
					raise ValueError("Canonical archive metadata requires compatible InnoDB tables; run explicit schema setup")
				yield cursor
				if write:
					connection.commit()
				else:
					connection.rollback()
			except BaseException:
				connection.rollback()
				raise
			finally:
				cursor.close()
		finally:
			connection.close()

	def inventory(self) -> dict[date, dict[str, Any]]:
		with self._cursor() as cursor:
			cursor.execute(f"SELECT session_date, metadata FROM {ARCHIVE_TABLE} WHERE source_key = %s ORDER BY session_date", (self.source_key,))
			return {date.fromisoformat(str(row[0])): json.loads(str(row[1])) for row in cursor.fetchall()}

	def get(self, session_date: date) -> dict[str, Any] | None:
		with self._cursor() as cursor:
			cursor.execute(f"SELECT metadata FROM {ARCHIVE_TABLE} WHERE source_key = %s AND session_date = %s", (self.source_key, session_date))
			rows = cursor.fetchall()
			return None if not rows else json.loads(str(rows[0][0]))

	def put(self, session_date: date, metadata: dict[str, Any]) -> None:
		with self._cursor(write=True) as cursor:
			cursor.execute(f"DELETE FROM {ARCHIVE_TABLE} WHERE source_key = %s AND session_date = %s", (self.source_key, session_date))
			cursor.execute(f"INSERT INTO {ARCHIVE_TABLE} (source_key, session_date, metadata) VALUES (%s, %s, %s)", (self.source_key, session_date, encode_json(metadata).decode("utf-8")))
			if metadata.get("pending"):
				cursor.execute("DELETE FROM stockguidance_daily_source_files WHERE source_key = %s AND session_date = %s", (self.source_key, session_date))
				cursor.execute("INSERT INTO stockguidance_daily_source_files (source_key, session_date, file_size, modified_ns, download_pending) VALUES (%s, %s, 0, 0, 1)", (self.source_key, session_date))
