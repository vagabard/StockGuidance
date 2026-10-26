from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
from time import perf_counter, process_time
from typing import Any
from zoneinfo import ZoneInfo

from stockguidance.database.configuration import MySQLDriver, load_database_configuration
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.mysql_market_read import decode_derived_row as _decode_derived_row
from stockguidance.database.repositories.mysql_market_read import decode_raw_row as _decode_raw_row
from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow, derived_study_id, legacy_rows
from stockguidance.market_data.records import DailyBar, count_value, records_id
from stockguidance.market_data.archive import decode_archive
from stockguidance.market_data.records import canonical_decimal
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.validation.derived import differing_columns


DERIVED_BATCH_SIZE = 1


@lru_cache(maxsize=32)
def _archive_calendar(year: int) -> ExchangeCalendar:
	return ExchangeCalendar("XNYS", date(year, 1, 1), date(year, 12, 31))


@dataclass(frozen=True)
class ArchiveBar:
	ticker: str
	open: Decimal
	high: Decimal
	low: Decimal
	close: Decimal
	volume: Decimal
	vwap: Decimal | None
	transactions: int | None


def archive_price(value: Any) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError("Invalid archive price")
	price = Decimal(str(value))
	if not price.is_finite() or not 0 < price < Decimal("1e60"):
		raise ValueError("Invalid archive price")
	with localcontext() as context:
		context.prec = 70
		result = price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
	if result <= 0:
		raise ValueError("Archive price rounds to zero")
	return result


def decode_archive_bar(row: dict[str, Any]) -> ArchiveBar:
	value = row["v"]
	if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
		raise ValueError("Invalid archive volume")
	volume = Decimal(str(value))
	if not volume.is_finite() or not 0 <= volume <= Decimal(2**63 - 1):
		raise ValueError("Invalid archive volume")
	ticker = row["T"]
	if not isinstance(ticker, str) or not ticker or len(ticker) > 16 or any(character.isspace() for character in ticker):
		raise ValueError("Invalid archive ticker")
	prices = tuple(archive_price(row[field]) for field in ("o", "h", "l", "c"))
	bar = ArchiveBar(ticker, prices[0], prices[1], prices[2], prices[3], volume, None if row.get("vw") is None else archive_price(row["vw"]), None if row.get("n") is None else count_value(row["n"]))
	if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
		raise ValueError("Inconsistent archive OHLC")
	return bar


def archive_records_id(bars: tuple[ArchiveBar, ...]) -> str:
	rows = []
	for item in sorted(bars, key=lambda item: item.ticker):
		volume = format(item.volume, "f")
		volume = volume.rstrip("0").rstrip(".") if "." in volume else volume
		row = {"T": item.ticker, "o": str(item.open), "h": str(item.high), "l": str(item.low), "c": str(item.close), "v": "0" if item.volume == 0 else volume, "vw": None if item.vwap is None else str(item.vwap), "n": item.transactions}
		rows.append(row)
	return sha256(encode_json(rows)).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for name, value in pairs:
		if name in result:
			raise ValueError("Duplicate JSON field")
		result[name] = value
	return result


def inventory(root: Path) -> tuple[Path, ...]:
	paths = tuple(sorted((root / "market_summary").glob("[0-9][0-9][0-9][0-9]/*.ndjson")))
	if not paths:
		raise ValueError("No retained archive files")
	for path in paths:
		session = date.fromisoformat(path.stem)
		if path.parent.name != str(session.year):
			raise ValueError("Archive year and session disagree")
	return paths


def read_archive(path: Path) -> tuple[tuple[ArchiveBar, ...], str]:
	expected = date.fromisoformat(path.stem)
	digest = sha256()
	bars: dict[str, ArchiveBar] = {}
	with path.open("rb") as stream:
		for line in stream:
			digest.update(line)
			row = json.loads(line, parse_float=Decimal, object_pairs_hook=unique_object)
			if not isinstance(row, dict):
				raise ValueError("Archive record must be an object")
			for name in ("t", "n"):
				value = row.get(name)
				if name == "n" and value is None:
					continue
				if type(value) is not int or not 0 <= value <= 2**63 - 1:
					raise ValueError("Invalid archive integer")
			observed = datetime.fromtimestamp(row["t"] / 1000, timezone.utc)
			if observed.astimezone(ZoneInfo("America/New_York")).date() != expected:
				raise ValueError("Archive timestamp and session disagree")
			bar = decode_archive_bar(row)
			if bar.ticker in bars:
				raise ValueError("Duplicate archive ticker")
			bars[bar.ticker] = bar
	if not bars:
		raise ValueError("Empty archive does not establish no trading")
	return tuple(sorted(bars.values(), key=lambda item: item.ticker)), digest.hexdigest()


