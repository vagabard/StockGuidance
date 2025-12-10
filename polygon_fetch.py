import argparse
import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from requests import Response

last_api_call = 0.0
POLYGON_BASE = "https://api.massive.com"
TICKERS_ENDPOINT = f"{POLYGON_BASE}/v3/reference/tickers"
TICKER_DETAILS_ENDPOINT_TMPL = f"{POLYGON_BASE}/v3/reference/tickers/{{ticker}}"
TRADES_ENDPOINT_TMPL = f"{POLYGON_BASE}/v3/trades/{{ticker}}"
# Daily summaries
MARKET_GROUPED_ENDPOINT_TMPL = f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/{{date}}"
OPEN_CLOSE_ENDPOINT_TMPL = f"{POLYGON_BASE}/v1/open-close/{{ticker}}/{{date}}"
REQUIRED_DELAY_BETWEEN_API_CALLS = .1 # seconds
# STARTING_DATE should be a string (YYYY-MM-DD) representing the date 2 years before today (UTC)
#STARTING_DATE = (datetime.now(timezone.utc) - timedelta(days=365 * 2)).strftime("%Y-%m-%d")

# Default API key provided by user; can be overridden by env or arg
DEFAULT_API_KEY = "y41KT5lnM51gLWEx_TbQ21DeQKxOabXk"

# Files to track progress
COMPLETED_FILE = "completed_symbols.txt"
CHECKPOINTS_FILE = "checkpoints.txt"
TICKERS_FILE = "tickers.txt"
TICKERS_FULL_FILE = "tickers_full.txt"


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Fetch Polygon.io tickers via v3/reference/tickers and download v3 trades "
			"for the last 2 years (day-by-day) for the first N tickers."
		)
	)
	parser.add_argument(
		"--api-key",
		default=os.getenv("POLYGON_API_KEY", DEFAULT_API_KEY),
		help="Polygon.io API key. Defaults to POLYGON_API_KEY env or built-in default.",
	)
	parser.add_argument(
		"--output-dir",
		default="data",
		help="Directory to write output files (NDJSON per ticker per day).",
	)
	parser.add_argument(
		"--max-tickers",
		type=int,
		default=5,
		help="Maximum number of tickers to process from the discovered list (default: 5).",
	)
	parser.add_argument(
		"--start-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If omitted, auto = now - 2 years (UTC)."
		),
	)
	parser.add_argument(
		"--end-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD, exclusive). If omitted, auto = today UTC."
		),
	)
	parser.add_argument(
		"--years-back",
		type=int,
		default=2,
		help=(
			"How many years in the past to fetch when --start-date is not provided (default: 2)."
		),
	)
	parser.add_argument(
		"--market",
		default="stocks",
		help=(
			"Ticker market filter for discovery (Polygon v3/reference/tickers). Default: stocks"
		),
	)
	parser.add_argument(
		"--active",
		default=True,
		type=lambda v: str(v).lower() in {"1", "true", "yes", "y"},
		help="Filter active tickers (true/false). Default: true",
	)
	parser.add_argument(
		"--exchanges",
		default="XNAS,XNYS,XASE",
		help=(
			"Comma-separated list of primary exchanges to include (e.g., XNAS,XNYS,XASE)."
		),
	)
	parser.add_argument(
		"--rps",
		type=float,
		default=4.0,
		help=(
			"Target requests per second throttle (approx). Set lower if hitting rate limits."
		),
	)
	parser.add_argument(
		"--max-retries",
		type=int,
		default=5,
		help="Max retries for 429/5xx with exponential backoff (default: 5).",
	)
	parser.add_argument(
		"--retry-checkpoints",
		action="store_true",
		help="Retry items from checkpoints.txt before normal processing.",
	)
	parser.add_argument(
		"--no-retry-checkpoints",
		action="store_true",
		help="Disable retrying checkpoints even if present.",
	)
	parser.add_argument(
		"--fetch-market-summary",
		action="store_true",
		help="Fetch and save the daily market grouped summary once per day (off by default).",
	)
	parser.add_argument(
		"--fetch-ticker-daily",
		action="store_true",
		help="Fetch and save the per-ticker daily summary (open/close) for each selected ticker/day (off by default).",
	)
	return parser.parse_args()


