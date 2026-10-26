from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.market_data.archive import archive_content, archive_path
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState


def download_canonical(source: SourceConfig, start: date, end: date, *, refresh: bool, max_retries: int = 3) -> dict[str, Any]:
	if start > end:
		raise ValueError("Download start must not follow its end")
	if source.data_configuration.adjustment.value != "unadjusted":
		raise ValueError("Download-only canonical mode requires an unadjusted data configuration")
	calendar = ExchangeCalendar(source.calendar_id, date(1970, 1, 1), end)
	provider = ConfiguredPolygonProvider(max_retries)
	downloaded = 0
	skipped = 0
	files: list[dict[str, Any]] = []
	for session in calendar.sessions(start, end):
		path = archive_path(source.data_root, session.session_date)
		if path.exists() and not refresh:
			skipped += 1
			continue
		response = provider.fetch(source, session)
		if response.state is not ResponseState.COMPLETE:
			raise ValueError(f"Provider did not return a complete response for {session.session_date}")
		download = DownloadedSession(session, response, datetime.now(timezone.utc))
		content = archive_content(download)
		content_id = sha256(content).hexdigest()
		if path.exists() and path.read_bytes() != content:
			previous = path.read_bytes()
			atomic_write(source.data_root / "archive-backup" / sha256(previous).hexdigest() / path.name, previous)
		atomic_write(path, content)
		downloaded += 1
		files.append({"date": session.session_date.isoformat(), "path": str(path), "sha256": content_id, "rows": len(response.records) + len(response.bad_records)})
	return {"successful": True, "downloaded": downloaded, "skipped": skipped, "start": start.isoformat(), "end": end.isoformat(), "files": files}
