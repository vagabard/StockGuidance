from dataclasses import replace
from datetime import timedelta

import pytest

from stockguidance.features.specification import CloseFeature, FeatureSpec, SignalMode
from stockguidance.market_data.availability import AvailabilityPolicy


def test_research_mode_releases_late_backfilled_daily_features_without_backdating_receipts(samples):
	late_key = samples.calendar.session(samples.decision().previous_session.session_date), 17
	late = samples.repository.observations[late_key]
	late_cutoff = samples.decision(10).decision_cutoff
	samples.repository.observations[late_key] = replace(late, timing=replace(late.timing, available_at=late_cutoff))

	strict = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	research = samples.build(label_cutoff=samples.decision(9).decision_cutoff, availability_policy=AvailabilityPolicy.RETROSPECTIVE_DAILY_PREVIOUS_CLOSE, data_snapshot=late_cutoff)

	assert strict.features.values is None
	assert research.features.values is not None
	assert research.training_eligible
	assert samples.repository.observations[late_key].timing.available_at == late_cutoff
	assert research.availability_policy is AvailabilityPolicy.RETROSPECTIVE_DAILY_PREVIOUS_CLOSE
	assert research.data_snapshot == late_cutoff
	assert research.feature_cache_key != strict.feature_cache_key


def test_research_mode_rejects_entry_open_before_any_data_read(samples):
	specification = FeatureSpec((17, 3), 3, signal_mode=SignalMode.ENTRY_OPEN, feature_order=(CloseFeature.RELATIVE_CLOSE, CloseFeature.ENTRY_OPEN_RETURN))
	with pytest.raises(ValueError, match="previous_close only|entry_open"):
		samples.build(specification=specification, availability_policy=AvailabilityPolicy.RETROSPECTIVE_DAILY_PREVIOUS_CLOSE, data_snapshot=samples.decision(10).decision_cutoff)
	assert samples.repository.calls == []


def test_strict_policy_remains_the_default():
	assert AvailabilityPolicy.STRICT.value == "strict"
