"""Explicit initializer and version guard for the Stage 3 replacement schema."""

from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol, Sequence

from stockguidance.config.source import SourceConfig
from stockguidance.database.protocols import Cursor
from stockguidance.database.simplified_contract import DATA_STATE_IDS, DataState, SCHEMA_VERSION
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key, source_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS


REGISTRY_VERSION = 2
SCHEMA_NAME = "stockguidance_simplified"
_RETIRED_FIXED_TABLES = (
	"stockguidance_archive_files",
	"stockguidance_daily_source_files",
	"stockguidance_daily_import_in_progress",
	"stockguidance_row_quality",
	"stockguidance_quality_revisions",
	"stockguidance_output_quality",
	"stockguidance_quality_scans",
	"stockguidance_migration_receipts",
	"stockguidance_migration_completion",
)
_STATE_DESCRIPTIONS = {
	DataState.GOOD: "Valid direct source observation.",
	DataState.BAD: "Source observation failed validation and has no usable effective value.",
	DataState.MISSING: "Requested observation is explicitly known to be absent or unavailable.",
	DataState.INTERPOLATED: "Contract-approved effective value calculated from bounded endpoints.",
}


class SchemaVersionError(ValueError):
	"""Raised when operational code is pointed at a non-target schema."""


class _ConnectionWithOptionalSqlite(Protocol):
	connection: object


def registry_digest(table_names: TableNames) -> str:
	payload = json.dumps({"tables": table_names.target_tables(), "zscore_templates": table_names.templates()}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def retired_table_names(table_names: TableNames) -> tuple[str, ...]:
	"""Return operational tables that are not allowed beside the target schema."""
	names = (*_RETIRED_FIXED_TABLES, table_names.imports, table_names.data_verifications, table_names.data_verification_samples)
	return tuple(dict.fromkeys(names))


def _missing_table(error: Exception) -> bool:
	message = str(error).casefold()
	return "no such table" in message or "doesn't exist" in message or "does not exist" in message


def _table_names(cursor: Cursor, names: Sequence[str]) -> set[str]:
	if not names:
		return set()
	placeholders = ",".join("%s" for _ in names)
	try:
		cursor.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})", tuple(names))
		rows = cursor.fetchall()
	except Exception:
		try:
			cursor.execute(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})", tuple(names))
			rows = cursor.fetchall()
		except Exception:
			return set()
	return {str(row[0]) for row in rows if len(row) >= 1}


def reject_target_schema(cursor: Cursor, table_names: TableNames, operation: str) -> None:
	"""Stop a historical writer before it can create or use legacy tables."""
	marker = quote_identifier(table_names.table(TableRole.SCHEMA))
	try:
		cursor.execute(f"SELECT id FROM {marker} LIMIT 1", ())
		if not cursor.fetchall():
			return
	except Exception as error:
		if _missing_table(error):
			return
		raise
	raise SchemaVersionError(f"{operation} is retired for the versioned simplified schema")


def _validate_windows(windows: Iterable[int]) -> tuple[int, ...]:
	values = tuple(windows)
	if not values or any(type(value) is not int or value < 2 for value in values) or len(set(values)) != len(values):
		raise ValueError("Simplified z-score windows must be distinct integers of at least 2")
	return values


def _is_sqlite(connection: WriteConnection) -> bool:
	return type(getattr(connection, "connection", None)).__module__ == "sqlite3"


