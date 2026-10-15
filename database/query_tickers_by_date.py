import argparse
import pandas as pd
from pathlib import Path
from database.db_connect import get_connection
from load_database_rows_into_tensors import create_ticker_id_selection_sql
from calculate_ticker_correlations import resolve_date_id, get_available_dates

def query_valid_tickers(dbinfo_file: str, start_date_id_str: str = None, end_date_id_str: str = None, output_file: str = None):
    conn = get_connection(dbinfo_file=dbinfo_file)
    try:
        available_dates = get_available_dates(conn)
        if not available_dates:
            print("No dates found in database.")
            return

        start_date_id = resolve_date_id(start_date_id_str, available_dates) if start_date_id_str else available_dates[0]
        end_date_id = resolve_date_id(end_date_id_str, available_dates) if end_date_id_str else available_dates[-1]

        print(f"Querying valid tickers for date range: {start_date_id} to {end_date_id}")

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

        print("Fetching data from database...")
        df = pd.read_sql(query, conn)

        if df.empty:
            print("No tickers found for the given criteria.")
            return

        if output_file:
            with open(output_file, 'w') as f:
                for _, row in df.iterrows():
                    f.write(f"{row['id']}\t{row['label']}\n")
            print(f"Saved {len(df)} tickers to {output_file}")
        else:
            print(f"Found {len(df)} tickers:")
            for _, row in df.iterrows():
                print(f"{row['id']}\t{row['label']}")

    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Query the database for valid ticker symbols for a given date range.")
    parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
    parser.add_argument("--start-date-id", type=str, help="Optional start date ID (absolute, +N for index from start, -N for index from end)")
    parser.add_argument("--end-date-id", type=str, help="Optional end date ID (absolute, +N for index from start, -N for index from end)")
    parser.add_argument("--output", help="Optional output text file path")

    args = parser.parse_args()
    query_valid_tickers(args.dbinfo_file, args.start_date_id, args.end_date_id, args.output)

if __name__ == "__main__":
    main()
