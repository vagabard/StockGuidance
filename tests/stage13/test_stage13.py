import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stockguidance.performance.stage13 import Stage13Error, _chunked, _population_manifest


def _base(tmp_path: Path) -> dict:
	return {
		"schema_version": 1,
		"stage": 6,
		"inputs": {"data_root": str(tmp_path)},
		"snapshot_identity": {"audit_sha256": "a" * 64, "source_key": "b" * 64},
		"source": {"archive_source": {"provider": "polygon"}},
		"authoritative_registry": {"tables": {}, "zscore_templates": {}},
		"mappings": {"ticker_mappings": [], "date_mappings": []},
	}


def _item(tmp_path: Path, session: str, valid: bool, index: int) -> dict:
	path = tmp_path / f"{session}.ndjson"
	path.write_text("{}\n", encoding="utf-8")
	return {"session": session, "path": str(path), "file_sha256": f"{index:064x}", "records_id": f"{index + 10:064x}", "record_count": 1, "valid": valid, "finding": None if valid else "invalid"}


def test_population_manifest_marks_invalid_neighbor_without_synthetic_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	files = [_item(tmp_path, "2026-01-02", True, 1), _item(tmp_path, "2026-01-05", False, 2), _item(tmp_path, "2026-01-06", True, 3)]
	audit = {"sessions": [{"session": item["session"], "file_valid": item["valid"], "file_sha256": item["file_sha256"]} for item in files]}
	precision = {"files": files}
	class Evidence:
		def verify(self, *args):
			return type("Proof", (), {"validated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})()
	monkeypatch.setattr("stockguidance.performance.stage13.ArchiveEvidenceStore", lambda path: Evidence())
	manifest, scope = _population_manifest(_base(tmp_path), precision, audit, tmp_path, datetime(2026, 1, 2, tzinfo=timezone.utc))
	target = next(item for item in manifest["targets"] if item["session"] == "2026-01-06")
	assert target["derived_dependencies"] == []
	assert scope["missing_dependencies"]


def test_chunk_limit_is_bounded():
	assert list(_chunked(("1", "2", "3"), 2)) == [("1", "2"), ("3",)]
	with pytest.raises(Stage13Error):
		list(_chunked(("1",), 21))


def test_population_manifest_identity_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	files = [_item(tmp_path, "2026-01-02", True, 1), _item(tmp_path, "2026-01-05", True, 2)]
	audit = {"sessions": [{"session": item["session"], "file_valid": True, "file_sha256": item["file_sha256"]} for item in files]}
	precision = {"files": files}
	class Evidence:
		def verify(self, *args):
			return type("Proof", (), {"validated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})()
	monkeypatch.setattr("stockguidance.performance.stage13.ArchiveEvidenceStore", lambda path: Evidence())
	first, _ = _population_manifest(_base(tmp_path), precision, audit, tmp_path, datetime(2026, 1, 2, tzinfo=timezone.utc))
	second, _ = _population_manifest(_base(tmp_path), precision, audit, tmp_path, datetime(2026, 1, 2, tzinfo=timezone.utc))
	assert first["manifest_id"] == second["manifest_id"]
	assert first["manifest_id"] == __import__("stockguidance.market_data.identity", fromlist=["content_id"]).content_id({key: value for key, value in first.items() if key != "manifest_id"})
