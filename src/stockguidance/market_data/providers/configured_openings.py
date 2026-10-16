from stockguidance.calendar.clock import Clock
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_positive_int
from stockguidance.market_data.openings import OpeningObservation
from stockguidance.market_data.providers.credentials import polygon_credential
from stockguidance.market_data.providers.http import PolygonHttpTransport
from stockguidance.market_data.providers.openings import PolygonOpeningProvider


class ConfiguredOpeningProvider:
	def __init__(self, max_retries: int = 5) -> None:
		require_positive_int(max_retries, "opening provider retry limit")
		self.max_retries = max_retries
		self.provider: PolygonOpeningProvider | None = None

	def fetch(self, source: SourceConfig, session: TradingSession, ticker_id: int, symbol: str, clock: Clock) -> OpeningObservation | None:
		if self.provider is None:
			self.provider = PolygonOpeningProvider(PolygonHttpTransport(polygon_credential(), self.max_retries))
		return self.provider.fetch(source, session, ticker_id, symbol, clock)
