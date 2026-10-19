from datetime import datetime

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig, SourceIdentity, canonical_source_family
from stockguidance.config.validation import require_positive_int
from stockguidance.market_data.providers.contracts import ProviderResponse, ProviderCapability, unknown_capability
from stockguidance.market_data.providers.credentials import polygon_credential
from stockguidance.market_data.providers.http import PolygonHttpTransport
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE, PolygonGroupedProvider


class ConfiguredPolygonProvider:
	def __init__(self, max_retries: int = 5) -> None:
		require_positive_int(max_retries, "provider retry limit")
		self.max_retries = max_retries
		self.provider: PolygonGroupedProvider | None = None

	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse:
		if self.provider is None:
			self.provider = PolygonGroupedProvider(PolygonHttpTransport(polygon_credential(), self.max_retries))
		return self.provider.fetch(source, session)

	def discover_capability(self, source: SourceIdentity, checked_at: datetime) -> ProviderCapability:
		if (canonical_source_family(source.provider), source.endpoint, source.market, source.universe) != ("polygon_massive", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE):
			raise ValueError("Polygon grouped adapter requires its exact US-stocks-excluding-OTC request scope")
		return unknown_capability(source, checked_at, "Configured Polygon adapter has no authoritative account capability endpoint")
