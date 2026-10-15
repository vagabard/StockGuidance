from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.market_data.providers.polygon import HttpResponse, PolygonGroupedProvider
from stockguidance.workflows.failures import StageExecutionError


class Transport:
	def __init__(self, payload, status=200):
		self.response = HttpResponse(status, payload)
		self.calls = []

	def get(self, path, adjusted):
		self.calls.append((path, adjusted))
		return self.response


@pytest.fixture
def payload():
	return {"status": "OK", "adjusted": True, "resultsCount": 1, "queryCount": 1, "results": [{"T": "AAA", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100.0, "vw": 10.2, "n": 5, "t": int(datetime(2024, 7, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)}]}


def test_grouped_response_validates_and_preserves_scope_counts(harness, calendar, payload):
	transport = Transport(payload)
	response = PolygonGroupedProvider(transport).fetch(harness.source, calendar.session(date(2024, 7, 1)))
	assert response.state is ResponseState.COMPLETE
	assert dict(response.counts) == {"resultsCount": 1, "queryCount": 1}
	assert transport.calls == [("/v2/aggs/grouped/locale/us/market/stocks/2024-07-01", True)]
	assert response.records[0].ticker == "AAA"
	assert response.source_timestamps[0][1] == payload["results"][0]["t"]


@pytest.mark.parametrize("change", ["error", "http", "wrong-adjustment", "missing-adjustment", "bad-array", "truncated", "duplicate", "bad-price", "nan-price", "negative-count", "fractional-count", "wrong-day", "utc-day-only", "bad-continuation", "bad-count"])
def test_invalid_provider_responses_are_sanitized_failures(harness, calendar, payload, change):
	status = 200
	if change == "error":
		payload = {"status": "ERROR", "error": "fixture secret API key"}
	elif change == "http":
		status = 403
	elif change == "wrong-adjustment":
		payload["adjusted"] = False
	elif change == "missing-adjustment":
		del payload["adjusted"]
	elif change == "bad-array":
		payload["results"] = {"error": "fixture secret"}
	elif change == "truncated":
		payload["resultsCount"] = 2
	elif change == "duplicate":
		payload["results"] *= 2
		payload["resultsCount"] = 2
	elif change == "bad-price":
		payload["results"][0]["c"] = 20
	elif change == "nan-price":
		payload["results"][0]["c"] = float("nan")
	elif change == "negative-count":
		payload["results"][0]["v"] = -1
	elif change == "fractional-count":
		payload["results"][0]["n"] = 1.1
	elif change == "wrong-day":
		payload["results"][0]["t"] += 86400000
	elif change == "utc-day-only":
		payload["results"][0]["t"] -= 4 * 60 * 60 * 1000
	elif change == "bad-continuation":
		payload["next_url"] = 123
	elif change == "bad-count":
		payload["resultsCount"] = True
	with pytest.raises(StageExecutionError) as error:
		PolygonGroupedProvider(Transport(payload, status)).fetch(harness.source, calendar.session(date(2024, 7, 1)))
	assert "fixture secret" not in str(error.value)
	assert error.value.__cause__ is None


@pytest.mark.parametrize("change", ["empty", "missing-count", "continuation", "delayed"])
def test_unknown_completeness_never_becomes_complete(harness, calendar, payload, change):
	if change == "empty":
		payload["results"] = []
		payload["resultsCount"] = 0
	elif change == "missing-count":
		del payload["resultsCount"]
	elif change == "continuation":
		payload["next_url"] = "https://api.polygon.io/page?apiKey=fixture-secret"
	else:
		payload["status"] = "DELAYED"
	response = PolygonGroupedProvider(Transport(payload)).fetch(harness.source, calendar.session(date(2024, 7, 1)))
	assert response.state is ResponseState.UNVERIFIED
	assert "fixture-secret" not in repr(response)


def test_delayed_empty_is_explicitly_not_published(harness, calendar):
	response = PolygonGroupedProvider(Transport({"status": "DELAYED", "adjusted": True, "resultsCount": 0})).fetch(harness.source, calendar.session(date(2024, 7, 1)))
	assert response.state is ResponseState.NOT_YET_PUBLISHED


def test_unadjusted_setting_is_forwarded(harness, calendar, payload):
	payload["adjusted"] = False
	source = replace(harness.source, identity=replace(harness.source.identity, adjustment=PriceAdjustment.UNADJUSTED))
	transport = Transport(payload)
	assert PolygonGroupedProvider(transport).fetch(source, calendar.session(date(2024, 7, 1))).state is ResponseState.COMPLETE
	assert transport.calls[0][1] is False


def test_universe_mismatch_is_rejected_before_fetch(harness, calendar, payload):
	source = replace(harness.source, identity=replace(harness.source.identity, universe="only-AAA"))
	transport = Transport(payload)
	with pytest.raises(ValueError):
		PolygonGroupedProvider(transport).fetch(source, calendar.session(date(2024, 7, 1)))
	assert transport.calls == []


def test_error_or_unexplained_empty_cannot_construct_complete_response():
	with pytest.raises(ValueError):
		ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 0),))
	with pytest.raises(ValueError):
		ProviderResponse(ResponseState.NO_TRADING, (), 200, "OK")


def test_zero_transactions_do_not_turn_valid_records_into_a_closure(harness, calendar, payload):
	payload["results"][0]["n"] = 0
	response = PolygonGroupedProvider(Transport(payload)).fetch(harness.source, calendar.session(date(2024, 7, 1)))
	assert response.state is ResponseState.COMPLETE
	assert response.records[0].transactions == 0