def streaming_connection(config_path: Path) -> Any:
	configuration = load_database_configuration(config_path)
	drivers = (MySQLDriver.CONNECTOR, MySQLDriver.PYMYSQL) if configuration.driver is MySQLDriver.AUTO else (configuration.driver,)
	for driver in drivers:
		try:
			module = import_module("mysql.connector" if driver is MySQLDriver.CONNECTOR else "pymysql")
		except ImportError:
			continue
		parameters: dict[str, Any] = {"host": configuration.host, "database": configuration.database, "user": configuration.user, "password": configuration.password, "port": configuration.port, "autocommit": False}
		if driver is MySQLDriver.CONNECTOR:
			parameters.update(buffered=False, connection_timeout=30)
		else:
			parameters.update(charset="utf8mb4", cursorclass=module.cursors.SSCursor, connect_timeout=30)
		try:
			return module.connect(**parameters)
		except Exception:
			raise RuntimeError("Configured archive-study connection failed") from None
	raise RuntimeError("No supported database driver")


def batched_rows(cursor: Any) -> Any:
	while batch := cursor.fetchmany(1000):
		yield from batch


def read_database(connection: Any, config_path: Path, session: date) -> tuple[ArchiveBar, ...]:
	names = load_database_configuration(config_path).load_table_names()
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id FROM {quote_identifier(names.sessions)} WHERE transaction_date = %s", (session,))
		mappings = list(cursor)
		if len(mappings) > 1:
			raise ValueError("Duplicate session mappings")
		if not mappings:
			return ()
		cursor.execute(
			f"SELECT s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM {quote_identifier(names.prices)} r JOIN {quote_identifier(names.tickers)} s ON s.id = r.ticker_id WHERE r.date_id = %s ORDER BY s.label",
			(mappings[0][0],),
		)
		bars = tuple(decode_archive_bar(dict(zip(("T", "o", "h", "l", "c", "v", "vw", "n"), row))) for row in batched_rows(cursor))
		if len({item.ticker for item in bars}) != len(bars):
			raise ValueError("Duplicate database ticker")
		return bars
	finally:
		cursor.close()


def read_derived_database(
	connection: Any,
	config_path: Path,
	sessions: tuple[date, ...],
) -> tuple[dict[date, tuple[DailyBar, ...]], dict[date, tuple[LegacyDerivedRow, ...]], int]:
	if not sessions or len(set(sessions)) != len(sessions):
		raise ValueError("Derived database scope requires distinct sessions")
	names = load_database_configuration(config_path).load_table_names()
	placeholders = ",".join("%s" for _ in sessions)
	cursor = connection.cursor()
	query_count = 0
	try:
		cursor.execute(
			f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date, id",
			sessions,
		)
		query_count += 1
		date_by_id: dict[int, date] = {}
		for row in batched_rows(cursor):
			if len(row) != 2:
				raise ValueError("Invalid derived date mapping")
			date_id = normalize_date_id(row[0])
			session = normalize_session_date(row[1])
			if session not in sessions or session in date_by_id.values() or date_id in date_by_id:
				raise ValueError("Duplicate or out-of-scope derived date mapping")
			date_by_id[date_id] = session

		ids = tuple(date_by_id)
		if not ids:
			return {}, {}, query_count
		id_placeholders = ",".join("%s" for _ in ids)
		cursor.execute(
			f"SELECT r.date_id, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM {quote_identifier(names.prices)} r JOIN {quote_identifier(names.tickers)} s ON s.id = r.ticker_id WHERE r.date_id IN ({id_placeholders}) ORDER BY r.date_id, s.label",
			ids,
		)
		query_count += 1
		raw: dict[int, list[DailyBar]] = {}
		for row in batched_rows(cursor):
			date_id, bar = _decode_raw_row(row)
			if date_id not in date_by_id:
				raise ValueError("Derived raw row is outside the bounded date scope")
			raw.setdefault(date_id, []).append(bar)

		columns = ", ".join(f"r.{column}" for column in DERIVED_COLUMNS)
		cursor.execute(
			f"SELECT r.date_id, s.label, {columns} FROM {quote_identifier(names.deltas)} r JOIN {quote_identifier(names.tickers)} s ON s.id = r.ticker_id WHERE r.date_id IN ({id_placeholders}) ORDER BY r.date_id, s.label",
			ids,
		)
		query_count += 1
		derived: dict[int, list[LegacyDerivedRow]] = {}
		for row in batched_rows(cursor):
			date_id, derived_row = _decode_derived_row(row)
			if date_id not in date_by_id:
				raise ValueError("Derived row is outside the bounded date scope")
			derived.setdefault(date_id, []).append(derived_row)
		return (
			{session: tuple(raw.get(date_id, ())) for date_id, session in date_by_id.items()},
			{session: tuple(derived.get(date_id, ())) for date_id, session in date_by_id.items()},
			query_count,
		)
	finally:
		cursor.close()


