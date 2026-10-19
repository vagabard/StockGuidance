from collections.abc import Iterable

from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import source_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier


_UPDATE_TABLE_ROLES = (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS)


def _validate_windows(windows: Iterable[int]) -> tuple[int, ...]:
	values = tuple(windows)
	if any(type(window) is not int or window < 2 for window in values) or len(set(values)) != len(values) or not values:
		raise ValueError("Z-score windows must be distinct integers of at least 2")
	return values


def update_schema_statements(table_names: TableNames) -> tuple[str, ...]:
	return (
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.source)} (id INT PRIMARY KEY, source_key VARCHAR(64) NOT NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.sessions)} (id INT AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.tickers)} (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(16) NOT NULL UNIQUE) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.prices)} (ticker_id INT NOT NULL, date_id INT NOT NULL, open DECIMAL(19,4), high DECIMAL(19,4), low DECIMAL(19,4), close DECIMAL(19,4), volume BIGINT, vwap DECIMAL(19,4), transactions BIGINT, PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.deltas)} (ticker_id INT NOT NULL, date_id INT NOT NULL, " + ", ".join(f"{column} DOUBLE NULL" for column in DERIVED_COLUMNS) + ", PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.imports)} (session_date DATE PRIMARY KEY, raw_id VARCHAR(64) NOT NULL, available_at VARCHAR(40) NOT NULL, derived_dependency_id VARCHAR(64) NULL, derived_content_id VARCHAR(64) NULL) ENGINE=InnoDB",
	)


def zscore_schema_statements(table_names: TableNames, windows: Iterable[int]) -> tuple[str, ...]:
	selected_windows = _validate_windows(windows)
	tables = table_names.resolve_zscore_tables(selected_windows)
	statements = [
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.ZSCORE_SERIES))} ("
		"series_key CHAR(64) NOT NULL, schema_version INT NOT NULL, source_scope CHAR(64) NOT NULL, "
		"calendar_id VARCHAR(128) NOT NULL, family VARCHAR(32) NOT NULL, window INT NOT NULL, "
		"calculation_version VARCHAR(128) NOT NULL, delta_definition VARCHAR(128) NOT NULL, "
		"precision VARCHAR(32) NOT NULL, missing_value_policy VARCHAR(64) NOT NULL, created_at VARCHAR(40) NOT NULL, "
		"PRIMARY KEY (series_key), UNIQUE "
		"(source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy)"
		") ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.ZSCORE_SESSIONS))} ("
		"series_key CHAR(64) NOT NULL, date_id INT NOT NULL, dependency_digest CHAR(64) NOT NULL, "
		"availability_digest CHAR(64) NOT NULL, actual_input_available TINYINT NOT NULL, completed_at VARCHAR(40) NOT NULL, "
		"row_count INT NOT NULL, ready_count INT NOT NULL, status_counts VARCHAR(256) NOT NULL, "
		"content_digest CHAR(64) NOT NULL, PRIMARY KEY (series_key, date_id)"
		") ENGINE=InnoDB",
	]
	for window in selected_windows:
		for table in (tables[window].price, tables[window].price_delta):
			statements.append(
				f"CREATE TABLE IF NOT EXISTS {quote_identifier(table)} ("
				"date_id INT NOT NULL, ticker_id INT NOT NULL, zscore FLOAT NULL, status TINYINT NOT NULL, "
				"PRIMARY KEY (date_id, ticker_id)) ENGINE=InnoDB"
			)
	return tuple(statements)


def _zscore_table_names(table_names: TableNames, windows: Iterable[int]) -> tuple[str, ...]:
	selected_windows = _validate_windows(windows)
	resolved = table_names.resolve_zscore_tables(selected_windows)
	return (
		table_names.table(TableRole.ZSCORE_SERIES),
		table_names.table(TableRole.ZSCORE_SESSIONS),
		*(table for window in selected_windows for table in (resolved[window].price, resolved[window].price_delta)),
	)


