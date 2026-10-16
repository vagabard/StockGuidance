from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_positive_int
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.market_data.identity import content_id, mapping, text_field
from stockguidance.market_data.openings import OPENING_ENDPOINT, OPENING_METHOD, OpeningData, OpeningObservation, OpeningQuery


class OpeningStore:
	def __init__(self, root: Path) -> None:
		self.root = root

	def path_for(self, source: SourceIdentity, observation_date: str, ticker_id: int) -> Path:
		if date.fromisoformat(observation_date).isoformat() != observation_date:
			raise ValueError("Opening receipt path requires an exact ISO date")
		require_positive_int(ticker_id, "opening receipt ticker ID")
		identity = content_id({"source": asdict(source), "endpoint": OPENING_ENDPOINT, "method": OPENING_METHOD})
		return self.root / "market_data" / "openings" / identity / observation_date / f"{ticker_id}.json"

	def save(self, source: SourceIdentity, observation: OpeningObservation) -> None:
		payload = {"schema": "stockguidance-opening-1", "source": asdict(source), "endpoint": OPENING_ENDPOINT, "method": observation.method, "ticker_id": observation.ticker_id, "symbol": observation.symbol, "session": observation.session.session_date.isoformat(), "calendar": observation.session.calendar_id, "observed_at": observation.session.opens_at.isoformat(), "available_at": observation.available_at.isoformat(), "price": observation.price, "response_identity": observation.response_identity, "revision": observation.revision}
		atomic_write(self.path_for(source, observation.session.session_date.isoformat(), observation.ticker_id), json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8"))

	def read_openings(self, query: OpeningQuery) -> OpeningData:
		observations, issues = [], []
		for session in query.sessions:
			for ticker_id in query.ticker_ids:
				state, detail = PriceState.MISSING, "Opening receipt is missing; capture with update_openings or recommendation refresh"
				if query.as_of < session.opens_at + timedelta(minutes=1):
					state, detail = PriceState.NOT_YET_AVAILABLE, "First regular-session minute is not yet complete"
				else:
					try:
						payload = mapping(json.loads(self.path_for(query.boundary.source, session.session_date.isoformat(), ticker_id).read_text(encoding="utf-8")))
						if set(payload) != {"schema", "source", "endpoint", "method", "ticker_id", "symbol", "session", "calendar", "observed_at", "available_at", "price", "response_identity", "revision"} or payload["schema"] != "stockguidance-opening-1" or payload["source"] != json.loads(json.dumps(asdict(query.boundary.source))) or payload["endpoint"] != OPENING_ENDPOINT or payload["method"] != OPENING_METHOD or type(payload["ticker_id"]) is not int or payload["ticker_id"] != ticker_id or payload["session"] != session.session_date.isoformat() or payload["calendar"] != session.calendar_id or payload["observed_at"] != session.opens_at.isoformat():
							raise ValueError("Opening identity mismatch")
						price = payload["price"]
						if isinstance(price, bool) or not isinstance(price, (int, float)):
							raise ValueError("Invalid opening price")
						observation = OpeningObservation(ticker_id, text_field(payload, "symbol"), session, float(price), datetime.fromisoformat(text_field(payload, "available_at")), text_field(payload, "response_identity"))
						if payload["revision"] != observation.revision:
							raise ValueError("Opening receipt content changed")
						if observation.available_at > query.as_of:
							state, detail = PriceState.NOT_YET_AVAILABLE, "Opening receipt arrived after the decision cutoff; historical availability cannot be invented"
						else:
							observations.append(observation)
							continue
					except FileNotFoundError:
						pass
					except (ValueError, KeyError, TypeError):
						state, detail = PriceState.UNVERIFIED, "Opening receipt is corrupt or has incompatible source/method metadata"
				issues.append(CloseIssue(ticker_id, session, state, detail))
		return OpeningData(query, tuple(observations), tuple(issues))
