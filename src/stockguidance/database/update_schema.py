from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import source_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.database.table_names import TableNames, load_table_names, quote_identifier


_UPDATE_TABLE_ROLES = ("source", "sessions", "tickers", "prices", "deltas", "imports")


def update_schema_statements(table_names: TableNames) -> tuple[str, ...]:
	return (
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.source)} (id INT PRIMARY KEY, source_key VARCHAR(64) NOT NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.sessions)} (id INT AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.tickers)} (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(16) NOT NULL UNIQUE) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.prices)} (ticker_id INT NOT NULL, date_id INT NOT NULL, open DECIMAL(19,4), high DECIMAL(19,4), low DECIMAL(19,4), close DECIMAL(19,4), volume BIGINT, vwap DECIMAL(19,4), transactions BIGINT, PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.deltas)} (ticker_id INT NOT NULL, date_id INT NOT NULL, " + ", ".join(f"{column} DOUBLE NULL" for column in DERIVED_COLUMNS) + ", PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.imports)} (session_date DATE PRIMARY KEY, raw_id VARCHAR(64) NOT NULL, available_at VARCHAR(40) NOT NULL, derived_dependency_id VARCHAR(64) NULL, derived_content_id VARCHAR(64) NULL) ENGINE=InnoDB",
	)


def assert_transactional_tables(cursor: Cursor, table_names: TableNames | None = None) -> None:
	selected_names = table_names or load_table_names()
	physical_names = tuple(getattr(selected_names, role) for role in _UPDATE_TABLE_ROLES)
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
