import argparse
import pandas as pd
from pathlib import Path
from typing import List, Tuple
from database.db_connect import get_connection
from load_database_rows_into_tensors import create_ticker_id_selection_sql
from trainer.util.date_bounds import DateBoundResolver, DateBoundType

def query_valid_tickers(dbinfo_file: str, start_date_id_str: str = None, end_date_id_str: str = None) -> List[Tuple[int, str]]:
	resolver = DateBoundResolver(dbinfo_file=dbinfo_file)
	start_date_id = resolver.resolve_bound(start_date_id_str, DateBoundType.START) if start_date_id_str else resolver.lowest_date_id
	end_date_id = resolver.resolve_bound(end_date_id_str, DateBoundType.END) if end_date_id_str else resolver.highest_date_id

	print(f"Querying tickers for date range: {start_date_id} to {end_date_id}")
	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		# Use existing logic to select tickers present in the range
		ticker_sql = create_ticker_id_selection_sql(
			only_use_tickers_without_missing_dates=True,
			start_date_id=start_date_id,
			end_date_id=end_date_id
		)

		query = f"""
		SELECT id, label
		FROM ticker_symbol
		WHERE id IN ({ticker_sql})
		ORDER BY id
		"""

		df = pd.read_sql(query, conn)

		if df.empty:
			return []

		ticker_list = list(df.itertuples(index=False, name=None))
		return ticker_list

	finally:
		conn.close()

def display_tickers(tickers: List[Tuple[int, str]]):
	for ticker_id, label in tickers:
		print(f"{ticker_id}\t{label}")

def main():
	parser = argparse.ArgumentParser(description="Query the database for valid ticker symbols for a given date range.")
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
	parser.add_argument("--start-date-id", type=str, help="Optional start date ID (absolute, +N for index from start, -N for index from end)")
	parser.add_argument("--end-date-id", type=str, help="Optional end date ID (absolute, +N for index from start, -N for index from end)")

	args = parser.parse_args()
	tickers = query_valid_tickers(args.dbinfo_file, args.start_date_id, args.end_date_id)
	display_tickers(tickers)

if __name__ == "__main__":
	main()