def _mysql_statements(table_names: TableNames, windows: tuple[int, ...]) -> tuple[str, ...]:
	schema = quote_identifier(table_names.table(TableRole.SCHEMA))
	source = quote_identifier(table_names.table(TableRole.SOURCE))
	sessions = quote_identifier(table_names.table(TableRole.SESSIONS))
	tickers = quote_identifier(table_names.table(TableRole.TICKERS))
	prices = quote_identifier(table_names.table(TableRole.PRICES))
	deltas = quote_identifier(table_names.table(TableRole.DELTAS))
	states = quote_identifier(table_names.table(TableRole.DATA_STATES))
	pending = quote_identifier(table_names.table(TableRole.PENDING_UPDATES))
	repairs = quote_identifier(table_names.table(TableRole.REPAIR_METADATA))
	series = quote_identifier(table_names.table(TableRole.ZSCORE_SERIES))
	zscore_sessions = quote_identifier(table_names.table(TableRole.ZSCORE_SESSIONS))
	ticker_definition = "label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL UNIQUE, exchange VARCHAR(10) NULL"
	statements = [
		f"CREATE TABLE IF NOT EXISTS {schema} (id TINYINT UNSIGNED NOT NULL PRIMARY KEY, schema_name VARCHAR(64) NOT NULL, schema_version INT UNSIGNED NOT NULL, registry_version INT UNSIGNED NOT NULL, registry_digest CHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL, CHECK (id = 1)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {source} (id INT UNSIGNED NOT NULL PRIMARY KEY, configuration_key CHAR(64) NOT NULL, source_key CHAR(64) NOT NULL, source_family VARCHAR(64) NOT NULL, endpoint VARCHAR(512) NOT NULL, market VARCHAR(64) NOT NULL, universe VARCHAR(128) NOT NULL, interval_code VARCHAR(8) NOT NULL, calendar_id VARCHAR(32) NOT NULL, raw_price_split_adjusted TINYINT(1) NOT NULL, raw_price_dividend_adjusted TINYINT(1) NOT NULL, raw_price_vendor_defined_adjustment VARCHAR(128) NULL, created_at DATETIME(6) NOT NULL, CHECK (id = 1)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {states} (id SMALLINT UNSIGNED NOT NULL PRIMARY KEY, code VARCHAR(16) NOT NULL UNIQUE, description VARCHAR(255) NOT NULL) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {sessions} (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, transaction_date DATE NOT NULL UNIQUE, raw_complete TINYINT(1) NOT NULL DEFAULT 0, raw_available_at DATETIME(6) NULL, raw_completed_at DATETIME(6) NULL, raw_revision CHAR(64) NULL, delta_complete TINYINT(1) NOT NULL DEFAULT 0, delta_completed_at DATETIME(6) NULL, CHECK (raw_complete IN (0, 1)), CHECK (delta_complete IN (0, 1))) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {tickers} (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, {ticker_definition}) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {prices} (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, open DECIMAL(28,8) NULL, high DECIMAL(28,8) NULL, low DECIMAL(28,8) NULL, close DECIMAL(28,8) NULL, volume DECIMAL(20,8) NULL, vwap DECIMAL(42,22) NULL, transactions BIGINT UNSIGNED NULL, state_id SMALLINT UNSIGNED NOT NULL, observed_at DATETIME(6) NULL, PRIMARY KEY(ticker_id, date_id), CONSTRAINT simplified_prices_ticker_fk FOREIGN KEY (ticker_id) REFERENCES {tickers}(id), CONSTRAINT simplified_prices_date_fk FOREIGN KEY (date_id) REFERENCES {sessions}(id), CONSTRAINT simplified_prices_state_fk FOREIGN KEY (state_id) REFERENCES {states}(id)) ENGINE=InnoDB",
		f"CREATE INDEX simplified_prices_date_ticker ON {prices} (date_id, ticker_id)",
		f"CREATE TABLE IF NOT EXISTS {deltas} (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, " + ", ".join(f"{column} DOUBLE NULL" for column in DERIVED_COLUMNS) + f", calculation_status VARCHAR(32) NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, completed_at DATETIME(6) NOT NULL, PRIMARY KEY(ticker_id, date_id), CONSTRAINT simplified_deltas_ticker_fk FOREIGN KEY (ticker_id) REFERENCES {tickers}(id), CONSTRAINT simplified_deltas_date_fk FOREIGN KEY (date_id) REFERENCES {sessions}(id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {repairs} (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, revision CHAR(64) NOT NULL, state_id SMALLINT UNSIGNED NOT NULL, original_values JSON NOT NULL, effective_values JSON NULL, invalid_fields JSON NOT NULL, interpolation_method VARCHAR(64) NULL, prior_date_id INT UNSIGNED NULL, following_date_id INT UNSIGNED NULL, endpoint_values JSON NULL, observed_at DATETIME(6) NULL, available_at DATETIME(6) NULL, endpoints_available_at DATETIME(6) NULL, recorded_at DATETIME(6) NOT NULL, UNIQUE KEY simplified_repair_revision (date_id, ticker_id, revision), CONSTRAINT simplified_repairs_date_fk FOREIGN KEY (date_id) REFERENCES {sessions}(id), CONSTRAINT simplified_repairs_ticker_fk FOREIGN KEY (ticker_id) REFERENCES {tickers}(id), CONSTRAINT simplified_repairs_state_fk FOREIGN KEY (state_id) REFERENCES {states}(id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {pending} (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, operation_kind VARCHAR(32) NOT NULL, scope_version SMALLINT UNSIGNED NOT NULL, scope JSON NOT NULL, created_at DATETIME(6) NOT NULL, clearing_started_at DATETIME(6) NULL, affected_rows_cleared TINYINT(1) NOT NULL DEFAULT 0, failure_code VARCHAR(64) NULL, failure_detail TEXT NULL, failed_at DATETIME(6) NULL, CHECK (affected_rows_cleared IN (0, 1)), CHECK (operation_kind IN ('initial_import', 'forced_replacement', 'derived_repair', 'recovery')), CHECK (scope_version = 1)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {series} (series_key CHAR(64) NOT NULL PRIMARY KEY, schema_version INT NOT NULL, source_scope CHAR(64) NOT NULL, calendar_id VARCHAR(128) NOT NULL, family VARCHAR(32) NOT NULL, `window` INT NOT NULL, calculation_version VARCHAR(128) NOT NULL, delta_definition VARCHAR(128) NOT NULL, `precision` VARCHAR(32) NOT NULL, missing_value_policy VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL, UNIQUE KEY simplified_series_definition (source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {zscore_sessions} (series_key CHAR(64) NOT NULL, date_id INT UNSIGNED NOT NULL, dependency_digest CHAR(64) NOT NULL, availability_digest CHAR(64) NOT NULL, actual_input_available TINYINT(1) NOT NULL, completed_at DATETIME(6) NOT NULL, row_count INT NOT NULL, ready_count INT NOT NULL, status_counts JSON NOT NULL, content_digest CHAR(64) NOT NULL, PRIMARY KEY (series_key, date_id), CONSTRAINT simplified_zscore_sessions_series_fk FOREIGN KEY (series_key) REFERENCES {series}(series_key), CONSTRAINT simplified_zscore_sessions_date_fk FOREIGN KEY (date_id) REFERENCES {sessions}(id)) ENGINE=InnoDB",
	]
	for window in windows:
		resolved = table_names.resolve_zscore_tables((window,))[window]
		for table in (resolved.price, resolved.price_delta):
			physical = quote_identifier(table)
			family = "price" if table == resolved.price else "price_delta"
			statements.append(f"CREATE TABLE IF NOT EXISTS {physical} (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL DEFAULT 0, gap_length INT UNSIGNED NOT NULL DEFAULT 0, provenance JSON NOT NULL, PRIMARY KEY (date_id, ticker_id), CONSTRAINT simplified_{family}_{window}_date_fk FOREIGN KEY (date_id) REFERENCES {sessions}(id), CONSTRAINT simplified_{family}_{window}_ticker_fk FOREIGN KEY (ticker_id) REFERENCES {tickers}(id)) ENGINE=InnoDB")
	return tuple(statements)


