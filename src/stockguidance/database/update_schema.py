from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import source_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS


UPDATE_TABLES = ("stockguidance_source", "transaction_dates", "ticker_symbol", "raw_stock_data", "daily_delta_stock_data", "stockguidance_imports")


def assert_transactional_tables(cursor: Cursor) -> None:
	placeholders = ",".join("%s" for table in UPDATE_TABLES)
	cursor.execute(f"SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})", UPDATE_TABLES)
	rows = cursor.fetchall()
	if any(len(row) != 2 or row[1] != "InnoDB" for row in rows) or {row[0] for row in rows} != set(UPDATE_TABLES) or len(rows) != len(UPDATE_TABLES):
		raise ValueError("All updater tables must exist and use InnoDB; no automatic schema conversion is performed")


def assert_source(cursor: Cursor, source: SourceConfig) -> None:
	cursor.execute("SELECT source_key FROM stockguidance_source WHERE id = 1", ())
	rows = cursor.fetchall()
	if len(rows) != 1 or len(rows[0]) != 1 or rows[0][0] != source_key(source.identity):
		raise ValueError("Database must be explicitly initialized for this source scope; use a separate database for another scope")


def initialize_update_schema(source: SourceConfig, connect: WriteConnectionFactory) -> None:
	"""Explicit updater setup only; DDL is separate from ingestion transactions."""
	statements = (
		"CREATE TABLE IF NOT EXISTS stockguidance_source (id INT PRIMARY KEY, source_key VARCHAR(64) NOT NULL) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS transaction_dates (id INT AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS ticker_symbol (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(16) NOT NULL UNIQUE) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS raw_stock_data (ticker_id INT NOT NULL, date_id INT NOT NULL, open DECIMAL(19,4), high DECIMAL(19,4), low DECIMAL(19,4), close DECIMAL(19,4), volume BIGINT, vwap DECIMAL(19,4), transactions BIGINT, PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS daily_delta_stock_data (ticker_id INT NOT NULL, date_id INT NOT NULL, " + ", ".join(f"{column} DOUBLE NULL" for column in DERIVED_COLUMNS) + ", PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS stockguidance_imports (session_date DATE PRIMARY KEY, raw_id VARCHAR(64) NOT NULL, available_at VARCHAR(40) NOT NULL, derived_dependency_id VARCHAR(64) NULL, derived_content_id VARCHAR(64) NULL) ENGINE=InnoDB",
	)
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			for statement in statements:
				cursor.execute(statement, ())
			assert_transactional_tables(cursor)
			cursor.execute("INSERT IGNORE INTO stockguidance_source (id, source_key) VALUES (1, %s)", (source_key(source.identity),))
			assert_source(cursor, source)
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
	finally:
		connection.close()
