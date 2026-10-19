from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
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
from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.records import count_value


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
	if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
		raise ValueError("Invalid archive volume")
	volume = Decimal(str(value))
	if not volume.is_finite() or not 0 <= volume <= Decimal(2**63 - 1):
		raise ValueError("Invalid archive volume")
	ticker = row["T"]
	if not isinstance(ticker, str) or not ticker or len(ticker) > 16 or any(character.isspace() for character in ticker):
		raise ValueError("Invalid archive ticker")
	bar = ArchiveBar(ticker, *(archive_price(row[field]) for field in ("o", "h", "l", "c")), volume, None if row.get("vw") is None else archive_price(row["vw"]), None if row.get("n") is None else count_value(row["n"]))
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
			parameters["buffered"] = False
		else:
			parameters.update(charset="utf8mb4", cursorclass=module.cursors.SSCursor)
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


def run_chunk(arguments: tuple[list[str], str, str | None]) -> dict[str, Any]:
	paths, mode, configuration = arguments
	connection = None
	started, cpu_started = perf_counter(), process_time()
	results: list[dict[str, Any]] = []
	try:
		if mode != "files":
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
	if mode not in {"files", "database", "reconcile"}:
		raise ValueError("Unsupported study mode")
	paths = request["paths"]
	if not paths or len(set(paths)) != len(paths):
		raise ValueError("Study requires distinct paths")
	repeats = request.get("repeats", 4)
	if type(repeats) is not int or not 1 <= repeats <= 10:
		raise ValueError("Repeats must be between one and ten")
	chunks = [(paths[index::workers], mode, request.get("dbinfo_file")) for index in range(workers) if paths[index::workers]]
	measurements = []
	with ProcessPoolExecutor(max_workers=workers) if workers > 1 else _SerialExecutor() as executor:
		for repeat in range(repeats):
			started = perf_counter()
			parts = list(executor.map(run_chunk, chunks))
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