def simplified_schema_statements(table_names: TableNames, windows: Iterable[int]) -> tuple[str, ...]:
	"""Return the target DDL; identifiers are registry-resolved and validated."""
	return _mysql_statements(table_names, _validate_windows(windows))


def _sqlite_statement(statement: str) -> str:
	return (re.sub(r"UNIQUE KEY [A-Za-z0-9_]+ \(([^)]*)\)", r"UNIQUE (\1)", statement)
		.replace(" INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY", " INTEGER PRIMARY KEY AUTOINCREMENT")
		.replace(" BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY", " INTEGER PRIMARY KEY AUTOINCREMENT")
		.replace(" INT UNSIGNED", " INTEGER")
		.replace(" SMALLINT UNSIGNED", " INTEGER")
		.replace(" BIGINT UNSIGNED", " INTEGER")
		.replace(" INT UNSIGNED", " INTEGER")
		.replace(" TINYINT(1)", " INTEGER")
		.replace(" TINYINT UNSIGNED", " INTEGER")
		.replace(" INT ", " INTEGER ")
		.replace(" JSON ", " TEXT ")
		.replace(" DATETIME(6)", " TEXT")
		.replace(" COLLATE utf8mb4_bin", "")
		.replace(" ENGINE=InnoDB", ""))


def _existing_tables(cursor: Cursor, sqlite_compatible: bool, names: Sequence[str]) -> set[str]:
	if sqlite_compatible:
		placeholders = ",".join("%s" for _ in names)
		cursor.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})", tuple(names))
	else:
		placeholders = ",".join("%s" for _ in names)
		cursor.execute(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})", tuple(names))
	return {str(row[0]) for row in cursor.fetchall() if len(row) == 1}


