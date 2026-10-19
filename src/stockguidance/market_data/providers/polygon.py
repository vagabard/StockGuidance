from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity, canonical_source_family
from stockguidance.market_data.identity import mapping, text_field
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState, ProviderCapability, unknown_capability
from stockguidance.market_data.records import count_value, provider_bar
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage


GROUPED_ENDPOINT = "/v2/aggs/grouped/locale/us/market/stocks/{date}"
GROUPED_UNIVERSE = "us-stocks-excluding-otc"


@dataclass(frozen=True)
class HttpResponse:
	status: int
	payload: object


class HttpTransport(Protocol):
	def get(self, path: str, adjusted: bool) -> HttpResponse: ...


def provider_failure() -> StageExecutionError:
	return StageExecutionError(StageFailure(WorkflowStage.REFRESH, FailureCode.PROVIDER, "Grouped daily provider request or response validation failed", True))


class PolygonGroupedProvider:
	def __init__(self, transport: HttpTransport) -> None:
		self.transport = transport

	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse:
		identity = source.identity
		if (canonical_source_family(identity.provider), identity.endpoint, identity.market, identity.universe) != ("polygon_massive", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE):
			raise ValueError("Polygon grouped adapter requires its exact US-stocks-excluding-OTC request scope")
		try:
			response = self.transport.get(GROUPED_ENDPOINT.format(date=session.session_date.isoformat()), identity.adjustment is PriceAdjustment.ADJUSTED)
			if response.status != 200:
				raise ValueError("HTTP error")
			payload = mapping(response.payload)
			status = payload.get("status")
			if status not in {"OK", "DELAYED"}:
				raise ValueError("Provider error")
			if type(payload.get("adjusted")) is not bool or payload["adjusted"] != (identity.adjustment is PriceAdjustment.ADJUSTED):
				raise ValueError("Adjustment metadata differs from request")
			results = payload.get("results", [])
			if not isinstance(results, list):
				raise ValueError("Expected a results array")
			records = tuple(sorted((provider_bar(row, session) for row in results), key=lambda bar: bar.ticker))
			counts = tuple((name, count_value(payload[name])) for name in ("resultsCount", "queryCount", "count") if name in payload)
			continuation = payload.get("next_url")
			if continuation is not None and (not isinstance(continuation, str) or not continuation):
				raise ValueError("Invalid continuation metadata")
			if "resultsCount" in dict(counts) and dict(counts)["resultsCount"] != len(records):
				raise ValueError("Truncated response")
			state = ResponseState.COMPLETE if records and "resultsCount" in dict(counts) and not continuation and status == "OK" else ResponseState.UNVERIFIED
			if not records and status == "DELAYED":
				state = ResponseState.NOT_YET_PUBLISHED
			timestamps = tuple((text_field(row, "T"), count_value(row["t"])) for row in map(mapping, results))
			return ProviderResponse(state, records, response.status, str(status), counts, bool(continuation), source_timestamps=timestamps)
		except Exception:
			raise provider_failure() from None

	def discover_capability(self, source: SourceIdentity, checked_at: datetime) -> ProviderCapability:
		if (canonical_source_family(source.provider), source.endpoint, source.market, source.universe) != ("polygon_massive", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE):
			raise ValueError("Polygon grouped adapter requires its exact US-stocks-excluding-OTC request scope")
		return unknown_capability(source, checked_at, "Polygon grouped history has no authoritative account capability endpoint")
