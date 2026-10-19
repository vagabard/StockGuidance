from datetime import timedelta
import re
from typing import Protocol

from stockguidance.calendar.clock import Clock
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, canonical_source_family
from stockguidance.market_data.identity import content_id, mapping
from stockguidance.market_data.openings import OPENING_ENDPOINT, OpeningObservation
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE, HttpTransport


class OpeningProvider(Protocol):
	def fetch(self, source: SourceConfig, session: TradingSession, ticker_id: int, symbol: str, clock: Clock) -> OpeningObservation | None: ...


class PolygonOpeningProvider:
	def __init__(self, transport: HttpTransport) -> None:
		self.transport = transport

	def fetch(self, source: SourceConfig, session: TradingSession, ticker_id: int, symbol: str, clock: Clock) -> OpeningObservation | None:
		identity = source.identity
		if (canonical_source_family(identity.provider), identity.endpoint, identity.market, identity.universe) != ("polygon_massive", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE) or re.fullmatch(r"[A-Za-z0-9._-]+", symbol) is None:
			raise ValueError("Opening provider requires exact supported source and symbol")
		if not session.opens_at + timedelta(minutes=1) <= clock.now() < session.closes_at:
			raise ValueError("Capture openings after the first minute and before the entry close")
		start = int(session.opens_at.timestamp() * 1000)
		try:
			response = self.transport.get(OPENING_ENDPOINT.format(ticker=symbol, start=start, end=start + 59999), identity.adjustment is PriceAdjustment.ADJUSTED)
			payload = mapping(response.payload)
			if response.status != 200 or payload.get("status") != "OK" or payload.get("ticker") != symbol or type(payload.get("adjusted")) is not bool or payload["adjusted"] != (identity.adjustment is PriceAdjustment.ADJUSTED) or payload.get("next_url"):
				raise ValueError("Unverified opening response")
			rows = payload.get("results", [])
			if not isinstance(rows, list) or type(payload.get("resultsCount")) is not int or payload["resultsCount"] != len(rows) or len(rows) > 1:
				raise ValueError("Invalid opening response count")
			if not rows:
				return None
			row = mapping(rows[0])
			price = row.get("o")
			if type(row.get("t")) is not int or row["t"] != start or isinstance(price, bool) or not isinstance(price, (int, float)):
				raise ValueError("Opening is not the requested first regular-session minute")
			return OpeningObservation(ticker_id, symbol, session, float(price), clock.now(), content_id({"endpoint": OPENING_ENDPOINT, "start": start, "end": start + 59999, "symbol": symbol, "adjusted": payload["adjusted"], "status": "OK", "resultsCount": 1, "open": price}))
		except Exception:
			raise ValueError("Opening provider request or evidence validation failed") from None
