from dataclasses import asdict, replace
from datetime import timedelta
import json

import pytest

from stockguidance.artifacts.decoding import decode_features
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.training import build_training_dataset
from stockguidance.features.specification import CloseFeature, SignalMode
from stockguidance.market_data.closes import PriceState
from stockguidance.market_data.openings import OpeningData, OpeningObservation, OpeningQuery


def build(opening_signals, **overrides):
	samples = opening_signals.samples
	return build_sample(opening_signals.features, samples.target, samples.decision(), samples.boundary, samples.calendar, samples.publication, opening_signals.repository, **overrides)


def test_full_ordered_opening_channels_preserve_close_history_and_close_targets(opening_signals):
	samples = opening_signals.samples
	result = build(opening_signals, label_cutoff=samples.decision(9).decision_cutoff)
	assert result.training_eligible
	assert result.features.values == pytest.approx((0, 0.5, 0, -0.5, 0.1, 0.5, -0.2, -0.5, 0.21, 0.5, 0.2, -0.5))
	assert result.targets.raw == pytest.approx((0.05, -0.1))
	assert result.features.specification.version == "entry-open-v1"
	assert result.features.specification.shape == (3, 2, 2)
	assert decode_features(json.loads(json.dumps(asdict(opening_signals.features)))) == opening_signals.features
	assert "opening_channel_repeated" in result.features.specification.layout


def test_opening_inference_never_queries_entry_or_future_daily_bars(opening_signals):
	samples = opening_signals.samples
	samples.repository.calls.clear()
	result = build(opening_signals)
	assert result.features.values is not None
	assert all(session.sequence < samples.decision().entry_session.sequence for query in samples.repository.calls for session in query.scope.sessions)
	for query in samples.repository.calls:
		assert query.scope.as_of == samples.decision().decision_cutoff
	assert result.feature_cache_key == build(opening_signals, label_cutoff=samples.decision(9).decision_cutoff).feature_cache_key


@pytest.mark.parametrize("problem", ["missing", "late", "invalid", "wrong_source", "wrong_method"])
def test_missing_or_unverified_opening_for_any_ticker_invalidates_the_whole_layout(opening_signals, problem):
	samples, store = opening_signals.samples, opening_signals.store
	session = samples.decision().entry_session
	path = store.path_for(samples.boundary.source, session.session_date.isoformat(), 3)
	if problem == "missing":
		path.unlink()
	else:
		payload = json.loads(path.read_text(encoding="utf-8"))
		if problem == "late":
			observation = OpeningObservation(3, "T3", session, 30.0, samples.decision().decision_cutoff + timedelta(seconds=1), "fixture-late")
			store.save(samples.boundary.source, observation)
		elif problem == "invalid":
			payload["price"] = -1
		elif problem == "wrong_source":
			payload["source"]["adjustment"] = "unadjusted"
		else:
			payload["method"] = "daily-bar-open-backdated-to-morning"
		if problem != "late":
			path.write_text(json.dumps(payload), encoding="utf-8")
	result = build(opening_signals)
	assert result.features.values is None
	assert [issue.ticker_id for issue in result.features.issues] == [3]
	assert result.features.specification.ticker_ids == (17, 3)
	assert result.features.issues[0].state is (PriceState.MISSING if problem == "missing" else PriceState.NOT_YET_AVAILABLE if problem == "late" else PriceState.UNVERIFIED)


def test_opening_correction_invalidates_feature_and_sample_identity(opening_signals):
	first = build(opening_signals)
	samples = opening_signals.samples
	session = samples.decision().entry_session
	opening_signals.store.save(samples.boundary.source, OpeningObservation(17, "T17", session, 242.0, session.opens_at + timedelta(minutes=3), "corrected-fixture-opening"))
	second = build(opening_signals)
	assert first.features.values != second.features.values
	assert first.feature_cache_key != second.feature_cache_key and first.cache_key != second.cache_key
	assert samples.build().feature_cache_key != first.feature_cache_key


def test_training_does_not_backdate_late_captured_openings(opening_signals):
	samples = opening_signals.samples
	decision = samples.decision()
	opening_signals.store.save(samples.boundary.source, OpeningObservation(17, "T17", decision.entry_session, 181.5, samples.decision(9).decision_cutoff, "downloaded-days-later"))
	dataset = build_training_dataset((decision,), samples.decision(10).decision_cutoff, opening_signals.features, samples.target, samples.boundary, samples.calendar, samples.publication, opening_signals.repository)
	assert not dataset.eligible and len(dataset.excluded) == 1
	assert dataset.excluded[0].targets.ready
	assert dataset.excluded[0].features.issues[0].state is PriceState.NOT_YET_AVAILABLE


def test_unopened_session_is_deferred_without_touching_receipts(opening_signals, monkeypatch):
	samples = opening_signals.samples
	session = samples.decision().entry_session
	monkeypatch.setattr(type(opening_signals.store.root), "read_text", lambda *args, **kwargs: pytest.fail("Unopened session touched stored data"))
	query = OpeningQuery(samples.boundary, (session,), (17, 3), session.opens_at)
	data = opening_signals.store.read_openings(query)
	assert len(data.issues) == 2 and all(issue.state is PriceState.NOT_YET_AVAILABLE for issue in data.issues)


def test_opening_contracts_reject_duplicate_keys_future_receipts_and_history_crossings(opening_signals):
	samples = opening_signals.samples
	session = samples.decision().entry_session
	query = OpeningQuery(samples.boundary, (session,), (17, 3), samples.decision().decision_cutoff)
	data = opening_signals.store.read_openings(query)
	with pytest.raises(ValueError):
		OpeningData(query, data.observations * 2, ())
	with pytest.raises(ValueError):
		OpeningData(replace(query, as_of=session.opens_at), data.observations, ())
	with pytest.raises(ValueError):
		OpeningQuery(replace(samples.boundary, history_start=samples.decision(8).entry_session.session_date), (session,), (17, 3), query.as_of)
	with pytest.raises(ValueError):
		replace(opening_signals.features, signal_mode=SignalMode.PREVIOUS_CLOSE)
	with pytest.raises(ValueError):
		replace(opening_signals.features, feature_order=(CloseFeature.RELATIVE_CLOSE,))