def _derived_session_result(
	central: date,
	prior: date,
	following: date,
	raw: dict[date, tuple[DailyBar, ...]],
	stored: dict[date, tuple[LegacyDerivedRow, ...]],
) -> dict[str, Any]:
	column_counts = {column: 0 for column in DERIVED_COLUMNS}
	current = raw.get(central, ())
	previous = raw.get(prior, ())
	next_rows = raw.get(following, ())
	result: dict[str, Any] = {
		"session": central.isoformat(),
		"prior_session": prior.isoformat(),
		"following_session": following.isoformat(),
		"raw_counts": {"prior": len(previous), "current": len(current), "following": len(next_rows)},
		"derived_count": len(stored.get(central, ())),
		"expected_count": 0,
		"missing_derived_keys": [],
		"extra_derived_keys": [],
		"column_difference_counts": column_counts,
		"expected_derived_id": None,
		"stored_derived_id": None,
		"raw_ids": {"prior": None, "current": None, "following": None},
		"state": "unavailable",
		"reason": None,
		"matches": False,
	}
	for name, records in (("prior", previous), ("current", current), ("following", next_rows)):
		if records:
			result["raw_ids"][name] = records_id(records)
	if not current or not previous or not next_rows:
		result["reason"] = "missing_raw_context"
		return result
	expected = legacy_rows(previous, current, next_rows)
	actual = tuple(stored.get(central, ()))
	expected_by_ticker = {row.ticker: row for row in expected}
	actual_by_ticker = {row.ticker: row for row in actual}
	missing = sorted(set(expected_by_ticker) - set(actual_by_ticker))
	extra = sorted(set(actual_by_ticker) - set(expected_by_ticker))
	for ticker in sorted(set(expected_by_ticker) & set(actual_by_ticker)):
		for index in differing_columns(actual_by_ticker[ticker], expected_by_ticker[ticker]):
			column_counts[DERIVED_COLUMNS[index]] += 1
	result.update(
			{
				"expected_count": len(expected),
				"missing_derived_keys": missing,
				"extra_derived_keys": extra,
				"expected_derived_id": derived_study_id(expected),
				"stored_derived_id": derived_study_id(actual),
				"state": "complete",
				"matches": not missing and not extra and not any(column_counts.values()),
			}
		)
	return result


def run_derived_chunk(arguments: tuple[list[str], dict[str, list[str]], str | None]) -> dict[str, Any]:
	target_paths, neighbors, configuration = arguments
	if configuration is None:
		raise ValueError("Derived study requires database configuration")
	connection = None
	started, cpu_started = perf_counter(), process_time()
	try:
		connection = streaming_connection(Path(configuration))
		cursor = connection.cursor()
		try:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION READ ONLY")
		finally:
			cursor.close()
		results = []
		data_queries = 0
		raw_rows = 0
		derived_rows = 0
		for start in range(0, len(target_paths), DERIVED_BATCH_SIZE):
			batch = target_paths[start:start + DERIVED_BATCH_SIZE]
			context_dates = sorted({date.fromisoformat(Path(path).stem) for path in batch for path in neighbors[path]})
			raw, stored, batch_queries = read_derived_database(connection, Path(configuration), tuple(context_dates))
			data_queries += batch_queries
			raw_rows += sum(len(rows) for rows in raw.values())
			derived_rows += sum(len(rows) for rows in stored.values())
			for path in batch:
				prior, central, following = neighbors[path]
				results.append(
					_derived_session_result(
						date.fromisoformat(Path(central).stem),
						date.fromisoformat(Path(prior).stem),
						date.fromisoformat(Path(following).stem),
						raw,
						stored,
					)
				)
		return {
			"sessions": results,
			"worker_seconds": perf_counter() - started,
			"cpu_seconds": process_time() - cpu_started,
			"query_count": data_queries + 2,
			"data_query_count": data_queries,
			"raw_rows": raw_rows,
			"derived_rows": derived_rows,
		}
	finally:
		if connection is not None:
			try:
				connection.rollback()
			finally:
				connection.close()


