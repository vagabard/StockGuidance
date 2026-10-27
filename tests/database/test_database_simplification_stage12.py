from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from stockguidance.config.source import DataConfiguration, SourceConfig
from stockguidance.database.simplified_migration import MigrationError, MigrationPaths, SimplifiedDatabaseMigration, verify_backup_manifest


def _manifest(path: Path, backup: Path, restored: Path, *, restored_ok: bool = True) -> None:
	path.write_text(json.dumps({
		"schema_version": 1,
		"backup_path": str(backup),
		"backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
		"restore_path": str(restored),
		"restore_sha256": hashlib.sha256(restored.read_bytes()).hexdigest(),
		"restore_verified": restored_ok,
	}), encoding="utf-8")


def test_backup_manifest_checks_backup_and_restored_copy(tmp_path: Path) -> None:
	backup = tmp_path / "backup.sql"
	restored = tmp_path / "restored.sql"
	backup.write_bytes(b"disposable backup")
	restored.write_bytes(backup.read_bytes())
	manifest = tmp_path / "manifest.json"
	_manifest(manifest, backup, restored)
	verified = verify_backup_manifest(manifest)
	assert verified.backup_path == backup.resolve()
	assert verified.restore_verified
	restored.write_bytes(b"changed")
	with pytest.raises(MigrationError, match="restored copy"):
		verify_backup_manifest(manifest)


def test_migration_phases_require_exact_confirmation(tmp_path: Path) -> None:
	configuration = tmp_path / "configuration.json"
	configuration.write_text(json.dumps({
		"schema_version": 1, "source_family": "fixture", "endpoint": "fixture",
		"market": "market", "universe": "universe", "interval": "1d",
		"raw_price_split_adjusted": False, "raw_price_dividend_adjusted": False,
		"raw_price_vendor_defined_adjustment": None,
		"verification_policy": {"source_family_mismatch": "block", "endpoint_mismatch": "advisory", "source_family_sample_days": 1, "endpoint_sample_pulls": 1},
	}), encoding="utf-8")
	legacy_dbinfo = tmp_path / "legacy.json"
	target_dbinfo = tmp_path / "target.json"
	legacy_dbinfo.write_text(json.dumps({"database": "legacy_fixture", "host": "fixture", "user": "fixture", "password": "", "data_configuration_file": str(configuration)}), encoding="utf-8")
	target_dbinfo.write_text(json.dumps({"database": "target_fixture", "host": "fixture", "user": "fixture", "password": "", "data_configuration_file": str(configuration)}), encoding="utf-8")
	identity = DataConfiguration("fixture", "fixture", "market", "universe", False, False, None).to_source_identity()
	legacy = SourceConfig(identity, tmp_path, legacy_dbinfo)
	target = SourceConfig(identity, tmp_path, target_dbinfo)
	migration = SimplifiedDatabaseMigration(legacy, target, lambda _path: None, lambda _path: None, windows=(2,))  # type: ignore[arg-type]
	paths = MigrationPaths.from_values(tmp_path / "backup.json", tmp_path / "export")
	with pytest.raises(MigrationError, match="Apply requires"):
		migration.apply(paths, confirmation="")
