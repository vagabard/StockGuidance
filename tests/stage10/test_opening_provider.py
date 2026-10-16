from datetime import timedelta
from types import SimpleNamespace
import json
from unittest.mock import patch

import pytest

from stockguidance.market_data.providers.openings import PolygonOpeningProvider
from stockguidance.market_data.providers.polygon import HttpResponse
from stockguidance.market_data.providers.http import PolygonHttpTransport


def source_and_response(harness, calendar):
	session = calendar.session(harness.boundary.history_start)
	start = int(session.opens_at.timestamp() * 1000)
	response = {"status": "OK", "ticker": "AAA", "adjusted": True, "resultsCount": 1, "results": [{"t": start, "o": 100, "c": 999999, "h": 999999, "l": 1, "v": 999999}]}
	return session, start, response


def test_provider_requests_only_the_first_regular_session_minute_and_records_receipt(harness, calendar):
	session, start, payload = source_and_response(harness, calendar)
	calls = []
	clock = SimpleNamespace(now=lambda: session.opens_at + timedelta(minutes=3))
	provider = PolygonOpeningProvider(SimpleNamespace(get=lambda path, adjusted: calls.append((path, adjusted)) or HttpResponse(200, payload)))
	result = provider.fetch(harness.source, session, 17, "AAA", clock)
	assert result.price == 100 and result.available_at == clock.now()
	assert calls == [(f"/v2/aggs/ticker/AAA/range/1/minute/{start}/{start + 59999}", True)]
	assert result.session.opens_at < result.available_at < result.session.closes_at


@pytest.mark.parametrize("problem", ["premarket", "wrong_ticker", "delayed", "truncated", "pagination", "adjustment", "invalid", "http", "error"])
def test_unverified_opening_payloads_fail_without_exposing_provider_details(harness, calendar, problem):
	session, start, payload = source_and_response(harness, calendar)
	status = 200
	if problem == "premarket":
		payload["results"][0]["t"] = start - 60000
	elif problem == "wrong_ticker":
		payload["ticker"] = "BBB"
	elif problem == "delayed":
		payload["status"] = "DELAYED"
	elif problem == "truncated":
		payload["resultsCount"] = 2
	elif problem == "pagination":
		payload["next_url"] = "fixture-secret-must-not-leak"
	elif problem == "adjustment":
		payload["adjusted"] = False
	elif problem == "invalid":
		payload["results"][0]["o"] = float("nan")
	elif problem == "error":
		payload = {"status": "ERROR", "error": "fixture-secret-must-not-leak"}
	else:
		status = 403
	provider = PolygonOpeningProvider(SimpleNamespace(get=lambda *args: HttpResponse(status, payload)))
	with pytest.raises(ValueError) as error:
		provider.fetch(harness.source, session, 17, "AAA", SimpleNamespace(now=lambda: session.opens_at + timedelta(minutes=2)))
	assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("offset", [0, 59, 24000])
def test_opening_fetch_never_waits_for_a_daily_bar_or_fetches_before_first_minute(harness, calendar, offset):
	session, start, payload = source_and_response(harness, calendar)
	provider = PolygonOpeningProvider(SimpleNamespace(get=lambda *args: pytest.fail("Unavailable opening triggered network")))
	with pytest.raises(ValueError):
		provider.fetch(harness.source, session, 17, "AAA", SimpleNamespace(now=lambda: session.opens_at + timedelta(seconds=offset)))


def test_empty_first_minute_is_unavailable_not_synthesized_or_replaced_by_later_trade(harness, calendar):
	session, start, payload = source_and_response(harness, calendar)
	payload.update(resultsCount=0, results=[])
	provider = PolygonOpeningProvider(SimpleNamespace(get=lambda *args: HttpResponse(200, payload)))
	assert provider.fetch(harness.source, session, 17, "AAA", SimpleNamespace(now=lambda: session.opens_at + timedelta(minutes=2))) is None


def test_shared_http_transport_sends_only_bounded_minute_query_and_header_credential(harness, calendar):
	session, start, payload = source_and_response(harness, calendar)
	requests = []
	response = SimpleNamespace(status=200, read=lambda: json.dumps(payload).encode("utf-8"))

	class Response:
		def __enter__(self):
			return response

		def __exit__(self, *values):
			return None

	with patch("urllib.request.urlopen", side_effect=lambda request, timeout: requests.append(request) or Response()):
		result = PolygonHttpTransport("fixture-credential-never-log", 0, 0).get(f"/v2/aggs/ticker/AAA/range/1/minute/{start}/{start + 59999}", True)
	assert result.status == 200
	assert requests[0].full_url.endswith("?adjusted=true&sort=asc&limit=1")
	assert "fixture-credential" not in requests[0].full_url
	assert requests[0].get_header("Authorization") == "Bearer fixture-credential-never-log"
	assert "fixture-credential" not in repr(PolygonHttpTransport("fixture-credential-never-log"))