def require_simplified_schema(cursor: Cursor, table_names: TableNames) -> None:
	table = quote_identifier(table_names.table(TableRole.SCHEMA))
	try:
		cursor.execute(f"SELECT schema_name, schema_version, registry_version, registry_digest FROM {table} WHERE id = 1", ())
		rows = cursor.fetchall()
	except Exception as error:
		raise SchemaVersionError("Simplified schema version metadata is unavailable") from error
	if len(rows) != 1 or len(rows[0]) != 4:
		raise SchemaVersionError("Database is not initialized with exactly one simplified schema version")
	name, version, registry_version, digest = rows[0]
	if name != SCHEMA_NAME or version != SCHEMA_VERSION or registry_version != REGISTRY_VERSION or digest != registry_digest(table_names):
		raise SchemaVersionError("Database schema or table registry version is incompatible")
	target_roles = (TableRole.SCHEMA, TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.DATA_STATES, TableRole.PENDING_UPDATES, TableRole.REPAIR_METADATA, TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS)
	required = tuple(table_names.table(role) for role in target_roles)
	if _table_names(cursor, required) != set(required):
		raise SchemaVersionError("Simplified schema is incomplete; no automatic migration is performed")
	retired = _table_names(cursor, retired_table_names(table_names))
	if retired:
		raise SchemaVersionError("Simplified schema is mixed with retired operational tables: " + ", ".join(sorted(retired)))


def initialize_simplified_schema(source: SourceConfig, connect: WriteConnectionFactory, windows: Iterable[int] = (20, 60), table_names: TableNames | None = None) -> None:
	"""Create a fresh disposable target schema and seed immutable reference rows.

	This is an explicit setup operation. It refuses an existing legacy or partial
	database instead of attempting an additive migration.
	"""
	names = table_names or load_table_names()
	selected_windows = _validate_windows(windows)
	resolved = names.resolve_zscore_tables(selected_windows)
	target_roles = (TableRole.SCHEMA, TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.DATA_STATES, TableRole.PENDING_UPDATES, TableRole.REPAIR_METADATA, TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS)
	all_names = tuple(names.table(role) for role in target_roles) + tuple(table for pair in resolved.values() for table in (pair.price, pair.price_delta))
	connection = connect(source.database_config)
	sqlite_compatible = _is_sqlite(connection)
	try:
		cursor = connection.cursor()
		try:
			existing = _existing_tables(cursor, sqlite_compatible, all_names)
			schema_name = names.table(TableRole.SCHEMA)
			if existing and schema_name not in existing:
				raise SchemaVersionError("Existing database has no simplified schema version; refusing legacy/partial initialization")
			if schema_name in existing:
				require_simplified_schema(cursor, names)
				# Existing target schemas are never silently extended with new windows.
				missing = set(all_names) - existing
				if missing:
					raise SchemaVersionError("Simplified schema is incomplete; no automatic migration is performed")
				connection.rollback()
				return
			retired = _table_names(cursor, retired_table_names(names))
			if retired:
				raise SchemaVersionError("Existing database contains retired operational tables; initialize the simplified schema on a clean target: " + ", ".join(sorted(retired)))
			statements = _mysql_statements(names, selected_windows)
			for statement in statements:
				cursor.execute(_sqlite_statement(statement) if sqlite_compatible else statement, ())
			created_at = datetime.now(timezone.utc)
			metadata = (1, SCHEMA_NAME, SCHEMA_VERSION, REGISTRY_VERSION, registry_digest(names), created_at)
			cursor.execute(f"INSERT INTO {quote_identifier(schema_name)} (id, schema_name, schema_version, registry_version, registry_digest, created_at) VALUES (%s,%s,%s,%s,%s,%s)", metadata)
			for state, state_id in DATA_STATE_IDS.items():
				cursor.execute(f"INSERT INTO {quote_identifier(names.table(TableRole.DATA_STATES))} (id, code, description) VALUES (%s,%s,%s)", (state_id, state.value, _STATE_DESCRIPTIONS[state]))
			configuration = source.data_configuration
			cursor.execute(
				f"INSERT INTO {quote_identifier(names.table(TableRole.SOURCE))} (id, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment, created_at) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
				(data_configuration_key(configuration), source_key(source.identity), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value, configuration.calendar_id, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted, configuration.raw_price_vendor_defined_adjustment, created_at),
			)
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
	finally:
		connection.close()


def assert_simplified_schema(cursor: Cursor, table_names: TableNames | None = None) -> None:
	require_simplified_schema(cursor, table_names or load_table_names())
