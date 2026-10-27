from collections.abc import Iterator
from contextlib import contextmanager

from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory, WriteCursor


class MaintenanceWriterLock:
	"""Existing MySQL advisory lock with a process-bound heartbeat."""

	NAME = "stockguidance_daily_maintenance"

	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, *, timeout_seconds: int = 0) -> None:
		if type(timeout_seconds) is not int or timeout_seconds < 0:
			raise ValueError("timeout_seconds must be a non-negative integer")
		self.source = source
		self.connect = connect
		self.timeout_seconds = timeout_seconds
		self.connection: WriteConnection | None = None
		self.cursor: WriteCursor | None = None
		self._connection_id: int | None = None

	def __enter__(self) -> "MaintenanceWriterLock":
		self.connection = self.connect(self.source.database_config)
		self.cursor = self.connection.cursor()
		try:
			self.cursor.execute("SELECT GET_LOCK(%s, %s)", (self.NAME, self.timeout_seconds))
			rows = self.cursor.fetchall()
			if rows != [(1,)]:
				raise RuntimeError("Another market-data updater is running on this database server")
			self.cursor.execute("SELECT CONNECTION_ID()", ())
			connection_rows = self.cursor.fetchall()
			if len(connection_rows) != 1 or len(connection_rows[0]) != 1 or type(connection_rows[0][0]) is not int:
				# A disposable fixture may not expose a connection ID.  MySQL does,
				# and production lock-loss detection must never silently downgrade.
				if type(getattr(self.connection, "connection", None)).__module__ != "sqlite3":
					raise RuntimeError("Maintenance lock connection identity is unavailable")
			else:
				self._connection_id = connection_rows[0][0]
			return self
		except Exception:
			self.__exit__(None, None, None)
			raise

	def assert_held(self) -> None:
		if self.cursor is None or self.connection is None:
			raise RuntimeError("Maintenance writer lock is not active")
		try:
			if self._connection_id is None:
				return
			self.cursor.execute(f"SELECT IS_USED_LOCK('{self.NAME}')", ())
			rows = self.cursor.fetchall()
			if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] != self._connection_id:
				raise RuntimeError("Maintenance writer lock was lost")
		except RuntimeError:
			raise
		except Exception as error:
			raise RuntimeError("Maintenance writer lock was lost") from error

	def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
		try:
			if self.cursor is not None and self.connection is not None and self._connection_id is not None:
				try:
					self.cursor.execute(f"SELECT RELEASE_LOCK('{self.NAME}')", ())
					self.cursor.fetchall()
				except Exception:
					pass
		finally:
			if self.cursor is not None:
				self.cursor.close()
			if self.connection is not None:
				self.connection.close()
			self.cursor = None
			self.connection = None
			self._connection_id = None


@contextmanager
def market_update_lock(source: SourceConfig, connect: WriteConnectionFactory) -> Iterator[None]:
	with MaintenanceWriterLock(source, connect):
		yield
