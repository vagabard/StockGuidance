from enum import StrEnum


class ObservationInterval(StrEnum):
	DAILY = "1d"
	HOURLY = "1h"
	FIVE_MINUTES = "5m"


def require_daily_interval(interval: ObservationInterval) -> None:
	if not isinstance(interval, ObservationInterval):
		raise ValueError("interval must be an ObservationInterval")
	if interval is not ObservationInterval.DAILY:
		raise ValueError("Only daily observations are implemented; intraday intervals are reserved")
