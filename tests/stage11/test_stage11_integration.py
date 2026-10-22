from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore, evidence_for_file
from stockguidance.legacy_migration.precision import ArchiveFileInventory, exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id
from stockguidance.performance.stage11_integration import Stage11Context, Stage11Error, Stage11Store


def _fixture(tmp_path: Path) -> tuple[Stage11Context, Path]:
	data_root = tmp_path / "data"
	data_root.mkdir()
	evidence_root = tmp_path / "evidence"
	files = []
	for index, session in enumerate(("2024-07-01", "2024-07-02", "2024-07-03")):
		timestamp = int(datetime.fromisoformat(session).replace(tzinfo=timezone.utc).timestamp() * 1000) + 16 * 3600000
		path = data_root / f"{session}.ndjson"
		path.write_text(
			json.dumps({"T": "AAP", "o": 10 + index, "h": 12 + index, "l": 9 + index, "c": 11 + index, "v": 100.4 + index, "vw": 10.5 + index, "n": 10, "t": timestamp}) + "\n"
			+ json.dumps({"T": "AAp", "o": 20 + index, "h": 22 + index, "l": 19 + index, "c": 21 + index, "v": 200.4 + index, "vw": 20.5 + index, "n": None, "t": timestamp}) + "\n",
			encoding="utf-8",
		)
		bars, file_hash = read_exact_archive(path)
		records_id = exact_records_id(bars)
		item = ArchiveFileInventory(str(path.resolve()), session, file_hash, True, len(bars), records_id, tuple(bar.ticker for bar in bars))
		evidence = evidence_for_file(ARCHIVE_SOURCE, "Stage 11 fixture attestation", item, datetime(2026, 10, 20, tzinfo=timezone.utc))
		ArchiveEvidenceStore(evidence_root).save(evidence)
		files.append({"session": session, "path": str(path.resolve()), "valid": True, "file_sha256": file_hash, "records_id": records_id, "record_count": len(bars)})
	manifest = {
		"schema_version": 1,
		"stage": 6,
		"manifest_id": "fixture",
		"inputs": {"data_root": str(data_root)},
		"files": files,
		"targets": [{"session": "2024-07-02", "derived_dependencies": ["2024-07-01", "2024-07-03"], "zscore_dependencies": ["2024-07-01", "2024-07-02", "2024-07-03"]}],
		"mappings": {"ticker_mappings": [{"id": 11, "label": "AAP"}, {"id": 13, "label": "AAp"}], "date_mappings": [{"id": 1001 + index, "session": session} for index, session in enumerate(("2024-07-01", "2024-07-02", "2024-07-03"))]},
		"authoritative_registry": {"tables": {"source": "stockguidance_source", "sessions": "transaction_dates", "tickers": "ticker_symbol", "prices": "raw_stock_data", "deltas": "daily_delta_stock_data", "imports": "stockguidance_imports", "zscore_series": "stockguidance_zscore_series", "zscore_sessions": "stockguidance_zscore_sessions"}, "zscore_templates": {"price": "zscores_price_{window}", "price_delta": "zscores_price_delta_{window}"}},
		"source": {"archive_source": {"provider": "polygon", "endpoint": "/v2/aggs/grouped/locale/us/market/stocks/{date}", "market": "us-stocks", "universe": "us-stocks-excluding-otc", "adjustment": "adjusted", "interval": "1d"}},
		"snapshot_identity": {"source_key": "a" * 64, "audit_sha256": "b" * 64},
		"corrected_schema_hash": "c" * 64,
	}
	manifest["manifest_id"] = content_id({key: value for key, value in manifest.items() if key != "manifest_id"})
	decision = {"rollout": {"transactions": {"raw": "one session", "derived": "one session", "zscores": "one series/session"}}, "workloads": {"raw_adoption_repair": {"selected_backend": "python"}, "derived_computation_persistence": {"selected_backend": "java"}, "zscores_persistence": {"selected_backend": "java"}}}
	context = Stage11Context(decision, manifest, {}, tmp_path / "decision.json", tmp_path / "manifest.json", tmp_path / "audit.json", evidence_root, datetime(2026, 10, 22, tzinfo=timezone.utc), manifest["manifest_id"], "d" * 64, "e" * 64, "f" * 64)
	return context, tmp_path / "disposable.sqlite"


def test_raw_adoption_is_exact_case_idempotent_and_invalidates_dependencies(tmp_path):
	context, database = _fixture(tmp_path)
	runner = Stage11Store(context, database)
	runner.initialize()
	for session in ("2024-07-01", "2024-07-02", "2024-07-03"):
		runner.adopt_raw(session)
	runner.write_derived("2024-07-02")
	matching = runner.adopt_raw("2024-07-02")
	assert matching.changed_rows == 0
	with runner._connect() as connection:
		before = connection.execute("SELECT derived_content_id FROM stockguidance_imports WHERE session_date = '2024-07-02'").fetchone()[0]
		connection.execute("DELETE FROM raw_stock_data WHERE date_id = 1002 AND ticker_id = 11")
		connection.execute("UPDATE raw_stock_data SET close = '999' WHERE date_id = 1002 AND ticker_id = 13")
	with runner._connect() as connection:
		assert connection.execute("SELECT derived_content_id FROM stockguidance_imports WHERE session_date = '2024-07-02'").fetchone()[0] == before
	repaired = runner.adopt_raw("2024-07-02")
	assert repaired.changed_rows == 2
	assert repaired.invalidated_derived == ("2024-07-02",)
	with runner._connect() as connection:
		labels = connection.execute("SELECT t.label FROM raw_stock_data p JOIN ticker_symbol t ON t.id = p.ticker_id WHERE p.date_id = 1002 ORDER BY t.label").fetchall()
		receipt = connection.execute("SELECT provenance_kind, original_receipt_at FROM stage11_adoption_receipts WHERE session_date = '2024-07-02'").fetchone()
	assert [row[0] for row in labels] == ["AAP", "AAp"]
	assert receipt == ("adopted_archive_snapshot", None)


def test_stage11_before_commit_rolls_back_and_zscores_publish_dense_rows(tmp_path):
	context, database = _fixture(tmp_path)
	runner = Stage11Store(context, database)
	runner.initialize()
	for session in ("2024-07-01", "2024-07-02", "2024-07-03"):
		runner.adopt_raw(session)
	with pytest.raises(RuntimeError, match="before_commit"):
		runner.write_derived("2024-07-02", interruption="before_commit")
	with runner._connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM daily_delta_stock_data").fetchone()[0] == 0
	runner.write_derived("2024-07-02")
	zscores = runner.write_zscores("2024-07-02")
	assert zscores["series"] == 4
	with runner._connect() as connection:
		for table in ("zscores_price_20", "zscores_price_60", "zscores_price_delta_20", "zscores_price_delta_60"):
			assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE date_id = 1002").fetchone()[0] == 2


def test_stage11_rejects_incomplete_or_future_cutoffs(tmp_path):
	context, database = _fixture(tmp_path)
	with pytest.raises(Stage11Error):
		Stage11Store(context, database).adopt_raw("missing")
