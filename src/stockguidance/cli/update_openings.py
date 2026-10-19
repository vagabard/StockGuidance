import argparse
from datetime import date
import json
from pathlib import Path

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.market_data.opening_refresh import OpeningRefresh
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.providers.configured_openings import ConfiguredOpeningProvider
from stockguidance.training.jobs import load_job, open_job_history


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Capture first-regular-minute openings for the full explicit universe, before the entry close; no daily-bar reads for that session.")
	parser.add_argument("--config", required=True, type=Path, help="Explicit entry_open training job defining source and full ordered universe")
	parser.add_argument("--session", required=True, type=date.fromisoformat)
	arguments = parser.parse_args(argv)
	try:
		job = load_job(arguments.config)
		history = open_job_history(job, connect=MySQLConnectionFactory())
		entry = history.calendar.session(arguments.session)
		clock = SystemClock()
		timing = DecisionTiming(history.calendar.shift(entry, -1), entry, history.calendar.shift(entry, 1), clock.now())
		result = OpeningRefresh(job.source, OpeningStore(job.source.data_root), ConfiguredOpeningProvider(), history.closes).refresh(job.features, timing, history.boundary, clock)
		print(json.dumps({"successful": True, "session": entry.session_date.isoformat(), "ticker_ids": result.query.ticker_ids, "opening_revision": result.revision, "available_at": max(item.available_at for item in result.observations).isoformat()}))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Full-universe opening capture requires valid source/history, first-minute provider access and a pre-close cutoff"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