def assert_zscore_tables(cursor: Cursor, table_names: TableNames, windows: Iterable[int]) -> None:
	selected_windows = _validate_windows(windows)
	physical_names = _zscore_table_names(table_names, selected_windows)
	placeholders = ",".join("%s" for _ in physical_names)
	cursor.execute(
		f"SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})",
		physical_names,
	)
	rows = cursor.fetchall()
	if any(len(row) != 2 or row[1] != "InnoDB" for row in rows) or {row[0] for row in rows} != set(physical_names) or len(rows) != len(physical_names):
		raise ValueError("All selected z-score tables must exist and use InnoDB; no automatic schema conversion is performed")

	expected: dict[str, tuple[str, ...]] = {
		table_names.table(TableRole.ZSCORE_SERIES): ("series_key", "schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy", "created_at"),
		table_names.table(TableRole.ZSCORE_SESSIONS): ("series_key", "date_id", "dependency_digest", "availability_digest", "actual_input_available", "completed_at", "row_count", "ready_count", "status_counts", "content_digest"),
	}
	resolved = table_names.resolve_zscore_tables(selected_windows)
	for tables in resolved.values():
		for table in (tables.price, tables.price_delta):
			expected[table] = ("date_id", "ticker_id", "zscore", "status")
	column_placeholders = ",".join("%s" for _ in expected)
	try:
		cursor.execute(
			f"SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({column_placeholders}) ORDER BY TABLE_NAME, ORDINAL_POSITION",
			tuple(expected),
		)
		column_rows = cursor.fetchall()
	except Exception as error:
		if "no such table: information_schema.COLUMNS" in str(error):
			return
		raise
	actual: dict[str, list[str]] = {table: [] for table in expected}
	for row in column_rows:
		if len(row) != 2 or row[0] not in actual or not isinstance(row[1], str):
			raise ValueError("Invalid z-score table column metadata")
		actual[row[0]].append(row[1])
	if any(tuple(actual[table]) != columns for table, columns in expected.items()):
		raise ValueError("Selected z-score tables have incompatible columns; no automatic schema conversion is performed")


def assert_transactional_tables(cursor: Cursor, table_names: TableNames | None = None) -> None:
	selected_names = table_names or load_table_names()
	physical_names = tuple(selected_names.table(role) for role in _UPDATE_TABLE_ROLES)
	placeholders = ",".join("%s" for _ in physical_names)
	cursor.execute(f"SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})", physical_names)
	rows = cursor.fetchall()
	if any(len(row) != 2 or row[1] != "InnoDB" for row in rows) or {row[0] for row in rows} != set(physical_names) or len(rows) != len(physical_names):
		raise ValueError("All updater tables must exist and use InnoDB; no automatic schema conversion is performed")


def assert_source(cursor: Cursor, source: SourceConfig, table_names: TableNames | None = None) -> None:
	selected_names = table_names or load_table_names()
	cursor.execute(f"SELECT source_key FROM {quote_identifier(selected_names.source)} WHERE id = 1", ())
	rows = cursor.fetchall()
	if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] != source_key(source.identity):
		raise ValueError("Database must be explicitly initialized for this source scope; use a separate database for another scope")


def initialize_update_schema(source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
	"""Explicit updater setup only; DDL is separate from ingestion transactions."""
	selected_names = table_names or load_table_names()
	statements = update_schema_statements(selected_names)
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			for statement in statements:
				cursor.execute(statement, ())
			assert_transactional_tables(cursor, selected_names)
			cursor.execute(f"INSERT IGNORE INTO {quote_identifier(selected_names.source)} (id, source_key) VALUES (1, %s)", (source_key(source.identity),))
			assert_source(cursor, source, selected_names)
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
	finally:
		connection.close()


def initialize_zscore_schema(source: SourceConfig, connect: WriteConnectionFactory, windows: Iterable[int], table_names: TableNames | None = None) -> None:
	"""Explicitly create optional z-score metadata and materialization tables."""
	selected_names = table_names or load_table_names()
	selected_windows = _validate_windows(windows)
	statements = zscore_schema_statements(selected_names, selected_windows)
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute("START TRANSACTION", ())
			assert_transactional_tables(cursor, selected_names)
			assert_source(cursor, source, selected_names)
			for statement in statements:
				cursor.execute(statement, ())
			assert_zscore_tables(cursor, selected_names, selected_windows)
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
	finally:
		connection.close()
