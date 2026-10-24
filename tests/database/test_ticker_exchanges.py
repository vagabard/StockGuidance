from pathlib import Path

import pytest

from stockguidance.database.repositories.mysql_tickers import read_ticker_calendar, read_ticker_exchanges
from stockguidance.database.table_names import load_table_names


class Cursor:
	def __init__(self, rows):
		self.rows = rows
		self.query = None

	def execute(self, query, parameters):
		self.query = query
		self.parameters = parameters

	def fetchall(self):
		return self.rows

	def close(self):
		pass


class Connection:
	def __init__(self, rows):
		self.cursor_value = Cursor(rows)

	def cursor(self):
		return self.cursor_value

	def close(self):
		pass


def test_reads_database_exchange_codes_for_requested_tickers():
	connection = Connection([(3, "NASDAQ"), (17, "XNYS")])
	source = type("Source", (), {"database_config": Path("dbinfo.id")})()

	assert read_ticker_exchanges(source, lambda _: connection, (3, 17), load_table_names()) == {3: "XNAS", 17: "XNYS"}

	assert connection.cursor_value.parameters == (3, 17)
	with pytest.raises(ValueError, match="one database exchange"):
		read_ticker_calendar(source, lambda _: connection, (3, 17), load_table_names())


def test_reads_and_canonicalizes_one_exchange():
	connection = Connection([(3, "NASDAQ")])
	source = type("Source", (), {"database_config": Path("dbinfo.id")})()

	assert read_ticker_exchanges(source, lambda _: connection, (3,), load_table_names()) == {3: "XNAS"}
