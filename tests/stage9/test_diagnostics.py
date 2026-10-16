from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from stockguidance.datasets.training import build_training_dataset
from stockguidance.diagnostics.correlations import target_correlations
from stockguidance.diagnostics.features import inspect_features, select_samples
from stockguidance.reporting.email import EmailDraft, recommendation_draft, send_draft


def test_feature_inspection_is_deterministic_keeps_exclusions_and_original_cutoffs(training):
	dataset, evaluation, configuration = training
	first = inspect_features(dataset, 10, 31)
	assert first == inspect_features(dataset, 10, 31)
	assert first["excluded_samples"] == len(dataset.excluded)
	assert first["eligible_samples"] == len(dataset.eligible)
	assert len(first["samples"]) == len(dataset.samples)
	for sample, payload in zip(dataset.samples, first["samples"]):
		assert payload["feature_identity"] == sample.feature_cache_key
		assert payload["decision_cutoff"] == sample.timing.decision_cutoff.isoformat()
		assert all(value < payload["entry_session"] for value in payload["history_sessions"])
	assert tuple(sample.timing.entry_session.sequence for sample in select_samples(evaluation, 1, 7)) == tuple(sample.timing.entry_session.sequence for sample in select_samples(evaluation, 1, 7))


def test_correlations_use_only_mature_complete_raw_close_returns(samples):
	decisions = tuple(samples.decision(day) for day in (2, 3, 5, 8, 9, 10, 11))
	cutoff = samples.calendar.session(date(2024, 7, 11)).closes_at + timedelta(hours=1)
	dataset = build_training_dataset(decisions, cutoff, samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	payload = target_correlations(dataset)
	assert payload["target_formula"] == "close(T+1) / close(T) - 1"
	assert payload["mature_samples"] == len(dataset.eligible)
	assert payload["excluded_samples"] == len(dataset.excluded)
	for row in payload["correlations"]:
		if row["left_ticker_id"] == row["right_ticker_id"]:
			assert row["correlation"] == pytest.approx(1)


def test_diagnostic_sampling_validates_count_and_seed(training):
	dataset = training[0]
	for count, seed in ((0, 1), (1, -1), (True, 1), (1, True)):
		with pytest.raises(ValueError):
			select_samples(dataset, count, seed)


def test_email_requires_an_explicit_transport_and_never_records_fills(recommender):
	run = recommender.run()
	draft = recommendation_draft(run, "sender@example.invalid", ("recipient@example.invalid",))
	messages = []

	class FakeTransport:
		def send_message(self, message):
			messages.append(message)

	assert not messages and "no fills are recorded" in draft.body
	send_draft(draft, FakeTransport())
	assert len(messages) == 1
	assert messages[0]["To"] == "recipient@example.invalid"
	assert "Close-to-close" in messages[0]["Subject"]
	with pytest.raises(ValueError):
		EmailDraft("sender\nBcc: injection", draft.recipients, "subject", "body")
