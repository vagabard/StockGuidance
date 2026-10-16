from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_positive_int
from stockguidance.market_data.providers.contracts import ProviderResponse
from stockguidance.market_data.providers.credentials import polygon_credential
from stockguidance.market_data.providers.http import PolygonHttpTransport
from stockguidance.market_data.providers.polygon import PolygonGroupedProvider


class ConfiguredPolygonProvider:
	def __init__(self, max_retries: int = 5) -> None:
		require_positive_int(max_retries, "provider retry limit")
		self.max_retries = max_retries
		self.provider: PolygonGroupedProvider | None = None

	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse:
		if self.provider is None:
			self.provider = PolygonGroupedProvider(PolygonHttpTransport(polygon_credential(), self.max_retries))
		return self.provider.fetch(source, session)
