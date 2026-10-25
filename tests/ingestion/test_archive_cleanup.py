from argparse import Namespace
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
import json

import pytest

from stockguidance.cli.consolidate_market_data import configured_source
from stockguidance.market_data.archive import archive_content, archive_path
from stockguidance.market_data.canonical_store import CanonicalArchiveStore
from stockguidance.market_data.cleanup import ArchiveCleanup, checked_path, inventory
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from tests.ingestion.test_consolidation_cli import configuration


class MemoryCatalog:
	def __init__(self, source):
		self.source = source
		self.entries = {}

	def get(self, day):
		return self.entries.get(day)

	def put(self, day, metadata):
		self.entries[day] = metadata


@pytest.fixture
def setup(tmp_path, harness, calendar):
	source = configured_source(Namespace(dbinfo_file=configuration(tmp_path), data_root=tmp_path / "data"))
	store = CanonicalArchiveStore(source.data_root, MemoryCatalog(source))
	session = calendar.session(date(2024, 7, 1))
	response = replace(harness.response(), source_timestamps=(("AAA", int(session.opens_at.timestamp() * 1000)),))
	download = DownloadedSession(session, response, harness.clock.now())
	return source, store, download


def run_cleanup(tmp_path, source, store, *, apply=True, consolidate=True, conflicts=False):
	return ArchiveCleanup(source, store, tmp_path / "output", apply=apply).run(consolidate=consolidate, cleanup_conflicts=conflicts)


def legacy(source, download, *, adjusted=False):
	identity = source.identity
	if adjusted:
		from stockguidance.config.source import PriceAdjustment
		identity = replace(source.identity, adjustment=PriceAdjustment.ADJUSTED)
	return ManifestStore(source.data_root).save_download(identity, download)


def test_preview_then_apply_promotes_and_deletes_verified_sources(tmp_path, setup):
	source, store, download = setup
	legacy(source, download)
	before = {path: path.read_bytes() for path in source.data_root.rglob("*") if path.is_file()}
	report = run_cleanup(tmp_path, source, store, apply=False)
	assert report["counts"]["consolidated_dates"] == 1
	assert report["counts"]["deleted_files"] == 2
	assert not store.catalog.entries
	assert before == {path: path.read_bytes() for path in source.data_root.rglob("*") if path.is_file()}
	report = run_cleanup(tmp_path, source, store)
	assert not report["unresolved"]
	assert archive_path(source.data_root, download.session.session_date).read_bytes() == archive_content(download)
	assert not (source.data_root / "market_data").exists()
	assert not run_cleanup(tmp_path, source, store)["counts"]


def test_numeric_equivalence_deletes_daily_copy_and_preserves_raw_test_symbol(tmp_path, setup):
	source, store, download = setup
	response = replace(download.response, records=(replace(download.response.records[0], ticker="ZVZZT"),), source_timestamps=(("ZVZZT", download.response.source_timestamps[0][1]),))
	download = replace(download, response=response)
	store.save_download(source.identity, download)
	path = source.data_root / "daily-maintenance" / "old" / "2024-07-01.ndjson"
	path.parent.mkdir(parents=True)
	row = json.loads(archive_content(download))
	row["o"] = 10.0
	path.write_text(json.dumps(row))
	report = run_cleanup(tmp_path, source, store)
	assert report["counts"]["deleted_files"] == 1
	assert not path.exists()
	assert json.loads(archive_path(source.data_root, download.session.session_date).read_bytes())["T"] == "ZVZZT"


def test_adjusted_sources_and_orphans_are_quarantined_not_promoted(tmp_path, setup):
	source, store, download = setup
	legacy(source, download, adjusted=True)
	orphan = source.data_root / "market_data" / "old" / "responses" / "orphan.json"
	orphan.parent.mkdir(parents=True)
	orphan.write_text("{}")
	report = run_cleanup(tmp_path, source, store)
	assert report["counts"]["quarantined_files"] == 3
	assert len(report["unresolved"]) == 2
	assert not archive_path(source.data_root, download.session.session_date).exists()
	assert len(inventory(source.data_root, quarantined=True)) == 2
	assert run_cleanup(tmp_path, source, store, consolidate=False, conflicts=True)["unresolved"]


def test_explicit_supersession_cleans_quarantined_differing_sources(tmp_path, setup):
	source, store, download = setup
	legacy(source, download, adjusted=True)
	run_cleanup(tmp_path, source, store)
	copy = inventory(source.data_root, quarantined=True)[0]
	identity = sha256(copy.path.read_bytes()).hexdigest()
	new_response = replace(download.response, records=(replace(download.response.records[0], close=download.response.records[0].close + 1),))
	store.save_download(source.identity, replace(download, response=new_response))
	metadata = store.catalog.get(download.session.session_date)
	store.catalog.put(download.session.session_date, {**metadata, "supersedes": [{"sha256": identity, "evidence": "Verified replacement explicitly approved"}]})
	report = run_cleanup(tmp_path, source, store, consolidate=False, conflicts=True)
	assert report["counts"]["deleted_files"] == 2
	assert not report["unresolved"]
	assert not (source.data_root / "market_summary_conflicts").exists()


