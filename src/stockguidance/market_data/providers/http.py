from dataclasses import dataclass, field

from stockguidance.market_data.providers.polygon import HttpResponse, provider_failure


@dataclass(frozen=True)
class PolygonHttpTransport:
	api_key: str = field(repr=False)
	max_retries: int = 3
	minimum_interval_seconds: float = 15.0

	def __post_init__(self) -> None:
		if not self.api_key or type(self.max_retries) is not int or not 0 <= self.max_retries <= 10 or self.minimum_interval_seconds < 0:
			raise ValueError("Invalid HTTP configuration")

	def get(self, path: str, adjusted: bool) -> HttpResponse:
		import json
		import time
		from urllib.error import HTTPError
		from urllib.request import Request, urlopen

		if not path.startswith("/v2/aggs/grouped/locale/us/market/stocks/") or "?" in path:
			raise ValueError("Unsupported provider path")
		for attempt in range(self.max_retries + 1):
			time.sleep(self.minimum_interval_seconds)
			request = Request(f"https://api.polygon.io{path}?adjusted={str(adjusted).lower()}&include_otc=false", headers={"Authorization": f"Bearer {self.api_key}"})
			try:
				with urlopen(request, timeout=60) as response:
					payload: object = json.loads(response.read())
					return HttpResponse(response.status, payload)
			except HTTPError as error:
				status = error.code
				error.close()
				if status not in {429, 500, 502, 503, 504} or attempt == self.max_retries:
					raise provider_failure() from None
			except Exception:
				if attempt == self.max_retries:
					raise provider_failure() from None
			time.sleep(min(2**attempt, 60))
		raise provider_failure()
