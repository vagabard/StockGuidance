from stockguidance.config.source import SourceConfig, canonical_calendar_id
from stockguidance.database.repositories.mysql_sessions import ConnectionFactory
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier


def read_ticker_exchanges(source: SourceConfig, connect: ConnectionFactory, ticker_ids: tuple[int, ...], table_names: TableNames | None = None) -> dict[int, str]:
	if not ticker_ids or len(set(ticker_ids)) != len(ticker_ids) or any(type(ticker_id) is not int or ticker_id < 1 for ticker_id in ticker_ids):
		raise ValueError("Ticker exchange lookup requires unique positive ticker IDs")
	names = table_names or load_table_names()
	placeholders = ",".join("%s" for _ in ticker_ids)
	sql = f"SELECT id, exchange FROM {quote_identifier(names.table(TableRole.TICKERS))} WHERE id IN ({placeholders}) ORDER BY id"
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(sql, ticker_ids)
			rows = cursor.fetchall()
		finally:
			cursor.close()
	finally:
		connection.close()
	if len(rows) != len(ticker_ids):
		raise ValueError("One or more ticker IDs have no database mapping")
	result: dict[int, str] = {}
	for row in rows:
		if len(row) != 2 or type(row[0]) is not int or row[0] < 1 or not isinstance(row[1], str) or not row[1].strip():
			raise ValueError("Ticker exchange mapping is incomplete")
		result[row[0]] = canonical_calendar_id(row[1])
	if set(result) != set(ticker_ids):
		raise ValueError("Ticker exchange mapping is duplicated or incomplete")
	return result


def read_ticker_calendar(source: SourceConfig, connect: ConnectionFactory, ticker_ids: tuple[int, ...], table_names: TableNames | None = None) -> str:
	exchanges = set(read_ticker_exchanges(source, connect, ticker_ids, table_names).values())
	if len(exchanges) != 1:
		raise ValueError("Requested ticker IDs must belong to one database exchange")
	return next(iter(exchanges))
