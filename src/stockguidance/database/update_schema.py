from collections.abc import Iterable
from datetime import datetime, timezone

from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.quality import schema_statements as quality_schema_statements
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.verification import VerificationStatus, VerificationType, register_verification, verification_allows


_UPDATE_TABLE_ROLES = (TableRole.DATA_CONFIGURATION, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS, TableRole.VERIFICATIONS, TableRole.VERIFICATION_SAMPLES)


def _validate_windows(windows: Iterable[int]) -> tuple[int, ...]:
	values = tuple(windows)
	if any(type(window) is not int or window < 2 for window in values) or len(set(values)) != len(values) or not values:
		raise ValueError("Z-score windows must be distinct integers of at least 2")
	return values


def update_schema_statements(table_names: TableNames, *, case_sensitive_tickers: bool = True) -> tuple[str, ...]:
	ticker_definition = ("label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL UNIQUE, exchange VARCHAR(10) NULL"
		if case_sensitive_tickers else "label VARCHAR(16) NOT NULL UNIQUE, exchange VARCHAR(10) NULL")
	return (
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.DATA_CONFIGURATION))} (id INT PRIMARY KEY, configuration_key CHAR(64) NOT NULL, source_key CHAR(64) NOT NULL, source_family VARCHAR(64) NOT NULL, endpoint VARCHAR(512) NOT NULL, market VARCHAR(64) NOT NULL, universe VARCHAR(128) NOT NULL, interval_code VARCHAR(8) NOT NULL, raw_price_split_adjusted TINYINT NOT NULL, raw_price_dividend_adjusted TINYINT NOT NULL, raw_price_vendor_defined_adjustment VARCHAR(128) NULL, created_at VARCHAR(40) NOT NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.sessions)} (id INT AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.tickers)} (id INT AUTO_INCREMENT PRIMARY KEY, {ticker_definition}) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.prices)} (ticker_id INT NOT NULL, date_id INT NOT NULL, open DECIMAL(28,8), high DECIMAL(28,8), low DECIMAL(28,8), close DECIMAL(28,8), volume DECIMAL(20,8), vwap DECIMAL(42,22), transactions BIGINT, PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.deltas)} (ticker_id INT NOT NULL, date_id INT NOT NULL, " + ", ".join(f"{column} DOUBLE NULL" for column in DERIVED_COLUMNS) + ", PRIMARY KEY(ticker_id, date_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.imports)} (session_date DATE PRIMARY KEY, raw_id VARCHAR(64) NOT NULL, available_at VARCHAR(40) NOT NULL, derived_dependency_id VARCHAR(64) NULL, derived_content_id VARCHAR(64) NULL) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS stockguidance_daily_source_files (source_key CHAR(64) NOT NULL, session_date DATE NOT NULL, file_size BIGINT NOT NULL, modified_ns BIGINT NOT NULL, download_pending TINYINT NOT NULL DEFAULT 0, PRIMARY KEY(source_key, session_date)) ENGINE=InnoDB",
		"CREATE TABLE IF NOT EXISTS stockguidance_daily_import_in_progress (session_date DATE NOT NULL PRIMARY KEY, started_at VARCHAR(40) NOT NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.VERIFICATIONS))} (id INT AUTO_INCREMENT PRIMARY KEY, comparison_type VARCHAR(32) NOT NULL, baseline_key CHAR(64) NOT NULL, candidate_key CHAR(64) NOT NULL, status VARCHAR(16) NOT NULL, requested_count INT NOT NULL, completed_count INT NOT NULL, random_seed BIGINT NOT NULL, registered_at VARCHAR(40) NOT NULL, completed_at VARCHAR(40) NULL, detail VARCHAR(512) NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.VERIFICATION_SAMPLES))} (verification_id INT NOT NULL, sample_date DATE NOT NULL, status VARCHAR(16) NOT NULL, baseline_digest CHAR(64) NULL, candidate_digest CHAR(64) NULL, mismatch_count INT NOT NULL, detail VARCHAR(512) NULL, PRIMARY KEY (verification_id, sample_date)) ENGINE=InnoDB",
	) + quality_schema_statements()


