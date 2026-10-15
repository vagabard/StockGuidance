from dataclasses import replace
from datetime import date
import json

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.history import HistoryProvenance, UnknownHistoryBoundary, establish_history_boundary
from stockguidance.market_data.history_store import JsonHistoryStore


def test_boundary_roundtrip_is_independent_of_empty_storage(boundary, source, tmp_path):
	store = JsonHistoryStore(tmp_path / "metadata" / "history")
	assert store.load(source) is None
	assert not store.directory.exists()
	store.save(boundary)
	assert store.load(source) == boundary
	assert len(list(store.directory.iterdir())) == 1
	assert "key-reference" not in store.path_for(source).read_text()
	assert "database_config" not in store.path_for(source).read_text()


@pytest.mark.parametrize("changes", [{"endpoint": "other"}, {"universe": "other"}, {"adjustment": PriceAdjustment.UNADJUSTED}, {"provider": "other"}, {"market": "other"}])
def test_history_is_keyed_by_entire_nonsecret_source_scope(boundary, source, tmp_path, changes):
	store = JsonHistoryStore(tmp_path)
	store.save(boundary)
	other_source = replace(source, **changes)
	assert store.path_for(source) != store.path_for(other_source)
	assert store.load(other_source) is None


def test_unknown_boundary_does_not_infer_from_database_minimum(source_config, data_request, calendar, publication, repository, clock):
	with pytest.raises(UnknownHistoryBoundary, match="unknown"):
		plan_coverage(source_config, data_request, None, calendar, publication, repository, clock)
	assert repository.queries == []


def test_explicit_boundary_must_be_a_real_session(source, calendar, clock):
	with pytest.raises(ValueError, match="not a XNYS trading session"):
		establish_history_boundary(source, calendar, date(2024, 7, 4), clock, provenance=HistoryProvenance.CONFIGURATION, evidence="Supplied fixture")
	with pytest.raises(ValueError, match="nonempty"):
		establish_history_boundary(source, calendar, date(2024, 7, 1), clock, provenance=HistoryProvenance.SOURCE_HISTORY, evidence="")


@pytest.mark.parametrize("corruption", ["json", "version", "identity", "date", "naive"])
def test_corrupt_or_mismatched_metadata_is_not_complete_history(boundary, source, tmp_path, corruption):
	store = JsonHistoryStore(tmp_path)
	store.save(boundary)
	path = store.path_for(source)
	payload = json.loads(path.read_text())
	if corruption == "json":
		path.write_text("{")
	else:
		if corruption == "version":
			payload["schema_version"] = 2
		elif corruption == "identity":
			payload["source"]["universe"] = "different"
		elif corruption == "date":
			payload["history_start"] = "bad-date"
		else:
			payload["established_at"] = "2024-07-08T19:30:00"
		path.write_text(json.dumps(payload))
	with pytest.raises(ValueError):
		store.load(source)


def test_interrupted_metadata_save_preserves_prior_boundary(boundary, source, tmp_path, monkeypatch):
	store = JsonHistoryStore(tmp_path)
	store.save(boundary)
	original = store.path_for(source).read_bytes()

	def fail_replace(*args):
		raise OSError("Fixture interrupted replace")

	monkeypatch.setattr("stockguidance.market_data.history_store.os.replace", fail_replace)
	with pytest.raises(OSError):
		store.save(replace(boundary, evidence="Updated evidence"))
	assert store.path_for(source).read_bytes() == original
	assert len(list(tmp_path.iterdir())) == 1