def daterange(start_date: datetime, end_date: datetime, reverse=True) -> Iterable[datetime]:
	target_days = (end_date - start_date).days
	cur = end_date if reverse else start_date
	for _ in range(target_days):
		yield cur
		cur = cur - timedelta(days=1) if reverse else cur + timedelta(days=1)


def iso_date(d: datetime) -> str:
	return d.strftime("%Y-%m-%d")


def day_bounds_utc(day: datetime) -> Tuple[str, str]:
	# Returns ISO8601 timestamps [start, end) for the UTC day
	start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
	end = start + timedelta(days=1)
	return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def throttle_sleep(last_call_ts: float) -> None:
	# crude throttle: ensure at most rps
	min_interval = REQUIRED_DELAY_BETWEEN_API_CALLS  # 1.0 / max(rps, 0.01)
	now = time.time()
	if last_call_ts and now - last_call_ts < min_interval:
		time.sleep(min_interval - (now - last_call_ts[0]))
	last_call_ts = time.time()


def http_get(url: str, params: Dict[str, str], headers: Dict[str, str], *,
			 max_retries: int) -> Response | None:
	attempt = 0
	global last_api_call
	backoff = REQUIRED_DELAY_BETWEEN_API_CALLS
	while True:
		time_since_last_call = time.time() - last_api_call
		if time_since_last_call < REQUIRED_DELAY_BETWEEN_API_CALLS:
			time.sleep(REQUIRED_DELAY_BETWEEN_API_CALLS - time_since_last_call)
		try:
			last_api_call = time.time()
			resp = requests.get(url, params=params, headers=headers, timeout=60)
		except requests.RequestException as e:
			attempt += 1
			if attempt > max_retries:
				raise
			time.sleep(backoff)
#			backoff = min(backoff * 2, 30)
			continue
		print(f"HTTP {resp.status_code} {resp.reason} {url} {params}")
		if resp.status_code == 200:
			return resp
		if resp.status_code in (429, 500, 502, 503, 504):
			attempt += 1
			if attempt > max_retries:
				resp.raise_for_status()
			# Respect Retry-After if provided
			ra = resp.headers.get("Retry-After")
			if ra:
				try:
					delay = float(ra)
				except ValueError:
					delay = backoff
			else:
				delay = backoff
			time.sleep(delay)
			backoff = min(backoff * 2, 60)
			continue
		# Other errors: raise immediately
		resp.raise_for_status()


def load_completed_symbols(path: Path) -> set:
	if not path.exists():
		return set()
	with path.open("r", encoding="utf-8") as f:
		return {line.strip() for line in f if line.strip()}


def append_completed_symbol(path: Path, symbol: str) -> None:
	with path.open("a", encoding="utf-8") as f:
		f.write(symbol + "\n")


def append_checkpoint(path: Path, ticker: str, day: str, reason: str, kind: Optional[str] = None) -> None:
	with path.open("a", encoding="utf-8") as f:
		obj = {
			"ticker": ticker,
			"date": day,
			"reason": reason,
			"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
		}
		if kind:
			obj["kind"] = kind
		f.write(json.dumps(obj) + "\n")


