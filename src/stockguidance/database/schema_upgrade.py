"""Explicit, resumable upgrade of a validated Stage 12 MySQL target to registry v3."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from stockguidance.database.migration_sql import ENGINE_VERSION, RUN, SQLMigration
from stockguidance.database.simplified_migration import MigrationError, MigrationPaths, _json, verify_backup_manifest
from stockguidance.database.simplified_schema import REGISTRY_VERSION, registry_digest, require_simplified_schema, retired_table_names, simplified_schema_statements
from stockguidance.database.table_names import TableRole, quote_identifier


UPGRADES = "stockguidance_schema_upgrades"
OLD_REGISTRY_VERSION = 2


def old_registry_digest(names: Any) -> str:
	payload = {"tables": names.target_tables(), "zscore_templates": names.templates()}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _identity(owner: Any, server_uuid: str, registry: str) -> dict[str, object]:
	return {"engine": ENGINE_VERSION, "migration": owner._migration_id, "server_uuid": server_uuid, "registry": registry}


def _volume_ddl(names: Any, windows: tuple[int, ...]) -> dict[str, str]:
	selected = {names.resolve_zscore_tables((window,))[window].volume for window in windows}
	return {name: statement for statement in simplified_schema_statements(names, windows)
		for name in selected if statement.startswith(f"CREATE TABLE IF NOT EXISTS {quote_identifier(name)} ")}


def _volume_shape(engine: SQLMigration, name: str, *, require_empty: bool = False) -> None:
	rows = engine.rows("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (engine.owner.target_database, name))
	if rows != [("InnoDB",)]:
		raise MigrationError(f"Volume table {name} is absent or is not InnoDB")
	columns = engine.rows("SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (engine.owner.target_database, name))
	expected = [("date_id", "int unsigned", "NO"), ("ticker_id", "int unsigned", "NO"), ("zscore", "float", "YES"),
		("status", "tinyint unsigned", "NO"), ("quality_flags", "smallint unsigned", "NO"), ("gap_length", "int unsigned", "NO"), ("provenance", "json", "NO")]
	if columns != expected:
		raise MigrationError(f"Volume table {name} columns differ from v3 DDL")
	defaults = engine.rows("SELECT COLUMN_NAME,COLUMN_DEFAULT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME IN ('quality_flags','gap_length') ORDER BY COLUMN_NAME", (engine.owner.target_database, name))
	if [(str(column), str(default)) for column, default in defaults] != [("gap_length", "0"), ("quality_flags", "0")]:
		raise MigrationError(f"Volume table {name} defaults differ from v3 DDL")
	primary = engine.rows("SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX", (engine.owner.target_database, name))
	if primary != [("date_id",), ("ticker_id",)]:
		raise MigrationError(f"Volume table {name} primary key differs from v3 DDL")
	fks = engine.rows("SELECT COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY COLUMN_NAME", (engine.owner.target_database, name))
	names = engine.names
	if fks != [("date_id", names.sessions, "id"), ("ticker_id", names.tickers, "id")]:
		raise MigrationError(f"Volume table {name} foreign keys differ from v3 DDL")
	if require_empty and engine.scalar(f"SELECT EXISTS(SELECT 1 FROM {engine.target(name)} LIMIT 1)"):
		raise MigrationError(f"Volume table {name} is populated before upgrade completion")


def verify_upgrade_lineage(engine: SQLMigration, stored_identity: object) -> bool:
	"""Accept immutable v2 control identity only with a committed v3 upgrade record."""
	try:
		stored = json.loads(str(stored_identity))
	except (TypeError, ValueError):
		return False
	if stored == json.loads(engine.identity()):
		return True
	old = old_registry_digest(engine.names)
	if stored != _identity(engine.owner, engine.server_uuid, old):
		return False
	if not engine.scalar("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (engine.owner.target_database, UPGRADES)):
		return False
	rows = engine.rows(f"SELECT old_registry_version,old_registry_digest,new_registry_version,new_registry_digest,identity_json FROM {engine.target(UPGRADES)} WHERE id=1")
	if len(rows) != 1 or tuple(rows[0][:4]) != (OLD_REGISTRY_VERSION, old, REGISTRY_VERSION, registry_digest(engine.names)) or json.loads(str(rows[0][4])) != stored:
		return False
	try:
		require_simplified_schema(engine.cursor, engine.names)
	except Exception:
		return False
	for name in _volume_ddl(engine.names, engine.owner.windows):
		_volume_shape(engine, name)
	return True


def upgrade(owner: Any, paths: MigrationPaths, *, confirmation: str, interrupt_after_ddl: bool = False) -> dict[str, object]:
	"""Perform only the v2->v3 additive upgrade; never cut over or copy source rows."""
	if confirmation != f"UPGRADE {owner.target_database}":
		raise MigrationError(f"Upgrade requires --upgrade-confirmation 'UPGRADE {owner.target_database}'")
	if REGISTRY_VERSION != 3:
		raise MigrationError("This upgrade supports only registry version 3")
	verify_backup_manifest(paths.backup_manifest)
	journal = owner._load_journal(paths)
	if journal.get("phase") != "validated" or journal.get("target_schema", {}).get("registry_digest") != old_registry_digest(owner.table_names):
		raise MigrationError("Upgrade requires the validated v2 journal and registry")
	engine = SQLMigration(owner, paths)
	with engine.session(write=True):
		rows = engine.rows(f"SELECT identity_json,phase FROM {engine.target(RUN)} WHERE id=1 FOR UPDATE")
		if len(rows) != 1 or rows[0][1] != "validated":
			raise MigrationError("Upgrade requires one validated, uncutover migration control row")
		identity = json.loads(str(rows[0][0]))
		old = old_registry_digest(owner.table_names)
		if identity != _identity(owner, engine.server_uuid, old):
			raise MigrationError("Validated control identity is not the expected v2 migration")
		if engine.scalar(f"SELECT COUNT(*) FROM {engine.target(owner.table_names.pending_updates)}"):
			raise MigrationError("Pending target operation blocks schema upgrade")
		required = set(owner.table_names.target_tables().values()) | {RUN, "stage12_migration_steps"}
		for pair in owner.table_names.resolve_zscore_tables(owner.windows).values():
			required.update((pair.price, pair.price_delta))
		existing = {str(row[0]) for row in engine.rows("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (owner.target_database,))}
		if not required.issubset(existing) or existing.intersection(retired_table_names(owner.table_names)):
			raise MigrationError("Validated v2 target tables are missing or mixed with retired tables")
		if not engine.scalar(f"SELECT COUNT(*) FROM {engine.target('stage12_migration_steps')} WHERE step='completion:sessions' AND complete=1"):
			raise MigrationError("Migration copy/completion is not checkpointed")
		if engine.scalar(f"SELECT COUNT(*) FROM {engine.target('stage12_migration_steps')} WHERE complete=0"):
			raise MigrationError("Migration has incomplete checkpoints")
		if not engine.scalar(f"SELECT COUNT(*) FROM {engine.target('stage12_migration_steps')} WHERE step LIKE 'validate:%%' AND complete=1"):
			raise MigrationError("Validated target has no completed validation checkpoints")
		meta = engine.rows(f"SELECT schema_name,schema_version,registry_version,registry_digest FROM {engine.target(owner.table_names.schema)} WHERE id=1 FOR UPDATE")
		if len(meta) != 1 or meta[0][:2] != ("stockguidance_simplified", 1):
			raise MigrationError("Target schema metadata is missing or unexpected")
		new = registry_digest(owner.table_names)
		if tuple(meta[0][2:]) == (REGISTRY_VERSION, new):
			if not verify_upgrade_lineage(engine, rows[0][0]):
				raise MigrationError("v3 schema exists without valid upgrade lineage")
			return {"phase": "already_upgraded", "old_registry_digest": old, "new_registry_digest": new}
		if tuple(meta[0][2:]) != (OLD_REGISTRY_VERSION, old):
			raise MigrationError("Target is neither the expected v2 nor committed v3 registry")
		# DDL commits on MySQL. Each retry verifies any table left by an interruption.
		engine.connection.rollback()
		engine.assert_lock()
		engine.execute(f"CREATE TABLE IF NOT EXISTS {engine.target(UPGRADES)} (id TINYINT UNSIGNED NOT NULL PRIMARY KEY, old_registry_version INT UNSIGNED NOT NULL, old_registry_digest CHAR(64) NOT NULL, new_registry_version INT UNSIGNED NOT NULL, new_registry_digest CHAR(64) NOT NULL, identity_json JSON NOT NULL, completed_at DATETIME(6) NOT NULL, CHECK(id=1)) ENGINE=InnoDB")
		if engine.scalar(f"SELECT COUNT(*) FROM {engine.target(UPGRADES)}"):
			raise MigrationError("Upgrade audit row exists while target still declares v2")
		for name, ddl in _volume_ddl(owner.table_names, owner.windows).items():
			engine.assert_lock()
			if not engine.scalar("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (owner.target_database, name)):
				engine.execute(ddl)
			_volume_shape(engine, name, require_empty=True)
			if interrupt_after_ddl:
				raise MigrationError("Injected interruption after volume DDL")
		engine.assert_lock()
		engine.execute(f"UPDATE {engine.target(owner.table_names.schema)} SET registry_version=%s,registry_digest=%s WHERE id=1 AND registry_version=%s AND registry_digest=%s", (REGISTRY_VERSION, new, OLD_REGISTRY_VERSION, old))
		engine.execute(f"INSERT INTO {engine.target(UPGRADES)} (id,old_registry_version,old_registry_digest,new_registry_version,new_registry_digest,identity_json,completed_at) VALUES (1,%s,%s,%s,%s,%s,%s)", (OLD_REGISTRY_VERSION, old, REGISTRY_VERSION, new, _json(identity), datetime.now(timezone.utc).replace(tzinfo=None)))
		engine.assert_lock()
		engine.connection.commit()
		if not verify_upgrade_lineage(engine, rows[0][0]):
			raise MigrationError("Committed schema upgrade failed independent lineage verification")
		return {"phase": "upgraded", "old_registry_digest": old, "new_registry_digest": new}
