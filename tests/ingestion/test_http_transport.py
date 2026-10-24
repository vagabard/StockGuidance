import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from stockguidance.market_data.providers.http import PolygonHttpTransport
from stockguidance.workflows.failures import StageExecutionError


class Response:
	status = 200

	def read(self):
		return json.dumps({"status": "OK"}).encode()

	def __enter__(self):
		return self

	def __exit__(self, *arguments):
		return None


def test_transport_carries_adjustment_scope_and_header_without_a_credential_url():
	def request(fake_request, timeout):
		assert fake_request.full_url == "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/2024-07-01?adjusted=false&include_otc=false"
		assert "fixture-token" not in fake_request.full_url
		assert fake_request.get_header("Authorization") == "Bearer fixture-token"
		assert timeout == 60
		return Response()

	transport = PolygonHttpTransport("fixture-token", minimum_interval_seconds=0)
	assert "fixture-token" not in repr(transport)
	with patch("urllib.request.urlopen", side_effect=request), patch("time.sleep"):
		assert transport.get("/v2/aggs/grouped/locale/us/market/stocks/2024-07-01", False).status == 200


@pytest.mark.parametrize(("status", "expected_calls"), [(429, 3), (403, 1)])
def test_transport_retries_only_transient_http_failures_and_sanitizes(status, expected_calls, capsys):
	def request(*arguments, **keywords):
		raise HTTPError("https://api.polygon.io/?apiKey=fixture-token", status, "fixture-token", {}, None)

	transport = PolygonHttpTransport("fixture-token", max_retries=2, minimum_interval_seconds=0)
	with patch("urllib.request.urlopen", side_effect=request) as http, patch("time.sleep"):
		with pytest.raises(StageExecutionError) as error:
			transport.get("/v2/aggs/grouped/locale/us/market/stocks/2024-07-01", True)
	assert http.call_count == expected_calls
	assert "fixture-token" not in str(error.value)
	assert capsys.readouterr().out == ""
