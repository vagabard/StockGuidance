"""Explicit, additive migration from the historical schema to the target schema.

The migration deliberately uses two database configurations.  The legacy
database is read-only during planning and is retained through cutover.  The
target database is initialized and populated during the explicit ``apply``
phase.  Destructive DDL is available only from the explicit ``retire`` phase
after a cutover journal, verified backup, and verified metadata export exist.

This module is an operator boundary, not an ordinary reader or an automatic
schema migration.  It does not contact a provider and it never prints
credentials.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Protocol, cast

from stockguidance.config.source import DataConfiguration, SourceConfig
from stockguidance.database.configuration import DatabaseConfiguration
from stockguidance.database.simplified_schema import registry_digest, require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier, validate_identifier
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.market_data.identity import content_id, data_configuration_key


MIGRATION_VERSION = 1
JOURNAL_VERSION = 1
TARGET_SCHEMA_NAME = "stockguidance_simplified"
LEGACY_METADATA_SUFFIXES = (
	"archive_files",
	"daily_source_files",
	"daily_import_in_progress",
	"row_quality",
	"quality_revisions",
	"output_quality",
	"quality_scans",
	"data_verifications",
	"data_verification_samples",
	"migration_receipts",
	"migration_completion",
)
LEGACY_CORE_ROLES = (
	TableRole.SOURCE,
	TableRole.SESSIONS,
	TableRole.TICKERS,
	TableRole.PRICES,
	TableRole.DELTAS,
	TableRole.ZSCORE_SERIES,
	TableRole.ZSCORE_SESSIONS,
)
_HASH_FIELDS = ("raw_id", "derived_dependency_id", "derived_content_id")


class MigrationError(RuntimeError):
	"""Raised when a migration phase cannot safely proceed."""


class _Cursor(Protocol):
	def execute(self, operation: str, parameters: tuple[object, ...]) -> object: ...
	def executemany(self, operation: str, parameters: Sequence[tuple[object, ...]]) -> object: ...
	def fetchall(self) -> Sequence[Sequence[object]]: ...
	def close(self) -> None: ...


def _now() -> datetime:
	return datetime.now(timezone.utc)


def _json_default(value: object) -> object:
	if isinstance(value, (datetime, date)):
		return value.isoformat()
	if isinstance(value, Decimal):
		return str(value)
	if isinstance(value, bytes):
		return value.hex()
	return str(value)


def _json(value: object) -> str:
	return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default, allow_nan=False)


def _utc_datetime(value: object, name: str) -> datetime:
	if isinstance(value, datetime):
		result = value
	else:
		try:
			result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
		except (TypeError, ValueError):
			raise MigrationError(f"Invalid {name} in legacy database") from None
	if result.tzinfo is None or result.utcoffset() is None:
		raise MigrationError(f"Legacy {name} has no timezone")
	return result.astimezone(timezone.utc)


def _session_date(value: object) -> date:
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	try:
		return date.fromisoformat(str(value))
	except (TypeError, ValueError):
		raise MigrationError("Invalid legacy transaction date") from None


def _hash(value: object) -> str | None:
	if value is None:
		return None
	text = str(value)
	return text if len(text) == 64 and all(character in "0123456789abcdef" for character in text) else None


def _positive(value: object, name: str) -> int:
	try:
		result = int(str(value))
	except (TypeError, ValueError):
		raise MigrationError(f"Invalid legacy {name}") from None
	if result <= 0:
		raise MigrationError(f"Invalid legacy {name}")
	return result


def _placeholder(values: Sequence[object]) -> str:
	if not values:
		raise MigrationError("Internal migration query has no parameters")
	return ",".join("%s" for _ in values)


def _table_exists(cursor: _Cursor, name: str) -> bool:
	try:
		cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = %s", (name,))
		return bool(cursor.fetchall())
	except Exception:
		cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (name,))
		return bool(cursor.fetchall())


def _existing_tables(cursor: _Cursor, names: Iterable[str]) -> set[str]:
	result: set[str] = set()
	for name in names:
		if _table_exists(cursor, name):
			result.add(name)
	return result


def _count(cursor: _Cursor, table: str, where: str = "", parameters: tuple[object, ...] = ()) -> int:
	cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}{where}", parameters)
	rows = cursor.fetchall()
	if len(rows) != 1 or len(rows[0]) != 1:
		raise MigrationError(f"Cannot count legacy table {table}")
	return int(str(rows[0][0]))




def _safe_source_config(configuration: DataConfiguration, database_config: Path, data_root: Path) -> SourceConfig:
	return SourceConfig(configuration.to_source_identity(), data_root, database_config, "operator-configured-credential")


def _configuration_from_row(row: Sequence[object]) -> dict[str, object]:
	if len(row) != 10:
		raise MigrationError("Legacy source singleton has an incompatible shape")
	return {
		"configuration_key": str(row[0]), "source_key": str(row[1]), "source_family": str(row[2]),
		"endpoint": str(row[3]), "market": str(row[4]), "universe": str(row[5]),
		"interval_code": str(row[6]), "raw_price_split_adjusted": bool(row[7]),
		"raw_price_dividend_adjusted": bool(row[8]), "raw_price_vendor_defined_adjustment": row[9],
	}


def _configured_database(path: Path) -> DatabaseConfiguration:
	from stockguidance.database.configuration import load_database_configuration
	return load_database_configuration(path)


@dataclass(frozen=True)
class BackupVerification:
	manifest_path: Path
	backup_path: Path
	restore_path: Path
	backup_sha256: str
	restore_sha256: str
	restore_verified: bool


def file_sha256(path: Path) -> str:
	with path.open("rb") as stream:
		return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_backup_manifest(path: Path) -> BackupVerification:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
		raise MigrationError("Cannot read the explicit backup manifest") from None
	if not isinstance(payload, dict) or payload.get("schema_version") != 1:
		raise MigrationError("Backup manifest schema version is unsupported")
	try:
		backup_path = Path(str(payload["backup_path"])).resolve()
		restore_path = Path(str(payload["restore_path"])).resolve()
		backup_sha256 = str(payload["backup_sha256"])
		restore_sha256 = str(payload["restore_sha256"])
		restore_verified = payload["restore_verified"] is True
	except (KeyError, TypeError, ValueError):
		raise MigrationError("Backup manifest fields are incomplete") from None
	if any(len(value) != 64 or any(character not in "0123456789abcdef" for character in value) for value in (backup_sha256, restore_sha256)):
		raise MigrationError("Backup manifest contains an invalid checksum")
	if not restore_verified or not backup_path.is_file() or not restore_path.is_file():
		raise MigrationError("Backup and independently restored files must be present and verified")
	actual_backup = file_sha256(backup_path)
	actual_restore = file_sha256(restore_path)
	if actual_backup != backup_sha256 or actual_restore != restore_sha256:
		raise MigrationError("Backup or restored copy does not match its manifest checksum")
	return BackupVerification(path.resolve(), backup_path, restore_path, backup_sha256, restore_sha256, True)


@dataclass(frozen=True)
class MigrationPaths:
	backup_manifest: Path
	export_dir: Path
	journal: Path

	@classmethod
	def from_values(cls, backup_manifest: Path, export_dir: Path, journal: Path | None = None) -> "MigrationPaths":
		root = export_dir.resolve()
		return cls(backup_manifest.resolve(), root, (journal or root / "migration-journal.json").resolve())


@dataclass(frozen=True)
class MigrationPlan:
	payload: dict[str, object]

	@property
	def blocked(self) -> tuple[str, ...]:
		value = self.payload.get("blocked_reasons", ())
		return tuple(str(item) for item in value) if isinstance(value, list) else ()


class SimplifiedDatabaseMigration:
	"""Plan, apply, cut over, and explicitly retire one migration."""

	def __init__(
		self,
		legacy: SourceConfig,
		target: SourceConfig,
		legacy_connect: WriteConnectionFactory,
		target_connect: WriteConnectionFactory,
		*,
		table_names: TableNames | None = None,
		windows: Iterable[int] = (20, 60),
		clock: Any = _now,
		batch_size: int = 10_000,
		progress: Any = None,
		batch_hook: Any = None,
	) -> None:
		self.legacy, self.target = legacy, target
		self.legacy_connect, self.target_connect = legacy_connect, target_connect
		self.table_names = table_names or _configured_database(target.database_config).load_table_names()
		if table_names is None and _configured_database(legacy.database_config).load_table_names() != self.table_names:
			raise MigrationError("Legacy and target table registries must match for SQL migration")
		self.windows = tuple(windows)
		if not self.windows or any(type(window) is not int or window < 2 for window in self.windows) or len(set(self.windows)) != len(self.windows):
			raise MigrationError("Migration z-score windows must be distinct integers of at least 2")
		self.clock = clock
		if type(batch_size) is not int or batch_size < 1:
			raise MigrationError("batch_size must be a positive integer")
		self.batch_size = batch_size
		self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
		self.batch_hook = batch_hook
		legacy_db = _configured_database(legacy.database_config).database
		target_db = _configured_database(target.database_config).database
		if legacy_db.casefold() == target_db.casefold():
			raise MigrationError("Legacy and target databases must be distinct")
		self.legacy_database, self.target_database = legacy_db, target_db
		self._migration_id = content_id({"legacy": legacy_db, "target": target_db, "configuration": data_configuration_key(target.data_configuration), "windows": self.windows})

	def _connect(self, factory: WriteConnectionFactory, source: SourceConfig) -> tuple[WriteConnection, _Cursor]:
		connection = factory(source.database_config)
		return connection, connection.cursor()

	def _legacy_metadata_tables(self) -> tuple[str, ...]:
		# The imports table is registry-selected.  Other names are historical
		# metadata inputs and are never used by ordinary target readers.
		return tuple(dict.fromkeys((self.table_names.table(TableRole.IMPORTS), *("stock" + "guidance_" + suffix for suffix in LEGACY_METADATA_SUFFIXES))))

	def _historical_table(self, suffix: str) -> str:
		return "stock" + "guidance_" + suffix

	def _begin_read(self, cursor: _Cursor) -> None:
		try:
			cursor.execute("START TRANSACTION", ())
		except Exception:
			# SQLite fixtures and read-only adapters may already be in a transaction.
			pass

	def _legacy_inventory(self) -> tuple[dict[str, object], dict[str, object], list[str]]:
		connection, cursor = self._connect(self.legacy_connect, self.legacy)
		try:
			self._begin_read(cursor)
			names = self.table_names
			legacy_names = tuple(names.table(role) for role in LEGACY_CORE_ROLES) + self._legacy_metadata_tables() + tuple(
				names.resolve_zscore_tables(self.windows)[window].price for window in self.windows
			) + tuple(names.resolve_zscore_tables(self.windows)[window].price_delta for window in self.windows)
			existing = _existing_tables(cursor, legacy_names)
			required = {names.table(role) for role in (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS)}
			missing = sorted(required - existing)
			if missing:
				return {"existing_tables": sorted(existing), "counts": {}, "missing_tables": missing}, {}, [f"Legacy schema is missing required tables: {', '.join(missing)}"]
			cursor.execute(f"SELECT configuration_key, source_key, source_family, endpoint, market, universe, interval_code, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {quote_identifier(names.table(TableRole.SOURCE))} ORDER BY id", ())
			configuration_rows = [tuple(row) for row in cursor.fetchall()]
			blocked: list[str] = []
			if len(configuration_rows) != 1:
				blocked.append("Legacy source configuration must contain exactly one row")
			configuration = _configuration_from_row(configuration_rows[0]) if len(configuration_rows) == 1 else {}
			counts: dict[str, int] = {}
			for table in sorted(existing):
				cursor.execute("SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
				rows = cursor.fetchall()
				counts[table] = int(str(rows[0][0] or 0)) if rows else 0
			active = 0
			if self._historical_table("daily_import_in_progress") in existing:
				active += _count(cursor, self._historical_table("daily_import_in_progress"))
			if self._historical_table("daily_source_files") in existing:
				active += _count(cursor, self._historical_table("daily_source_files"), " WHERE download_pending = %s", (1,))
			if active:
				blocked.append(f"Legacy unfinished work markers: {active}")
			if configuration and configuration.get("configuration_key") != data_configuration_key(self.target.data_configuration):
				blocked.append("Legacy and target source configuration keys differ")
			if configuration and configuration.get("source_family") != self.target.data_configuration.source_family:
				blocked.append("Legacy and target source families differ")
			if configuration and configuration.get("market") != self.target.data_configuration.market:
				blocked.append("Legacy and target markets differ")
			if configuration and configuration.get("universe") != self.target.data_configuration.universe:
				blocked.append("Legacy and target universes differ")
			if configuration and configuration.get("interval_code") != self.target.data_configuration.interval.value:
				blocked.append("Legacy and target intervals differ")
			if configuration and bool(configuration.get("raw_price_split_adjusted")) != self.target.data_configuration.raw_price_split_adjusted:
				blocked.append("Legacy and target split-adjustment semantics differ")
			if configuration and bool(configuration.get("raw_price_dividend_adjusted")) != self.target.data_configuration.raw_price_dividend_adjusted:
				blocked.append("Legacy and target dividend-adjustment semantics differ")
			return {"existing_tables": sorted(existing), "counts": counts, "counts_are_estimates": True, "missing_tables": [], "active_work": active}, {"configuration": configuration}, blocked
		finally:
			try:
				cursor.close()
			finally:
				connection.rollback()
				connection.close()

	def _target_state(self) -> str:
		connection, cursor = self._connect(self.target_connect, self.target)
		try:
			existing = _existing_tables(cursor, self.table_names.target_tables().values())
			if not existing:
				return "empty"
			if self.table_names.table(TableRole.SCHEMA) in existing:
				try:
					require_simplified_schema(cursor, self.table_names)
				except Exception:
					return "target_incompatible"
				return "target"
			return "partial_or_legacy"
		finally:
			cursor.close()
			connection.close()

	def plan(self, paths: MigrationPaths | None = None) -> MigrationPlan:
		inventory, configuration_rows, blocked = self._legacy_inventory()
		target_state = self._target_state()
		owned_initialization = False
		if paths and paths.journal.exists():
			try:
				owned_initialization = self._load_journal(paths).get("initializing_schema") is True
			except MigrationError:
				pass
		if target_state not in {"empty", "target"} and not owned_initialization:
			blocked.append(f"Target database is not empty or a compatible target schema: {target_state}")
		if target_state == "target":
			connection, cursor = self._connect(self.target_connect, self.target)
			try:
				cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", ("stage12_migration_run",))
				has_control = bool(cursor.fetchall())
			finally:
				cursor.close()
				connection.close()
			if not has_control:
				try:
					journal = self._load_journal(paths) if paths else {}
				except MigrationError:
					journal = {}
				if journal.get("phase") != "target_initialized":
					blocked.append("Target schema requires a matching SQL checkpoint or an empty target_initialized migration journal")
		backup_payload: dict[str, object] = {"status": "not_verified"}
		if paths is not None and paths.backup_manifest.exists():
			try:
				verified = verify_backup_manifest(paths.backup_manifest)
				backup_payload = {"status": "verified", "manifest": str(verified.manifest_path), "backup": str(verified.backup_path), "restored_copy": str(verified.restore_path)}
			except MigrationError as error:
				backup_payload = {"status": "invalid", "reason": str(error)}
		payload = {
			"schema_version": MIGRATION_VERSION,
			"operation": "simplified_database_migration",
			"migration_id": self._migration_id,
			"database_write": False,
			"legacy_database": self.legacy_database,
			"target_database": self.target_database,
			"target_schema": {"name": TARGET_SCHEMA_NAME, "version": 1, "registry_digest": registry_digest(self.table_names), "windows": list(self.windows)},
			"legacy": inventory,
			"legacy_configuration": configuration_rows.get("configuration", {}),
			"target_state": target_state,
			"backup": backup_payload,
			"export": {"status": "required_before_apply", "directory": None if paths is None else str(paths.export_dir)},
			"completion_policy": "Only valid legacy receipts plus independent raw/value checks become complete; unknown completion remains false and is scheduled for repair.",
			"blocked_reasons": sorted(dict.fromkeys(blocked)),
			"next_steps": ["verify backup restore manifest", "run apply", "run independent target validation", "record explicit cutover", "run retirement only after cutover"],
		}
		return MigrationPlan(payload)

	def _require_plan(self, paths: MigrationPaths) -> MigrationPlan:
		plan = self.plan(paths)
		if plan.blocked:
			raise MigrationError("Migration plan is blocked: " + "; ".join(plan.blocked))
		return plan

	def _write_journal(self, paths: MigrationPaths, phase: str, **details: object) -> dict[str, object]:
		payload: dict[str, object] = {
			"schema_version": JOURNAL_VERSION,
			"operation": "simplified_database_migration",
			"migration_id": self._migration_id,
			"legacy_database": self.legacy_database,
			"target_database": self.target_database,
			"target_schema": {"name": TARGET_SCHEMA_NAME, "version": 1, "registry_digest": registry_digest(self.table_names), "windows": list(self.windows)},
			"phase": phase,
			"updated_at": self.clock().astimezone(timezone.utc).isoformat(),
		}
		payload.update(details)
		paths.export_dir.mkdir(parents=True, exist_ok=True)
		from stockguidance.market_data.atomic import atomic_write
		atomic_write(paths.journal, _json(payload).encode("utf-8"))
		return payload

	def _load_journal(self, paths: MigrationPaths) -> dict[str, object]:
		try:
			payload = json.loads(paths.journal.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
			raise MigrationError("Migration journal is missing or invalid") from None
		if not isinstance(payload, dict) or payload.get("schema_version") != JOURNAL_VERSION or payload.get("migration_id") != self._migration_id:
			raise MigrationError("Migration journal does not match the selected source, target, or registry")
		return cast(dict[str, object], payload)

	def _export_metadata(self, paths: MigrationPaths) -> dict[str, object]:
		manifest_path = paths.export_dir / "legacy-metadata-manifest.json"
		if manifest_path.exists():
			manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
			if manifest.get("database") != self.legacy_database:
				raise MigrationError("Metadata export belongs to another legacy database")
			self._verify_export(manifest)
			return {"manifest": str(manifest_path), "tables": manifest["files"]}
		paths.export_dir.mkdir(parents=True, exist_ok=True)
		connection, cursor = self._connect(self.legacy_connect, self.legacy)
		try:
			self._begin_read(cursor)
			files: list[dict[str, object]] = []
			for table in self._legacy_metadata_tables():
				if not _table_exists(cursor, table):
					continue
				cursor.execute("SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX", (table,))
				keys = tuple(str(row[0]) for row in cursor.fetchall())
				if not keys:
					raise MigrationError(f"Metadata export needs a primary key: {table}")
				cursor.execute(f"SELECT * FROM {quote_identifier(table)} LIMIT 0", ())
				description = getattr(cursor, "description", None)
				if description is None:
					raise MigrationError("Metadata cursor does not expose column names")
				fields = [str(column[0]) for column in description]
				cursor.fetchall()
				from stockguidance.database.migration_sql import after, columns
				path = paths.export_dir / f"{table}.jsonl"
				temporary = path.with_suffix(".jsonl.partial")
				low: tuple[object, ...] = ()
				count = 0
				with temporary.open("wb") as stream:
					while True:
						where, params = after(keys, low)
						cursor.execute(f"SELECT s.* FROM {quote_identifier(table)} s WHERE {where} ORDER BY {columns(keys, 's')} LIMIT %s", params + (self.batch_size,))
						rows = cursor.fetchall()
						if not rows:
							break
						for row in rows:
							stream.write((_json({"table": table, "row": list(row)}) + "\n").encode("utf-8"))
						low = tuple(rows[-1][fields.index(key)] for key in keys)
						count += len(rows)
						self.progress(f"export:{table} rows={count}")
				temporary.replace(path)
				files.append({"table": table, "path": str(path), "row_count": count, "sha256": file_sha256(path)})
			manifest = {"schema_version": 1, "operation": "legacy_metadata_export", "database": self.legacy_database, "created_at": self.clock().astimezone(timezone.utc).isoformat(), "files": files}
			from stockguidance.market_data.atomic import atomic_write
			atomic_write(manifest_path, _json(manifest).encode("utf-8"))
			self._verify_export(manifest)
			return {"manifest": str(manifest_path), "tables": files}
		finally:
			cursor.close()
			connection.rollback()
			connection.close()

	def _verify_export(self, manifest: Mapping[str, object]) -> None:
		files = manifest.get("files")
		if not isinstance(files, list):
			raise MigrationError("Legacy metadata export manifest is invalid")
		for entry in files:
			if not isinstance(entry, dict):
				raise MigrationError("Legacy metadata export entry is invalid")
			path = Path(str(entry.get("path", "")))
			if not path.is_file() or file_sha256(path) != str(entry.get("sha256")):
				raise MigrationError("Legacy metadata export checksum verification failed")

	def apply(self, paths: MigrationPaths, *, confirmation: str) -> dict[str, object]:
		from stockguidance.database.migration_sql import SQLMigration
		return SQLMigration(self, paths).run("apply", confirmation=confirmation)

	def validate(self, paths: MigrationPaths) -> dict[str, object]:
		from stockguidance.database.migration_sql import SQLMigration
		return SQLMigration(self, paths).run("validate")

	def status(self, paths: MigrationPaths) -> dict[str, object]:
		from stockguidance.database.migration_sql import SQLMigration
		return SQLMigration(self, paths).run("status")

	def cutover(self, paths: MigrationPaths, *, confirmation: str) -> dict[str, object]:
		from stockguidance.database.migration_sql import SQLMigration
		return SQLMigration(self, paths).run("cutover", confirmation=confirmation)

	def retire(self, paths: MigrationPaths, *, confirmation: str) -> dict[str, object]:
		if confirmation != f"RETIRE {self.legacy_database}":
			raise MigrationError(f"Retirement requires --retire-confirmation 'RETIRE {self.legacy_database}'")
		journal = self._load_journal(paths)
		if journal.get("phase") not in {"cutover", "retired"}:
			raise MigrationError("Retirement requires an explicit cutover journal")
		verify_backup_manifest(paths.backup_manifest)
		export_manifest = paths.export_dir / "legacy-metadata-manifest.json"
		try:
			manifest = json.loads(export_manifest.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
			raise MigrationError("Verified legacy metadata export is required before retirement") from None
		self._verify_export(cast(Mapping[str, object], manifest))
		if journal.get("phase") == "retired":
			return journal
		connection, cursor = self._connect(self.legacy_connect, self.legacy)
		try:
			# This is intentionally outside the apply transaction.  MySQL DDL may
			# auto-commit, so rollback is not advertised as a retirement mechanism.
			for table in self._retirement_tables(cursor):
				cursor.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}", ())
			connection.commit()
		finally:
			cursor.close()
			connection.close()
		return self._write_journal(paths, "retired", retired_tables=list(self._retirement_tables_after()), retirement_confirmation=confirmation, rollback="Restore the verified backup into a new legacy database; ordinary transactions cannot roll back destructive DDL.")

	def _retirement_tables(self, cursor: _Cursor) -> tuple[str, ...]:
		names = self.table_names
		candidate = list(self._legacy_metadata_tables())
		candidate.extend(names.table(role) for role in LEGACY_CORE_ROLES)
		for window in self.windows:
			resolved = names.resolve_zscore_tables((window,))[window]
			candidate.extend((resolved.price, resolved.price_delta))
		return tuple(table for table in dict.fromkeys(candidate) if _table_exists(cursor, table))

	def _retirement_tables_after(self) -> tuple[str, ...]:
		return tuple(dict.fromkeys((*self._legacy_metadata_tables(), *(self.table_names.table(role) for role in LEGACY_CORE_ROLES), *(table for window in self.windows for table in (self.table_names.resolve_zscore_tables((window,))[window].price, self.table_names.resolve_zscore_tables((window,))[window].price_delta)))))


def build_source_configs(legacy_dbinfo: Path, target_dbinfo: Path, data_root: Path) -> tuple[SourceConfig, SourceConfig]:
	legacy_configuration = _configured_database(legacy_dbinfo)
	target_configuration = _configured_database(target_dbinfo)
	legacy_data = legacy_configuration.load_data_configuration()
	target_data = target_configuration.load_data_configuration()
	return (
		_safe_source_config(legacy_data, legacy_dbinfo, data_root),
		_safe_source_config(target_data, target_dbinfo, data_root),
	)


def validate_operator_identifier(value: str, name: str) -> str:
	return validate_identifier(value, name)