def read_checkpoints(path: Path) -> List[Dict[str, str]]:
	if not path.exists():
		return []
	items = []
	with path.open("r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
				if all(k in obj for k in ("ticker", "date")):
					items.append(obj)
			except json.JSONDecodeError:
				continue
	return items


def remove_checkpoint(path: Path, ticker: str, day: str) -> None:
	if not path.exists():
		return
	lines = path.read_text(encoding="utf-8").splitlines()
	new_lines = []
	removed = False
	for ln in lines:
		try:
			obj = json.loads(ln)
		except json.JSONDecodeError:
			new_lines.append(ln)
			continue
		if obj.get("ticker") == ticker and obj.get("date") == day and not removed:
			removed = True
			continue
		new_lines.append(ln)
	path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")


def _tickers_filenames(base_dir: Path, date: Optional[str] = None) -> Tuple[Path, Path]:
	"""Return (tickers_path, tickers_full_path) with optional date suffix.

	Examples:
	- Without date: data/tickers.txt, data/tickers_full.txt
	- With date:    data/tickers-YYYY-MM-DD.txt, data/tickers_full-YYYY-MM-DD.txt
	"""
	if date:
		return (
			base_dir / f"tickers-{date}.txt",
			base_dir / f"tickers_full-{date}.txt",
		)
	return (base_dir / TICKERS_FILE, base_dir / TICKERS_FULL_FILE)


def _write_tickers_files(base_dir: Path, tickers: List[str], full_results: List[Dict], date: Optional[str] = None) -> Tuple[Path, Path]:
	tickers_path, tickers_full_path = _tickers_filenames(base_dir, date)
	ensure_dir(base_dir)
	# Symbols only
	with tickers_path.open("w", encoding="utf-8") as f:
		for t in tickers:
			f.write(t + "\n")
	# Full payload, NDJSON
	with tickers_full_path.open("w", encoding="utf-8") as ff:
		for obj in full_results:
			ff.write(json.dumps(obj))
			ff.write("\n")
	return tickers_path, tickers_full_path


def discover_tickers(api_key: str, market: str, active: bool, exchanges: List[str], *,
					 max_retries: int, date: Optional[str] = None, output_dir: Optional[Path] = None) -> Tuple[List[str], List[Dict]]:
	headers = {"Authorization": f"Bearer {api_key}"}
	params = {
		"market": market,
#		"exchange": "XNAS",
		"active": str(active).lower(),
		"limit": 1000,
		# We'll filter exchanges by primary_exchange param when supported; otherwise post-filter.
	}
	# If a date is provided, include it to fetch a point-in-time universe
	if date:
		params["date"] = date
	results: List[Dict] = []
	url = TICKERS_ENDPOINT
	while True:
		resp = http_get(url, params, headers, max_retries=max_retries)
		data = resp.json()
		res = data.get("results", [])
		# Ensure list of dicts
		if isinstance(res, list):
			results.extend(res)
		elif isinstance(res, dict):
			results.append(res)
		next_url = data.get("next_url")
		if not next_url:
			break
		# next_url may already contain the api key auth, but we use header auth; reset params
		url = next_url
		params = {}
	# Post-filter by primary_exchange if field exists
	tickers: List[str] = []
	ex_set = set(exchanges)
	for item in results:
		# primary_exchange can be None for some assets
		pe = item.get("primary_exchange")
		if ex_set and pe and pe not in ex_set:
			continue
		sym = item.get("ticker") or item.get("symbol")
		if sym:
			tickers.append(sym)
	# If requested, write discovered files with optional date suffix
	if output_dir is not None:
		try:
			_write_tickers_files(output_dir, tickers, results, date=date)
		except Exception as e:
			# Do not fail discovery if write fails; just log
			print(f"WARN: failed to write tickers files: {e}")
	return tickers, results


def discover_consistent_tickers(
	api_key: str,
	market: str,
	active: bool,
	exchanges: List[str],
	*,
	start_date: str,
	end_date: Optional[str] = None,
	max_retries: int = 5,
	output_dir: Optional[Path] = None,
) -> Tuple[List[str], List[Dict]]:
	"""Discover tickers that are present on both start_date and end_date.

	- Calls discover_tickers twice (with date=start_date and date=end_date or start_date if None).
	- If output_dir is provided, each discovery also writes date-suffixed files.
	- The intersection of symbols is computed; those symbols are written to the standard
	  tickers.txt and tickers_full.txt (without date suffix) in output_dir.

	Returns the final (symbols, full_objects) where full_objects are taken from start_date set.
	"""
	# First discovery at start_date
	tickers_start, full_start = discover_tickers(
		api_key,
		market,
		active,
		exchanges,
		max_retries=max_retries,
		date=start_date,
		output_dir=output_dir,
	)

	# Second discovery at end_date
	tickers_end, full_end = discover_tickers(
		api_key,
		market,
		active,
		exchanges,
		max_retries=max_retries,
		date=end_date,
		output_dir=output_dir,
	)

	start_set = set(tickers_start)
	end_set = set(tickers_end)
	inter = sorted(start_set & end_set)

	# Map for quick lookup from start_date full results
	by_symbol = {}
	for obj in full_start:
		sym = obj.get("ticker") or obj.get("symbol")
		if sym in start_set:
			by_symbol[sym] = obj

	final_full = [by_symbol[s] for s in inter if s in by_symbol]

	# Write consolidated files without date suffix if output_dir is provided
	if output_dir is not None:
		_write_tickers_files(output_dir, inter, final_full, date=None)

	return inter, final_full


def ensure_dir(path: Path) -> None:
	path.mkdir(parents=True, exist_ok=True)


def day_file_path(base_dir: Path, ticker: str, day: datetime) -> Path:
	return base_dir / "trades" / ticker / f"{day.year:04d}" / f"{iso_date(day)}.ndjson"


def market_summary_file_path(base_dir: Path, day: datetime) -> Path:
	return base_dir / "market_summary" / f"{day.year:04d}" / f"{iso_date(day)}.ndjson"


def ticker_daily_file_path(base_dir: Path, ticker: str, day: datetime) -> Path:
	return base_dir / "ticker_daily" / ticker / f"{day.year:04d}" / f"{iso_date(day)}.ndjson"


def fetch_overview_day(api_key: str, ticker: str, day: datetime, out_file: Path, *,
					   max_retries: int) -> int:
	headers = {"Authorization": f"Bearer {api_key}"}
	# Overview is a point-in-time snapshot for a given date
	url = TICKER_DETAILS_ENDPOINT_TMPL.format(ticker=ticker)
	params = {
		"date": iso_date(day),
	}

	total = 0
	# Create dir & open file for writing (overwrite each run for that day)
	ensure_dir(out_file.parent)
	with out_file.open("w", encoding="utf-8") as f:
		resp = http_get(url, params, headers, max_retries=max_retries)
		data = resp.json()
		results = data.get("results")
		if isinstance(results, list):
			for obj in results:
				f.write(json.dumps(obj))
				f.write("\n")
				total += 1
		elif isinstance(results, dict):
			f.write(json.dumps(results))
			f.write("\n")
			total = 1
		else:
			# If the API shape differs, write the raw payload for diagnostics
			f.write(json.dumps(data))
			f.write("\n")
			total = 1
	return total


def fetch_market_summary_day(api_key: str, day: datetime, out_file: Path, *, max_retries: int) -> int:
	"""Fetch grouped daily market summary for all US stocks for a date and write NDJSON.

	Returns the number of lines written. If the total number of transactions for the day is 0
	(i.e., sum of 'n' across results is 0 or results is empty), the file is NOT written and 0 is returned.
	"""
	headers = {"Authorization": f"Bearer {api_key}"}
	url = MARKET_GROUPED_ENDPOINT_TMPL.format(date=iso_date(day))
	params = {
		"adjusted": "true",
		# "include_otc": "false",  # uncomment if supported by backend
	}
	resp = http_get(url, params, headers, max_retries=max_retries)
	data = resp.json()
	results = data.get("results") if isinstance(data, dict) else None

	# Determine total transactions across all symbols for the day
	total_tx = 0
	if isinstance(results, list):
		for obj in results:
			try:
				# Polygon grouped daily commonly uses 'n' as number of transactions
				total_tx += int(obj.get("n", 0))
			except Exception:
				# If field missing or not an int, treat as 0
				pass

	# If no results or total transactions is 0, do NOT write the file
	if not results or total_tx == 0:
		return 0

	# Otherwise, write each result as NDJSON
	ensure_dir(out_file.parent)
	total = 0
	with out_file.open("w", encoding="utf-8") as f:
		for obj in results:
			f.write(json.dumps(obj))
			f.write("\n")
			total += 1
	return total


def fetch_ticker_daily_summary_day(api_key: str, ticker: str, day: datetime, out_file: Path, *, max_retries: int) -> int:
	"""Fetch per-ticker daily summary (open/close) and write a single NDJSON line."""
	headers = {"Authorization": f"Bearer {api_key}"}
	url = OPEN_CLOSE_ENDPOINT_TMPL.format(ticker=ticker, date=iso_date(day))
	params = {
		"adjusted": "true",
	}
	ensure_dir(out_file.parent)
	with out_file.open("w", encoding="utf-8") as f:
		resp = http_get(url, params, headers, max_retries=max_retries)
		data = resp.json()
		f.write(json.dumps(data))
		f.write("\n")
	return 1


def compute_default_dates(start_date_arg: Optional[str], end_date_arg: Optional[str], years_back: int = 2) -> Tuple[datetime, datetime]:
	now_utc = datetime.now(timezone.utc)
	# Default end_date is today (UTC) start-of-day
	end_date = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
	# Default start_date is years_back years prior
	if years_back < 0:
		years_back = 0
	start_date = end_date - timedelta(days=365 * years_back)
	if start_date_arg:
		start_date = datetime.fromisoformat(start_date_arg).replace(tzinfo=timezone.utc)
	if end_date_arg:
		end_date = datetime.fromisoformat(end_date_arg).replace(tzinfo=timezone.utc)
	return start_date, end_date


def main() -> int:
	args = parse_args()
	api_key = args.api_key
	if not api_key:
		print("Error: API key is required. Provide --api-key or POLYGON_API_KEY env.", file=sys.stderr)
		return 1

	output_dir = Path(args.output_dir)
	ensure_dir(output_dir)

	completed_path = output_dir / COMPLETED_FILE
	checkpoints_path = output_dir / CHECKPOINTS_FILE

	completed = load_completed_symbols(completed_path)

	start_date, end_date = compute_default_dates(args.start_date, args.end_date, args.years_back)
	print(f"Processing date range [${iso_date(start_date)} .. ${iso_date(end_date)}) UTC")

	# Retry checkpoints first if requested
	if args.retry_checkpoints and not args.no_retry_checkpoints:
		cps = read_checkpoints(checkpoints_path)
		if cps:
			print(f"Retrying {len(cps)} checkpointed day(s)...")
		for cp in cps:
			ticker = cp.get("ticker")
			day_str = cp.get("date")
			kind = cp.get("kind")
			try:
				day = datetime.fromisoformat(day_str).replace(tzinfo=timezone.utc)
			except Exception:
				print(f"Skipping invalid checkpoint date: {cp}")
				continue
			try:
				if kind == "market_summary" or ticker == "__MARKET__":
					out_file = market_summary_file_path(output_dir, day)
					count = fetch_market_summary_day(api_key, day, out_file, max_retries=args.max_retries)
					print(f"OK (checkpoint) MARKET {iso_date(day)}: grouped summary saved ({count} line(s))")
				elif kind == "ticker_daily":
					out_file = ticker_daily_file_path(output_dir, ticker, day)
					count = fetch_ticker_daily_summary_day(api_key, ticker, day, out_file, max_retries=args.max_retries)
					print(f"OK (checkpoint) {ticker} {iso_date(day)}: daily summary saved ({count} line(s))")
				else:
					out_file = day_file_path(output_dir, ticker, day)
					count = fetch_overview_day(api_key, ticker, day, out_file, max_retries=args.max_retries)
					print(f"OK (checkpoint) {ticker} {iso_date(day)}: overview saved ({count} object(s))")
				if ticker:
					remove_checkpoint(checkpoints_path, ticker, day_str)
			except Exception as e:
				print(f"FAIL (checkpoint) {ticker} {iso_date(day)} [{kind or 'overview'}]: {e}")
				# leave checkpoint

	# Load or discover tickers
	tickers_path = output_dir / TICKERS_FILE
	if tickers_path.exists():
		with tickers_path.open("r", encoding="utf-8") as f:
			tickers = [ln.strip() for ln in f if ln.strip()]
		print(f"Loaded {len(tickers)} tickers from {tickers_path}")
	else:
		print("Discovering tickers...")
		exchanges = [ex.strip() for ex in args.exchanges.split(",") if ex.strip()]
		# tickers, full_results = discover_tickers(api_key, args.market, args.active, exchanges, max_retries=args.max_retries)
		tickers, full_results = discover_consistent_tickers(api_key, args.market, args.active, exchanges,start_date=iso_date(start_date), max_retries=args.max_retries)
		if not tickers:
			print("No tickers discovered.")
			return 1
		print(f"Discovered {len(tickers)} tickers; saving to {tickers_path}")
		# Save to tickers.txt (symbols only)
		with tickers_path.open("w", encoding="utf-8") as f:
			for t in tickers:
				f.write(t + "\n")
		# Save full discovery payload to tickers_full.txt (NDJSON)
		tickers_full_path = output_dir / TICKERS_FULL_FILE
		with tickers_full_path.open("w", encoding="utf-8") as ff:
			for obj in full_results:
				ff.write(json.dumps(obj))
				ff.write("\n")
	# Apply max-tickers and skip completed
	selected = []
	for t in tickers:
		if t in completed:
			continue
		selected.append(t)
#		if len(selected) >= args.max_tickers:
#			break

	if not selected:
		print("All selected tickers already completed or none available.")
		return 0

	print(f"Selected {len(selected)} tickers: {', '.join(selected)}")

	# For each ticker, iterate days
	for day in daterange(start_date, end_date):
		print(f"\n=== Date {day} ===")
		all_ok = True

		# Daily market summary (once per day)
		# if args.fetch_market_summary:
		ms_out = market_summary_file_path(output_dir, day)
		if not (ms_out.exists() and ms_out.stat().st_size > 0):
			try:
				ms_count = fetch_market_summary_day(api_key, day, ms_out, max_retries=args.max_retries)
				print(f"OK MARKET {iso_date(day)}: grouped summary saved ({ms_count} line(s))")
			except Exception as e:
				print(f"FAIL MARKET {iso_date(day)}: {e}")
				append_checkpoint(checkpoints_path, "__MARKET__", day.isoformat(), str(e), kind="market_summary")
				all_ok = False

		# for ticker in selected:
		# 	print(f"\n=== Ticker {ticker} ===")
			# Per-ticker overview
			# out_file = day_file_path(output_dir, ticker, day)
			# # Resume: skip if file exists and non-empty
			# if not (out_file.exists() and out_file.stat().st_size > 0):
			# 	try:
			# 		count = fetch_overview_day(api_key, ticker, day, out_file, max_retries=args.max_retries)
			# 		print(f"OK {ticker} {iso_date(day)}: overview saved ({count} object(s))")
			# 		# If there were 0 results, file may be empty; that's fine
			# 	except Exception as e:
			# 		print(f"FAIL {ticker} {iso_date(day)} [overview]: {e}")
			# 		append_checkpoint(checkpoints_path, ticker, day.isoformat(), str(e), kind="overview")
			# 		all_ok = False
			# 		# small delay to avoid hammering if continuous failures
			# 		time.sleep(1.0)

			# Per-ticker daily summary (open/close)
		# 	if args.fetch_ticker_daily:
		# 		td_out = ticker_daily_file_path(output_dir, ticker, day)
		# 		if not (td_out.exists() and td_out.stat().st_size > 0):
		# 			try:
		# 				td_count = fetch_ticker_daily_summary_day(api_key, ticker, day, td_out, max_retries=args.max_retries)
		# 				print(f"OK {ticker} {iso_date(day)}: daily summary saved ({td_count} line(s))")
		# 			except Exception as e:
		# 				print(f"FAIL {ticker} {iso_date(day)} [daily]: {e}")
		# 				append_checkpoint(checkpoints_path, ticker, day.isoformat(), str(e), kind="ticker_daily")
		# 				all_ok = False
		#
		# if all_ok:
		# 	append_completed_symbol(completed_path, ticker)
		# 	print(f"Completed ticker {ticker}")
		# else:
		# 	print(f"Ticker {ticker} completed with some failures; see checkpoints.txt")

	print("Done.")
	return 0


if __name__ == "__main__":
	sys.exit(main())



def fetch_daily_market_summary(api_key: str, day: datetime, *, adjusted: bool = True, max_retries: int = 5) -> List[Dict]:
	"""Return the grouped daily market summary (US stocks) for a given UTC date.

	Parameters:
	- api_key: Polygon.io API key
	- day: datetime date (UTC or naive interpreted as UTC)
	- adjusted: whether to request adjusted data
	- max_retries: number of retries for transient errors

	Returns a list of result objects (one per symbol) or an empty list if none.
	"""
	headers = {"Authorization": f"Bearer {api_key}"}
	url = MARKET_GROUPED_ENDPOINT_TMPL.format(date=iso_date(day))
	params = {
		"adjusted": "true" if adjusted else "false",
	}
	resp = http_get(url, params, headers, max_retries=max_retries)
	data = resp.json()
	results = data.get("results") if isinstance(data, dict) else None
	if isinstance(results, list):
		return results
	# Fallback: return the payload as a single-item list if it's a dict, else empty
	if isinstance(data, dict) and data:
		return [data]
	return []
