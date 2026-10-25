from dataclasses import replace
from datetime import date
from decimal import Decimal
import json

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.database.repositories.archive_catalog import ArchiveCatalog
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.market_data.archive import archive_content, archive_path, decode_archive
from stockguidance.market_data.canonical_store import CanonicalArchiveStore
from stockguidance.market_data.consolidation import conflicts
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.workflows.update_market_data import update_market_data


@pytest.fixture
def canonical(harness, calendar, monkeypatch):
	from contextlib import nullcontext
	from stockguidance.workflows import market_lock
	monkeypatch.setattr(market_lock, "market_update_lock", lambda *args: nullcontext())
	source = replace(harness.source, identity=replace(harness.source.identity, adjustment=PriceAdjustment.UNADJUSTED), _configuration=None)
	harness.database.sql("DELETE FROM stockguidance_source")
	initialize_update_schema(source, harness.database)
	store = CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, harness.database))
	repository = MySQLMarketDataRepository(source, harness.database)
	repository.metadata_only = True
	boundary = replace(harness.boundary, source=source.identity)
	for session in calendar.sessions(harness.settings.start, harness.settings.end):
		response = harness.response()
		harness.provider.responses[session.session_date] = replace(response, source_timestamps=(("AAA", int(session.opens_at.timestamp() * 1000)),))
	settings = replace(harness.settings, database_first=True)
	return source, store, repository, boundary, settings


def run(harness, canonical, calendar, **settings_changes):
	source, store, repository, boundary, settings = canonical
	return update_market_data(source, replace(settings, **settings_changes), boundary, calendar, harness.provider, repository, harness.clock, manifest_store=store)


def test_canonical_roundtrip_preserves_exact_values_and_creates_no_sidecars(harness, canonical, calendar):
	source, store, _, _, _ = canonical
	session = calendar.session(date(2024, 7, 1))
	response = harness.provider.responses[session.session_date]
	bar = replace(response.records[0], open=Decimal("10.12345678"), volume=Decimal("123.00000001"), vwap=Decimal("10.1234567890123456789012"))
	download = DownloadedSession(session, replace(response, records=(bar,)), harness.clock.now())
	manifest = store.save_download(source.identity, download)
	loaded = store.load(source.identity, session)
	assert loaded.download.response.records == (bar,)
	assert loaded.content_id == manifest.content_id
	assert list(source.data_root.rglob("*.manifest.json")) == []
	assert list(source.data_root.rglob("responses")) == []
	assert store.path_for(source.identity, session) == archive_path(source.data_root, session.session_date)


def test_completed_rerun_never_opens_archive_or_provider(harness, canonical, calendar, monkeypatch):
	assert run(harness, canonical, calendar).successful
	harness.provider.calls.clear()
	monkeypatch.setattr(canonical[1], "load", lambda *args: pytest.fail("Completed dates must not decode archives"))
	monkeypatch.setattr(canonical[1], "load_metadata", lambda *args: pytest.fail("Completed dates must not read archive metadata"))
	assert run(harness, canonical, calendar).successful
	assert harness.provider.calls == []


def test_partial_marker_cannot_count_as_complete(harness, canonical, calendar):
	assert run(harness, canonical, calendar).successful
	harness.database.sql("INSERT INTO stockguidance_daily_import_in_progress VALUES (?, ?)", ("2024-07-01", harness.clock.now().isoformat()))
	harness.provider.calls.clear()
	result = run(harness, canonical, calendar)
	assert result.successful
	assert harness.provider.calls == []
	assert harness.database.sql("SELECT * FROM stockguidance_daily_import_in_progress") == []


