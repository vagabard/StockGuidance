from pathlib import Path

from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.reporting.documents import ReportKind


def write_report(path: Path, payload: dict[str, object]) -> None:
	ReportKind(str(payload.get("schema")))
	if type(payload.get("successful")) is not bool:
		raise ValueError("Reports must state their success status")
	atomic_write(path, encode_json(payload))
