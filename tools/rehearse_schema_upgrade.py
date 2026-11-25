"""Rehearse the v2-to-v3 upgrade on newly created disposable MySQL schemas."""

from __future__ import annotations

import argparse
from pathlib import Path
import hashlib
import json
import sys
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.connections import MySQLConnectionFactory  # noqa: E402
from stockguidance.database.migration_sql import ENGINE_VERSION, RUN, SQLMigration  # noqa: E402
from stockguidance.database.schema_upgrade import old_registry_digest, upgrade, verify_upgrade_lineage  # noqa: E402
from stockguidance.database.simplified_migration import MigrationError, MigrationPaths, SimplifiedDatabaseMigration, _json, build_source_configs  # noqa: E402
from stockguidance.database.simplified_schema import initialize_simplified_schema, registry_digest, require_simplified_schema  # noqa: E402


def run(dbinfo: Path, prefix: str, output_dir: Path) -> dict[str, object]:
	if not prefix.startswith("stockguidance_disposable_") or not prefix.replace("_", "").isalnum() or len(prefix) > 48:
		raise ValueError("Use a new stockguidance_disposable_* prefix of at most 48 characters")
	config = load_database_configuration(dbinfo)
	legacy_name, target_name = prefix + "_old", prefix + "_new"
	if config.database.casefold() in {legacy_name.casefold(), target_name.casefold()}:
		raise ValueError("Disposable names must differ from configured database")
	output_dir.mkdir(parents=True, exist_ok=True)
	connect = MySQLConnectionFactory()
	base = connect(dbinfo)
	q: Any = base.cursor()
	try:
		for name in (legacy_name, target_name):
			q.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (name,))
			if q.fetchall()[0][0]:
				raise ValueError("Disposable schema already exists: " + name)
		for name in (legacy_name, target_name):
			q.execute(f"CREATE DATABASE `{name}`")
		base.commit()
	finally:
		q.close(); base.close()
	try:
		for name, path in ((legacy_name, output_dir / "legacy-dbinfo.json"), (target_name, output_dir / "target-dbinfo.json")):
			path.write_text(json.dumps({"host": config.host, "port": config.port, "database": name, "user": config.user,
				"password": config.password, "data_configuration_file": str(config.data_configuration_file)}), encoding="utf-8")
		legacy, target = build_source_configs(output_dir / "legacy-dbinfo.json", output_dir / "target-dbinfo.json", ROOT / "data")
		initialize_simplified_schema(target, connect, windows=(20, 60))
		migration = SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(20, 60))
		old = old_registry_digest(migration.table_names)
		c = connect(target.database_config); q = c.cursor()
		try:
			q.execute("SELECT @@server_uuid"); uuid = str(q.fetchall()[0][0])
			q.execute("INSERT INTO transaction_dates (id,transaction_date,raw_complete,delta_complete) VALUES (1,'2024-07-01',1,1)")
			q.execute("INSERT INTO ticker_symbol (id,label,exchange) VALUES (1,'FIXTURE','XNYS')")
			q.execute("INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions,state_id) VALUES (1,1,10,11,9,10,100,10,5,1)")
			q.execute("INSERT INTO zscores_price_20 (date_id,ticker_id,zscore,status,provenance) VALUES (1,1,1.25,0,JSON_OBJECT())")
			c.commit()
			for window in (20, 60):
				q.execute(f"DROP TABLE `zscores_volume_{window}`")
			q.execute("UPDATE stockguidance_schema_version SET registry_version=2,registry_digest=%s WHERE id=1", (old,))
			identity = {"engine": ENGINE_VERSION, "migration": migration._migration_id, "server_uuid": uuid, "registry": old}
			q.execute("CREATE TABLE stage12_migration_run (id TINYINT NOT NULL PRIMARY KEY, identity_json JSON NOT NULL, migration_time DATETIME(6) NOT NULL, phase VARCHAR(32) NOT NULL) ENGINE=InnoDB")
			q.execute("CREATE TABLE stage12_migration_steps (step VARCHAR(128) NOT NULL PRIMARY KEY, last_key JSON NOT NULL, committed_rows BIGINT UNSIGNED NOT NULL, complete TINYINT NOT NULL, report JSON NULL) ENGINE=InnoDB")
			q.execute("INSERT INTO stage12_migration_run VALUES (1,%s,NOW(6),'validated')", (_json(identity),))
			q.execute("INSERT INTO stage12_migration_steps VALUES ('completion:sessions',JSON_ARRAY(),1,1,NULL)")
			q.execute("INSERT INTO stage12_migration_steps VALUES ('validate:completion',JSON_ARRAY(),1,1,NULL)")
			c.commit()
			q.execute("SELECT close,volume FROM raw_stock_data WHERE ticker_id=1 AND date_id=1"); before_raw = tuple(q.fetchall()[0])
			q.execute("SELECT zscore,status FROM zscores_price_20 WHERE ticker_id=1 AND date_id=1"); before_score = tuple(q.fetchall()[0])
		finally:
			q.close(); c.close()
		backup = output_dir / "synthetic-backup.sql"
		restore = output_dir / "synthetic-restore.sql"
		backup.write_bytes(b"Synthetic disposable fixture, not a production backup.\n")
		restore.write_bytes(backup.read_bytes())
		digest = hashlib.sha256(backup.read_bytes()).hexdigest()
		manifest = output_dir / "backup-manifest.json"
		manifest.write_text(json.dumps({"schema_version": 1, "backup_path": str(backup.resolve()), "backup_sha256": digest,
			"restore_path": str(restore.resolve()), "restore_sha256": digest, "restore_verified": True}), encoding="utf-8")
		paths = MigrationPaths.from_values(manifest, output_dir / "export")
		paths.export_dir.mkdir(parents=True, exist_ok=True)
		journal = migration._write_journal(paths, "validated", engine_version=ENGINE_VERSION)
		cast(dict[str, Any], journal["target_schema"])["registry_digest"] = old
		paths.journal.write_text(_json(journal), encoding="utf-8")
		try:
			upgrade(migration, paths, confirmation="UPGRADE wrong_database")
		except MigrationError as error:
			assert "Upgrade requires" in str(error)
		else:
			raise AssertionError("Incorrect target confirmation was accepted")
		try:
			upgrade(migration, paths, confirmation="UPGRADE " + target_name, interrupt_after_ddl=True)
		except MigrationError as error:
			assert "Injected interruption" in str(error), str(error)
		else:
			raise AssertionError("DDL interruption was not reached")
		c = connect(target.database_config); q = c.cursor()
		try:
			q.execute("SELECT registry_version,registry_digest FROM stockguidance_schema_version WHERE id=1")
			assert q.fetchall()[0] == (2, old), "Interrupted DDL promoted metadata"
		finally:
			q.close(); c.close()
		assert upgrade(migration, paths, confirmation="UPGRADE " + target_name)["phase"] == "upgraded"
		assert upgrade(migration, paths, confirmation="UPGRADE " + target_name)["phase"] == "already_upgraded"
		assert migration.status(paths)["phase"] == "validated"
		# Exercise the same checkpoint/reference path used before validation and cutover.
		engine = SQLMigration(migration, paths)
		with engine.session(write=True):
			engine.initialize_control()
		assert migration.status(paths)["phase"] == "validated"
		c = connect(target.database_config); q = c.cursor()
		try:
			require_simplified_schema(q, migration.table_names)
			q.execute("SELECT close,volume FROM raw_stock_data WHERE ticker_id=1 AND date_id=1"); assert tuple(q.fetchall()[0]) == before_raw
			q.execute("SELECT zscore,status FROM zscores_price_20 WHERE ticker_id=1 AND date_id=1"); assert tuple(q.fetchall()[0]) == before_score
			q.execute("SELECT identity_json FROM stage12_migration_run WHERE id=1"); assert json.loads(str(q.fetchall()[0][0])) == identity
			q.execute("SELECT registry_version,registry_digest FROM stockguidance_schema_version WHERE id=1"); assert q.fetchall()[0] == (3, registry_digest(migration.table_names))
		finally:
			q.close(); c.close()
		return {"status": "passed", "schemas": [legacy_name, target_name], "interruption_resumed": True,
			"repeat_run": "already_upgraded", "migration_phase": "validated", "original_control_identity_preserved": True,
			"existing_raw_and_zscore_rows_preserved": True, "checkpoint_initialization_after_upgrade": "passed",
			"wrong_confirmation_rejected": True, "new_volume_tables": ["zscores_volume_20", "zscores_volume_60"]}
	finally:
		base = connect(dbinfo); q = base.cursor()
		try:
			for name in (target_name, legacy_name):
				q.execute(f"DROP DATABASE IF EXISTS `{name}`")
			base.commit()
		finally:
			q.close(); base.close()
		for path in (output_dir / "legacy-dbinfo.json", output_dir / "target-dbinfo.json"):
			path.unlink(missing_ok=True)


def main() -> int:
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--dbinfo-file", type=Path, required=True)
	p.add_argument("--prefix", required=True)
	p.add_argument("--output-dir", type=Path, required=True)
	a = p.parse_args()
	result = run(a.dbinfo_file, a.prefix, a.output_dir)
	(a.output_dir / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
	print(json.dumps(result, indent=2))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