def test_interrupted_publication_never_reuses_old_file(harness, canonical, calendar, monkeypatch):
	source, store, _, _, _ = canonical
	session = calendar.session(date(2024, 7, 1))
	response = harness.provider.responses[session.session_date]
	download = DownloadedSession(session, response, harness.clock.now())
	store.save_download(source.identity, download)
	from stockguidance.market_data import canonical_store
	original_write = canonical_store.atomic_write
	def interrupt(path, content):
		if path == store.path_for(source.identity, session):
			raise OSError("interrupted")
		original_write(path, content)
	monkeypatch.setattr(canonical_store, "atomic_write", interrupt)
	with pytest.raises(OSError):
		store.save_download(source.identity, replace(download, response=replace(response, records=(replace(response.records[0], close=Decimal("12")),))))
	with pytest.raises(ValueError, match="Invalid canonical"):
		store.load(source.identity, session)


def test_archive_rejects_wrong_date_duplicate_fields_and_truncation(harness, canonical, calendar):
	session = calendar.session(date(2024, 7, 1))
	content = archive_content(DownloadedSession(session, harness.provider.responses[session.session_date], harness.clock.now()))
	with pytest.raises(ValueError):
		decode_archive(content, calendar.session(date(2024, 7, 2)))
	with pytest.raises(ValueError):
		decode_archive(content + content, session)
	with pytest.raises(ValueError):
		decode_archive(content.replace(b'"T":"AAA"', b'"T":"AAA","T":"AAA"'), session)
	with pytest.raises(ValueError):
		decode_archive(content[:-8], session)


def test_conflicts_compare_numbers_and_include_classification(tmp_path):
	left, right = tmp_path / "left.ndjson", tmp_path / "right.ndjson"
	left.write_text(json.dumps({"T": "ABC", "c": "1.5500", "v": "10"}) + "\n")
	right.write_text(json.dumps({"T": "ABC", "c": "24.8", "v": 10}) + "\n")
	before = {"path": str(left), "location": "market_summary"}
	after = {"path": str(right), "location": "daily-maintenance"}
	result = list(conflicts("2024-07-01", before, after, {"ABC": {"exchange": "XNAS", "asset_type": "CS", "is_test": False, "evidence": "fixture"}}))
	assert len(result) == 1
	assert result[0]["field"] == "c"
	assert result[0]["exchange"] == "XNAS"
	assert result[0]["baseline_value"] == "1.5500"


def test_explicit_refresh_does_not_leave_pending_import(harness, canonical, calendar):
	assert run(harness, canonical, calendar).successful
	assert run(harness, canonical, calendar, refresh_source=True).successful
	assert harness.database.sql("SELECT session_date FROM stockguidance_daily_source_files WHERE download_pending = 1") == []


def test_missing_derived_receipts_use_database_without_archives(harness, canonical, calendar, monkeypatch):
	assert run(harness, canonical, calendar).successful
	harness.database.sql("UPDATE stockguidance_imports SET derived_dependency_id = NULL, derived_content_id = NULL WHERE session_date = ?", ("2024-07-02",))
	harness.provider.calls.clear()
	monkeypatch.setattr(canonical[1], "load", lambda *args: pytest.fail("Derived repairs must not read source archives"))
	assert run(harness, canonical, calendar).successful
	assert harness.provider.calls == []


def test_force_verification_repairs_raw_damage_from_canonical_archive(harness, canonical, calendar):
	assert run(harness, canonical, calendar).successful
	harness.database.sql("DELETE FROM raw_stock_data WHERE date_id = (SELECT id FROM transaction_dates WHERE transaction_date = ?)", ("2024-07-02",))
	harness.provider.calls.clear()
	assert run(harness, canonical, calendar, force_verify=True).successful
	assert harness.provider.calls == []


def test_unregistered_archive_is_not_assumed_unadjusted(harness, canonical, calendar):
	source, store, _, _, _ = canonical
	session = calendar.session(date(2024, 7, 1))
	path = archive_path(source.data_root, session.session_date)
	path.parent.mkdir(parents=True)
	path.write_bytes(archive_content(DownloadedSession(session, harness.provider.responses[session.session_date], harness.clock.now())))
	assert store.load(source.identity, session) is None
