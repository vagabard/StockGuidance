"""Run target maintenance one session at a time and stop on the first failure."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
import sys

from stockguidance.database.connections import MySQLConnectionFactory


def inspect(dbinfo: Path, day: date) -> tuple[int, int, int, int]:
	connection = MySQLConnectionFactory()(dbinfo)
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT COUNT(*) FROM stockguidance_pending_updates")
		pending = int(cursor.fetchone()[0])
		cursor.execute("SELECT id,raw_complete,delta_complete FROM transaction_dates WHERE transaction_date=%s", (day,))
		row = cursor.fetchone()
		if row is None:
			return pending, 0, 0, 0
		cursor.execute("SELECT COUNT(*) FROM stockguidance_zscore_sessions WHERE date_id=%s", (row[0],))
		return pending, int(row[1]), int(row[2]), int(cursor.fetchone()[0])
	finally:
		cursor.close()
		connection.close()


def pending_raw_date(dbinfo: Path) -> date | None:
	connection = MySQLConnectionFactory()(dbinfo)
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(scope,'$.clear.raw[0].session_date')) FROM stockguidance_pending_updates")
		rows = cursor.fetchall()
		if len(rows) != 1 or rows[0][0] is None:
			return None
		return date.fromisoformat(str(rows[0][0]))
	finally:
		cursor.close()
		connection.close()


def write_ticker_readiness(dbinfo: Path, days: list[date], output: Path) -> None:
	from revalidate_stage12_scope import validate
	report = validate(dbinfo, min(days[0], date(2026, 10, 23)), days[-1], output)
	print(json.dumps({"validation_report": str(output), "complete": report["stored_coverage_complete"], "findings": len(report["findings"])}), flush=True)
	if not report["stored_coverage_complete"]:
		raise RuntimeError("AAPL/MSFT validation did not pass; inspect the readiness report")


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True)
	parser.add_argument("--start-date", type=date.fromisoformat, required=True)
	parser.add_argument("--end-date", type=date.fromisoformat, required=True)
	parser.add_argument("--output-dir", type=Path, required=True)
	args = parser.parse_args()
	args.output_dir.mkdir(parents=True, exist_ok=True)
	connection = MySQLConnectionFactory()(args.dbinfo_file)
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT transaction_date FROM transaction_dates WHERE transaction_date BETWEEN %s AND %s ORDER BY transaction_date", (args.start_date, args.end_date))
		days = [row[0] for row in cursor.fetchall()]
	finally:
		cursor.close()
		connection.close()
	if not days:
		print("No registered sessions in requested range", flush=True)
		return 2
	for day in days:
		pending, raw, delta, zscore = inspect(args.dbinfo_file, day)
		if pending and pending_raw_date(args.dbinfo_file) != day:
			print(json.dumps({"date": str(day), "pending": pending, "stop": "another date has a pending operation"}), flush=True)
			return 2
		if not pending and raw and delta and zscore == 4:
			print(json.dumps({"date": str(day), "status": "already_complete"}), flush=True)
			continue
		out = args.output_dir / f"maintenance-{day}.json"
		err = args.output_dir / f"maintenance-{day}.log"
		with out.open("w", encoding="utf-8") as stdout, err.open("w", encoding="utf-8") as stderr:
			process = subprocess.run(
				[sys.executable, "-m", "stockguidance.cli.maintain_daily", "--dbinfo-file", str(args.dbinfo_file), "--start-date", str(day), "--end-date", str(day)],
				stdout=stdout, stderr=stderr, check=False,
			)
		pending, raw, delta, zscore = inspect(args.dbinfo_file, day)
		print(json.dumps({"date": str(day), "exit_code": process.returncode, "pending": pending, "raw_complete": raw, "delta_complete": delta, "zscore_sessions": zscore}), flush=True)
		if pending or not (raw and delta and zscore == 4):
			return 2
	write_ticker_readiness(args.dbinfo_file, days, args.output_dir / "aapl-msft-readiness.json")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
