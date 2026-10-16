from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.openings import OpeningData, OpeningQuery, OpeningRepository


def opening_values(specification: FeatureSpec, timing: DecisionTiming, boundary: HistoryBoundary, repository: CloseRepository) -> OpeningData:
	query = OpeningQuery(boundary, (timing.entry_session,), specification.ticker_ids, timing.decision_cutoff)
	if isinstance(repository, OpeningRepository):
		data = repository.read_openings(query)
		if not isinstance(data, OpeningData) or data.query != query:
			raise ValueError("Opening repository changed source, session, cutoff or ticker order")
		return data
	return OpeningData(query, (), tuple(CloseIssue(ticker_id, timing.entry_session, PriceState.UNVERIFIED, "No evidenced opening repository is configured") for ticker_id in specification.ticker_ids))
