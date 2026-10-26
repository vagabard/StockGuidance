import argparse
from datetime import date
import json
from pathlib import Path
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import SourceConfig
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.table_names import TableRole, load_table_names, quote_identifier
from stockguidance.market_data.archive import archive_path, decode_archive
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import data_configuration_key, encode_json
from stockguidance.market_data.records import records_id
from stockguidance.database.repositories.mysql_market_read import decode_raw_row


def _source(arguments: argparse.Namespace) -> SourceConfig:
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	configuration = load_database_configuration(database_path)
	if configuration.data_configuration_file is None:
		raise ValueError("Source transition requires an explicit data_configuration_file")
	data_configuration = configuration.load_data_configuration()
	if data_configuration.adjustment.value != arguments.expected_adjustment:
		raise ValueError("Expected adjustment does not match the explicit data configuration")
	return SourceConfig(data_configuration.to_source_identity(), Path(arguments.data_root).resolve(), database_path, "administrative source transition", _configuration=data_configuration)


def verify(arguments: argparse.Namespace) -> dict[str, Any]:
	source = _source(arguments)
	names = load_table_names()
	connect = MySQLConnectionFactory()
	connection = connect(source.database_config)
	mismatches: list[dict[str, Any]] = []
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT MIN(transaction_date), MAX(transaction_date) FROM {quote_identifier(names.table(TableRole.SESSIONS))}", ())
			bounds = cursor.fetchall()
			if not bounds or bounds[0][0] is None or bounds[0][1] is None:
				raise ValueError("Database has no transaction sessions")
			first = date.fromisoformat(str(arguments.start_date)) if arguments.start_date else date.fromisoformat(str(bounds[0][0]))
			last = date.fromisoformat(str(arguments.end_date)) if arguments.end_date else date.fromisoformat(str(bounds[0][1]))
			calendar = ExchangeCalendar(source.calendar_id, date(1970, 1, 1), last)
			cursor.execute(f"SELECT transaction_date FROM {quote_identifier(names.table(TableRole.SESSIONS))} WHERE transaction_date BETWEEN %s AND %s ORDER BY transaction_date", (first, last))
			dates = tuple(date.fromisoformat(str(row[0])) for row in cursor.fetchall())
			for session_date in dates:
				path = archive_path(source.data_root, session_date)
				try:
					response = decode_archive(path.read_bytes(), calendar.session(session_date))
					cursor.execute(f"SELECT t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM {quote_identifier(names.table(TableRole.PRICES))} p JOIN {quote_identifier(names.table(TableRole.TICKERS))} t ON t.id=p.ticker_id JOIN {quote_identifier(names.table(TableRole.SESSIONS))} s ON s.id=p.date_id WHERE s.transaction_date=%s ORDER BY t.label", (session_date,))
					rows = cursor.fetchall()
					records = []
					for row in rows:
						try:
							records.append(decode_raw_row((0, *row))[1])
						except (TypeError, ValueError):
							continue
					if len(rows) != len(response.records) + len(response.bad_records) or records_id(tuple(records)) != records_id(response.records):
						mismatches.append({"date": session_date.isoformat(), "reason": "raw rows differ from canonical archive", "database_rows": len(rows), "canonical_rows": len(response.records) + len(response.bad_records)})
				except (OSError, TypeError, ValueError, KeyError) as error:
					mismatches.append({"date": session_date.isoformat(), "reason": str(error), "path": str(path)})
		finally:
			cursor.close()
	finally:
		connection.close()
	report: dict[str, Any] = {"successful": not mismatches, "applied": False, "expected_adjustment": arguments.expected_adjustment, "start": first.isoformat(), "end": last.isoformat(), "sessions_checked": len(dates), "mismatch_count": len(mismatches), "mismatches": mismatches}
	if arguments.apply:
		if mismatches:
			raise ValueError(f"Cannot relabel database; {len(mismatches)} source mismatches remain")
		if not (arguments.backup_confirmation or "").strip():
			raise ValueError("A database backup confirmation is required")
		connection = connect(source.database_config)
		try:
			cursor = connection.cursor()
			try:
				configuration = source.data_configuration
				cursor.execute(f"UPDATE {quote_identifier(names.table(TableRole.SOURCE))} SET configuration_key=%s, source_key=%s, source_family=%s, endpoint=%s, market=%s, universe=%s, interval_code=%s, raw_price_split_adjusted=0, raw_price_dividend_adjusted=0, raw_price_vendor_defined_adjustment=NULL WHERE id=1", (data_configuration_key(configuration), data_configuration_key(configuration), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value))
				connection.commit()
			finally:
				cursor.close()
		finally:
			connection.close()
		report.update({"applied": True, "backup_confirmation": arguments.backup_confirmation})
	atomic_write(arguments.output / "source-transition-report.json", encode_json(report))
	return report


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Verify and optionally relabel the database source configuration")
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	parser.add_argument("--data-root", type=Path, default=Path("data"))
	parser.add_argument("--output", type=Path, default=Path("output/storage-consolidation"))
	parser.add_argument("--expected-adjustment", choices=("unadjusted",), default="unadjusted")
	parser.add_argument("--start-date", type=date.fromisoformat)
	parser.add_argument("--end-date", type=date.fromisoformat)
	parser.add_argument("--backup-confirmation")
	parser.add_argument("--apply", action="store_true")
	arguments = parser.parse_args(argv)
	try:
		print(json.dumps(verify(arguments), sort_keys=True))
		return 0
	except (OSError, TypeError, ValueError, KeyError) as error:
		print(json.dumps({"successful": False, "error": str(error)}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