def run_chunk(arguments: tuple[list[str], str, str | None]) -> dict[str, Any]:
	paths, mode, configuration = arguments
	connection = None
	started, cpu_started = perf_counter(), process_time()
	results: list[dict[str, Any]] = []
	try:
		if mode not in {"files", "canonical"}:
			if configuration is None:
				raise ValueError("Database mode requires configuration")
			connection = streaming_connection(Path(configuration))
			cursor = connection.cursor()
			try:
				cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
				cursor.execute("START TRANSACTION READ ONLY")
			finally:
				cursor.close()
		for name in paths:
			path = Path(name)
			result: dict[str, Any] = {"session": path.stem}
			if mode == "canonical":
				session_date = date.fromisoformat(path.stem)
				calendar = _archive_calendar(session_date.year)
				content = path.read_bytes()
				response = decode_archive(content, calendar.session(session_date))
				result.update(file_sha256=sha256(content).hexdigest(), archive_id=records_id(response.records), archive_count=len(response.records) + len(response.bad_records),
					bad_records={ticker: [None if value is None else canonical_decimal(Decimal(str(value))) for value in values] for ticker, values in response.bad_records})
				results.append(result)
				continue
			if mode != "database":
				bars, file_hash = read_archive(path)
				result.update(file_sha256=file_hash, archive_id=archive_records_id(bars), archive_count=len(bars))
			if mode != "files":
				stored = read_database(connection, Path(str(configuration)), date.fromisoformat(path.stem))
				result.update(database_id=archive_records_id(stored), database_count=len(stored))
			if mode == "reconcile":
				result["matches"] = result["archive_id"] == result["database_id"]
			results.append(result)
	finally:
		if connection is not None:
			try:
				connection.rollback()
			finally:
				connection.close()
	return {"sessions": results, "worker_seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu_started}


def run_request(request: dict[str, Any]) -> dict[str, Any]:
	workers = request["workers"]
	if type(workers) is not int or not 1 <= workers <= 3:
		raise ValueError("Workers must be between one and three")
	mode = request["mode"]
	if mode not in {"files", "canonical", "database", "reconcile", "derived"}:
		raise ValueError("Unsupported study mode")
	paths = request["paths"]
	if not paths or len(set(paths)) != len(paths):
		raise ValueError("Study requires distinct paths")
	repeats = request.get("repeats", 4)
	if type(repeats) is not int or not 1 <= repeats <= 10:
		raise ValueError("Repeats must be between one and ten")
	if mode == "derived":
		neighbors = request.get("neighbors")
		if not isinstance(neighbors, dict) or set(neighbors) != set(paths):
			raise ValueError("Derived study requires a neighbor triple for every target")
		for path in paths:
			triple = neighbors[path]
			if not isinstance(triple, list) or len(triple) != 3 or triple[1] != path or len(set(triple)) != 3:
				raise ValueError("Derived study neighbor triples are invalid")
		configuration = request.get("dbinfo_file")
		if not isinstance(configuration, str) or not configuration:
			raise ValueError("Derived study requires database configuration")
		chunks = [(paths[index::workers], neighbors, configuration) for index in range(workers) if paths[index::workers]]
		measurements: list[dict[str, Any]] = []
		with ProcessPoolExecutor(max_workers=workers) if workers > 1 else _SerialExecutor() as executor:
			for repeat in range(repeats):
				started = perf_counter()
				parts = list(executor.map(run_derived_chunk, chunks))
				sessions = sorted((session for part in parts for session in part["sessions"]), key=lambda item: item["session"])
				measurements.append(
					{
						"seconds": perf_counter() - started,
						"cpu_seconds": sum(part["cpu_seconds"] for part in parts),
						"query_count": sum(part["query_count"] for part in parts),
						"data_query_count": sum(part["data_query_count"] for part in parts),
						"raw_rows": sum(part["raw_rows"] for part in parts),
						"derived_rows": sum(part["derived_rows"] for part in parts),
						"digest": sha256(encode_json(sessions)).hexdigest(),
						"sessions": sessions,
					}
				)
		return {"schema_version": 1, "backend": "python", "workers": workers, "mode": mode, "measurements": measurements}
		run_chunks: list[tuple[list[str], str, str | None]] = [(paths[index::workers], mode, request.get("dbinfo_file")) for index in range(workers) if paths[index::workers]]
	measurements = []
	with ProcessPoolExecutor(max_workers=workers) if workers > 1 else _SerialExecutor() as executor:
		for repeat in range(repeats):
			started = perf_counter()
			parts = list(executor.map(run_chunk, run_chunks))
			sessions = sorted((session for part in parts for session in part["sessions"]), key=lambda item: item["session"])
			measurements.append({"seconds": perf_counter() - started, "cpu_seconds": sum(part["cpu_seconds"] for part in parts), "digest": sha256(encode_json(sessions)).hexdigest(), "sessions": sessions})
	return {"schema_version": 1, "backend": "python", "workers": workers, "mode": mode, "measurements": measurements}


class _SerialExecutor:
	def __enter__(self) -> "_SerialExecutor":
		return self

	def __exit__(self, *arguments: object) -> None:
		pass

	def map(self, function: Any, arguments: Any) -> Any:
		return map(function, arguments)