def test_later_verified_canonical_resolves_older_unadjusted_conflict(tmp_path, setup):
	source, store, download = setup
	legacy(source, download)
	new_response = replace(download.response, records=(replace(download.response.records[0], close=download.response.records[0].close + 1),))
	store.save_download(source.identity, replace(download, response=new_response, available_at=download.available_at + timedelta(days=1)))
	report = run_cleanup(tmp_path, source, store)
	assert report["counts"]["deleted_files"] == 2
	assert not report["unresolved"]


def test_unverified_existing_canonical_is_not_overwritten(tmp_path, setup):
	source, store, download = setup
	legacy(source, download)
	path = archive_path(source.data_root, download.session.session_date)
	path.parent.mkdir(parents=True)
	row = json.loads(archive_content(download))
	row["c"] = "12"
	path.write_text(json.dumps(row))
	before = path.read_bytes()
	report = run_cleanup(tmp_path, source, store)
	assert report["unresolved"]
	assert path.read_bytes() == before
	assert not store.catalog.entries


def test_incomplete_response_is_quarantined(tmp_path, setup):
	source, store, download = setup
	legacy(source, replace(download, response=replace(download.response, state=ResponseState.UNVERIFIED, has_more=True)))
	report = run_cleanup(tmp_path, source, store)
	assert report["unresolved"]
	assert not store.catalog.entries


def test_unknown_content_and_unrelated_directories_are_preserved(tmp_path, setup):
	source, store, download = setup
	for relative in ("history/keep.json", "java/keep.json", "market_data/openings/keep.json", "archive-backup/keep.json"):
		path = source.data_root / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("keep")
	bad = source.data_root / "daily-maintenance" / "old" / "not-a-date.ndjson"
	bad.parent.mkdir(parents=True)
	bad.write_text("broken")
	run_cleanup(tmp_path, source, store)
	assert (source.data_root / "market_summary_conflicts/daily-maintenance/old/not-a-date.ndjson").read_text() == "broken"
	assert len(list(source.data_root.rglob("keep.json"))) == 4


def test_source_change_aborts_before_deletion(tmp_path, setup, monkeypatch):
	source, store, download = setup
	store.save_download(source.identity, download)
	legacy(source, download)
	operation = ArchiveCleanup(source, store, tmp_path / "output", apply=True)
	original = operation.decide
	def changed(copy):
		result = original(copy)
		copy.path.write_text("changed")
		return result
	monkeypatch.setattr(operation, "decide", changed)
	with pytest.raises(ValueError, match="Source changed"):
		operation.run(consolidate=True, cleanup_conflicts=False)
	assert any(source.data_root.glob("market_data/*/responses/*.json"))


def test_output_under_data_and_traversal_are_rejected(tmp_path, setup):
	source, store, download = setup
	with pytest.raises(ValueError, match="outside data"):
		ArchiveCleanup(source, store, source.data_root / "output", apply=True)
	with pytest.raises(ValueError, match="escapes"):
		checked_path(source.data_root / ".." / "outside", source.data_root)


def test_shared_response_references_are_retained(tmp_path, setup):
	source, store, download = setup
	legacy(source, download)
	manifest = next(source.data_root.glob("market_data/*/*.manifest.json"))
	(manifest.parent / "duplicate.manifest.json").write_bytes(manifest.read_bytes())
	report = run_cleanup(tmp_path, source, store)
	assert len(report["unresolved"]) == 2
	assert not store.catalog.entries
	assert len(list((source.data_root / "market_summary_conflicts").rglob("*.json"))) == 3


def test_symlink_source_is_rejected(tmp_path, setup):
	source, store, download = setup
	outside = tmp_path / "outside.ndjson"
	outside.write_text("keep")
	link = source.data_root / "daily-maintenance/old/2024-07-01.ndjson"
	link.parent.mkdir(parents=True)
	try:
		link.symlink_to(outside)
	except OSError:
		pytest.skip("Symlink creation unavailable")
	with pytest.raises(ValueError, match="links"):
		run_cleanup(tmp_path, source, store)
	assert outside.read_text() == "keep"


def test_interrupted_delete_resumes_using_journal(tmp_path, setup, monkeypatch):
	source, store, download = setup
	legacy(source, download)
	operation = ArchiveCleanup(source, store, tmp_path / "output", apply=True)
	original = operation.transfer
	def interrupted(path, **arguments):
		if path.name.endswith(".manifest.json"):
			raise OSError("Simulated interruption")
		original(path, **arguments)
	monkeypatch.setattr(operation, "transfer", interrupted)
	with pytest.raises(OSError, match="interruption"):
		operation.run(consolidate=True, cleanup_conflicts=False)
	assert not list(source.data_root.glob("market_data/*/responses/*.json"))
	with (tmp_path / "output/cleanup-journal.jsonl").open("ab") as stream:
		stream.write(b'{"interrupted_record":')
	report = run_cleanup(tmp_path, source, store)
	assert not report["unresolved"]
	assert report["counts"]["deleted_files"] == 1
	assert not (source.data_root / "market_data").exists()