def zscore_schema_statements(table_names: TableNames, windows: Iterable[int]) -> tuple[str, ...]:
	selected_windows = _validate_windows(windows)
	tables = table_names.resolve_zscore_tables(selected_windows)
	statements = [
		*quality_schema_statements(),
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_names.table(TableRole.ZSCORE_SERIES))} ("
		"series_key CHAR(64) NOT NULL, schema_version INT NOT NULL, source_scope CHAR(64) NOT NULL, "
		"calendar_id VARCHAR(128) NOT NULL, family VARCHAR(32) NOT NULL, `window` INT NOT NULL, "
		"calculation_version VARCHAR(128) NOT NULL, delta_definition VARCHAR(128) NOT NULL, "
		"`precision` VARCHAR(32) NOT NULL, missing_value_policy VARCHAR(64) NOT NULL, created_at VARCHAR(40) NOT NULL, "
		"PRIMARY KEY (series_key), UNIQUE "
		"(source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy)"
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


def assert_data_configuration(cursor: Cursor, source: SourceConfig, table_names: TableNames | None = None, *, write: bool = False) -> None:
	selected_names = table_names or load_table_names()
	cursor.execute(
		f"SELECT configuration_key, source_key, source_family, endpoint, market, universe, interval_code, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {quote_identifier(selected_names.table(TableRole.DATA_CONFIGURATION))} WHERE id = 1",
		(),
	)
	rows = cursor.fetchall()
	if len(rows) != 1 or len(rows[0]) != 10:
		raise ValueError("Database must be explicitly initialized for this data configuration")
	configuration = source.data_configuration
	expected = (
		data_configuration_key(configuration), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe,
		configuration.interval.value, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted,
		configuration.raw_price_vendor_defined_adjustment,
	)
	actual = tuple(bool(value) if index in (7, 8) else value for index, value in enumerate(rows[0]))
	if actual[0] != expected[0] or actual[1] != expected[0] or actual[2] != expected[1] or actual[4:] != expected[3:]:
		mode = source.data_configuration.verification_policy.source_family_mismatch
		if not verification_allows(VerificationStatus.PENDING, mode, write):
			raise ValueError("Database data configuration is not verified for this semantic data scope")
	if actual[3] != expected[2]:
		mode = source.data_configuration.verification_policy.endpoint_mismatch
		if not verification_allows(VerificationStatus.PENDING, mode, write):
			raise ValueError("Database data configuration endpoint is not verified")


def assert_source(cursor: Cursor, source: SourceConfig, table_names: TableNames | None = None, *, write: bool = False) -> None:
	assert_data_configuration(cursor, source, table_names, write=write)


def register_configuration_verification(source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> int | None:
	selected_names = table_names or load_table_names()
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(
				f"SELECT configuration_key, source_family, endpoint FROM {quote_identifier(selected_names.table(TableRole.DATA_CONFIGURATION))} WHERE id = 1",
				(),
			)
			rows = cursor.fetchall()
			if len(rows) != 1 or len(rows[0]) != 3:
				return None
			configuration = source.data_configuration
			baseline_key, baseline_family, baseline_endpoint = rows[0]
			candidate_key = data_configuration_key(configuration)
			if baseline_key == candidate_key and baseline_endpoint == configuration.endpoint:
				return None
			verification_type = VerificationType.SOURCE_FAMILY if baseline_family != configuration.source_family or baseline_key != candidate_key else VerificationType.ENDPOINT
			requested_count = configuration.verification_policy.source_family_sample_days if verification_type is VerificationType.SOURCE_FAMILY else configuration.verification_policy.endpoint_sample_pulls
			seed = int(candidate_key[:16], 16) % (2**63 - 1)
			cursor.execute(
				f"SELECT id FROM {quote_identifier(selected_names.table(TableRole.VERIFICATIONS))} WHERE comparison_type = %s AND baseline_key = %s AND candidate_key = %s AND status IN (%s, %s) ORDER BY id DESC",
				(verification_type.value, str(baseline_key), candidate_key, VerificationStatus.PENDING.value, VerificationStatus.RUNNING.value),
			)
			pending = cursor.fetchall()
			if pending:
				return pending[0][0]
			verification_id = register_verification(cursor, selected_names, verification_type, str(baseline_key), candidate_key, requested_count, seed, datetime.now(timezone.utc))
			connection.commit()
			return verification_id
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
	finally:
		connection.close()


def initialize_update_schema(source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
	"""Explicit updater setup only; DDL is separate from ingestion transactions."""
	selected_names = table_names or load_table_names()
	connection = connect(source.database_config)
	try:
		sqlite_compatible = type(getattr(connection, "connection", None)).__module__ == "sqlite3"
		statements = update_schema_statements(selected_names, case_sensitive_tickers=not sqlite_compatible)
		cursor = connection.cursor()
		try:
			for statement in statements:
				cursor.execute(statement, ())
			if not sqlite_compatible:
				for table in (selected_names.prices, selected_names.deltas):
					cursor.execute("SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY INDEX_NAME, SEQ_IN_INDEX", (table,))
					indexes = cursor.fetchall()
					if not any(row[1] == "date_id" and row[2] == 1 for row in indexes):
						cursor.execute(f"CREATE INDEX stockguidance_date_ticker ON {quote_identifier(table)} (date_id, ticker_id)", ())
			assert_transactional_tables(cursor, selected_names)
			configuration = source.data_configuration
			cursor.execute(
				f"INSERT IGNORE INTO {quote_identifier(selected_names.table(TableRole.DATA_CONFIGURATION))} (id, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment, created_at) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
				(data_configuration_key(configuration), data_configuration_key(configuration), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted, configuration.raw_price_vendor_defined_adjustment, datetime.now(timezone.utc).isoformat()),
			)
			assert_data_configuration(cursor, source, selected_names)
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
			assert_source(cursor, source, selected_names, write=True)
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
