from collections.abc import Iterator
from contextlib import contextmanager

from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnectionFactory


@contextmanager
def market_update_lock(source: SourceConfig, connect: WriteConnectionFactory) -> Iterator[None]:
	connection = connect(source.database_config)
	cursor = connection.cursor()
	locked = False
	try:
		cursor.execute("SELECT GET_LOCK('stockguidance_daily_maintenance', 0)", ())
		locked = cursor.fetchall() == [(1,)]
		if not locked:
			raise RuntimeError("Another market-data updater is running on this database server")
		yield
	finally:
		try:
			if locked:
				cursor.execute("SELECT RELEASE_LOCK('stockguidance_daily_maintenance')", ())
				cursor.fetchall()
		finally:
			cursor.close()
			connection.close()