def test_lock_failure_prevents_cli_mutation(tmp_path, setup, monkeypatch, capsys):
	from contextlib import contextmanager
	from stockguidance.cli import consolidate_market_data as cli
	source, store, download = setup
	legacy(source, download)
	@contextmanager
	def unavailable(*arguments):
		raise ValueError("Update lock is busy")
		yield
	monkeypatch.setattr(cli, "market_update_lock", unavailable)
	assert cli.main(["--data-root", str(source.data_root), "--dbinfo-file", str(source.database_config), "--consolidate", "--apply"]) == 1
	assert "lock is busy" in capsys.readouterr().out
	assert len(inventory(source.data_root)) == 1


def test_cli_reports_next_commands_and_unresolved_exit_status(tmp_path, setup, monkeypatch, capsys):
	from stockguidance.cli import consolidate_market_data as cli
	source, store, download = setup
	legacy(source, download, adjusted=True)
	monkeypatch.setattr(cli, "CanonicalArchiveStore", lambda *arguments: store)
	report_dir = tmp_path / "output"
	assert cli.main(["--data-root", str(source.data_root), "--dbinfo-file", str(source.database_config), "--output", str(report_dir), "--consolidate"]) == 2
	report = json.loads(capsys.readouterr().out)
	assert "--consolidate --apply" in report["next_commands"][0]
	assert "--cleanup-conflicts" in report["next_commands"][1]
	assert not (source.data_root / "market_summary_conflicts").exists()


def test_later_legacy_response_is_published_and_old_canonical_backed_up(tmp_path, setup):
	source, store, download = setup
	store.save_download(source.identity, download)
	before = archive_path(source.data_root, download.session.session_date).read_bytes()
	new_response = replace(download.response, records=(replace(download.response.records[0], close=download.response.records[0].close + 1),))
	legacy(source, replace(download, response=new_response, available_at=download.available_at + timedelta(days=1)))
	report = run_cleanup(tmp_path, source, store)
	assert report["counts"]["consolidated_dates"] == 1
	assert not report["unresolved"]
	assert next((source.data_root / "archive-backup").rglob("*.ndjson")).read_bytes() == before


def test_missing_or_corrupt_response_is_quarantined(tmp_path, setup):
	source, store, download = setup
	legacy(source, download)
	response = next(source.data_root.glob("market_data/*/responses/*.json"))
	response.write_text("not-json")
	report = run_cleanup(tmp_path, source, store)
	assert report["counts"]["quarantined_files"] == 2
	assert not store.catalog.entries


def test_verified_decisions_preserve_superseded_hashes_in_catalog(tmp_path, setup, monkeypatch, capsys):
	from contextlib import nullcontext
	from stockguidance.cli import consolidate_market_data as cli
	source, store, download = setup
	path = tmp_path / "verified.ndjson"
	path.write_bytes(archive_content(download))
	supersedes = [{"sha256": "a" * 64, "evidence": "Operator verified this adjusted response is superseded"}]
	decisions = tmp_path / "decisions.json"
	decisions.write_text(json.dumps([{"date": "2024-07-01", "path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
		"adjustment": "unadjusted", "complete": True, "available_at": download.available_at.isoformat(), "evidence": "Verified unadjusted response", "supersedes": supersedes}]))
	monkeypatch.setattr(cli, "CanonicalArchiveStore", lambda *arguments: store)
	monkeypatch.setattr(cli, "ArchiveCatalog", lambda *arguments: store.catalog)
	monkeypatch.setattr(cli, "market_update_lock", lambda *arguments: nullcontext())
	arguments = ["--data-root", str(source.data_root), "--dbinfo-file", str(source.database_config), "--decisions", str(decisions), "--apply"]
	assert cli.main(arguments) == 0
	assert store.catalog.get(download.session.session_date)["supersedes"] == supersedes
	assert cli.main(arguments) == 0
	assert store.catalog.get(download.session.session_date)["supersedes"] == supersedes


def test_combined_cleanup_rechecks_newly_quarantined_sources(tmp_path, setup):
	source, store, download = setup
	legacy(source, download, adjusted=True)
	next(source.data_root.glob("market_data/*")).rename(source.data_root / "market_data/aaa")
	legacy(source, download)
	next(path for path in source.data_root.glob("market_data/*") if path.name != "aaa").rename(source.data_root / "market_data/zzz")
	preview = run_cleanup(tmp_path, source, store, apply=False, conflicts=True)
	assert not preview["unresolved"]
	assert preview["counts"]["deleted_files"] == 4
	report = run_cleanup(tmp_path, source, store, conflicts=True)
	assert not report["unresolved"]
	assert report["counts"] == preview["counts"]
	assert not (source.data_root / "market_summary_conflicts").exists()
