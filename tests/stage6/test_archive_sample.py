from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockguidance.cli.compare_archive_sample import compare_sample
from stockguidance.cli.verify_archive_sample import verify_sample
from stockguidance.market_data.identity import encode_json
from stockguidance.performance.archive_read import read_archive


def archive(tmp_path: Path) -> Path:
	directory = tmp_path / "market_summary" / "2024"
	directory.mkdir(parents=True)
	for day in (1, 2, 3, 5, 8, 9, 10, 11, 12):
		observed = datetime(2024, 7, day, 20, tzinfo=timezone.utc)
		row = {"T": "AAA", "o": 10, "h": 12, "l": 9, "c": 11, "v": 1.25, "t": int(observed.timestamp() * 1000)}
		(directory / f"2024-07-{day:02}.ndjson").write_text(json.dumps(row) + "\n", encoding="utf-8")
	return tmp_path


def test_attestation_and_samples_do_not_fabricate_receipts(tmp_path: Path) -> None:
	report = verify_sample(archive(tmp_path), "Operator confirms direct adjusted Massive grouped downloads")
	assert report["source_accepted"]
	assert report["original_network_received_at"] is None
	assert not report["all_files_adopted"]
	assert len(report["samples"]) == 9
	assert all(sample["fractional_volume_count"] == 1 for sample in report["samples"])
	assert report["samples"] == verify_sample(tmp_path, report["operator_attestation"])["samples"]


def test_one_invalid_file_blocks_source_gate(tmp_path: Path) -> None:
	root = archive(tmp_path)
	(root / "market_summary/2024/2024-07-01.ndjson").write_text("{}\n", encoding="utf-8")
	report = verify_sample(root, "Operator attestation")
	assert not report["source_accepted"]
	assert sum(sample["passed"] for sample in report["samples"]) == 8


def test_reports_fractional_volume_loss_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	root = archive(tmp_path)
	acceptance = verify_sample(root, "Operator attestation")
	acceptance_file = tmp_path / "acceptance.json"
	acceptance_file.write_bytes(encode_json(acceptance))
	actions = []
	cursor = SimpleNamespace(execute=lambda query: actions.append(query), close=lambda: None)
	connection = SimpleNamespace(cursor=lambda: cursor, rollback=lambda: actions.append("rollback"), close=lambda: actions.append("close"))
	monkeypatch.setattr("stockguidance.cli.compare_archive_sample.streaming_connection", lambda config: connection)
	def stored(connection, config, session):
		bars, digest = read_archive(root / f"market_summary/{session.year}/{session.isoformat()}.ndjson")
		return tuple(replace(bar, volume=Decimal(1)) for bar in bars)
	monkeypatch.setattr("stockguidance.cli.compare_archive_sample.read_database", stored)
	report = compare_sample(acceptance_file, tmp_path / "unused")
	assert not report["matches"]
	assert not report["import_receipts_created"]
	assert all(session["field_difference_counts"]["volume"] == 1 for session in report["sessions"])
	assert actions == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "START TRANSACTION READ ONLY", "rollback", "close"]


def test_failed_gate_never_connects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	acceptance_file = tmp_path / "acceptance.json"
	acceptance_file.write_bytes(encode_json({"schema_version": 1, "source_accepted": False, "samples": []}))
	monkeypatch.setattr("stockguidance.cli.compare_archive_sample.streaming_connection", lambda config: pytest.fail("Unexpected database access"))
	with pytest.raises(ValueError, match="accepted"):
		compare_sample(acceptance_file, tmp_path / "unused")
