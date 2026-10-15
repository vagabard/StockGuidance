import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from database.db_connect import get_connection
from load_database_rows_into_tensors import create_ticker_id_selection_sql
from trainer.util.date_bounds import DateBoundResolver, DateBoundType

def calculate_correlations(dbinfo_file: str, output_csv: str, start_date_id_str: str = None, end_date_id_str: str = None):
    resolver = DateBoundResolver(dbinfo_file=dbinfo_file)
    start_date_id = resolver.resolve_bound(start_date_id_str, DateBoundType.START) if start_date_id_str else resolver.lowest_date_id
    end_date_id = resolver.resolve_bound(end_date_id_str, DateBoundType.END) if end_date_id_str else resolver.highest_date_id

    print(f"Calculating correlations for date range: {start_date_id} to {end_date_id}")
    conn = get_connection(dbinfo_file=dbinfo_file)
    try:

        # Use existing logic to select tickers present in the range
        ticker_sql = create_ticker_id_selection_sql(
            only_use_tickers_without_missing_dates=True,
            start_date_id=start_date_id,
            end_date_id=end_date_id
        )

        query = f"""
        SELECT
            ddsd.date_id,
            ts.label AS symbol,
            ddsd.change_in_price_over_day
        FROM daily_delta_stock_data ddsd
        JOIN ticker_symbol ts ON ddsd.ticker_id = ts.id
        WHERE ddsd.ticker_id IN ({ticker_sql})
        """

        if start_date_id is not None:
            query += f" AND ddsd.date_id >= {start_date_id}"
        if end_date_id is not None:
            query += f" AND ddsd.date_id <= {end_date_id}"

        query += " ORDER BY ddsd.date_id, symbol"

        print("Fetching data from database...")
        df = pd.read_sql(query, conn)

        if df.empty:
            print("No data found for the given criteria.")
            return

        actual_start = df['date_id'].min()
        actual_end = df['date_id'].max()
        print(f"Actual date range in data: {actual_start} to {actual_end}")

        print(f"Pivotting data for {len(df['symbol'].unique())} tickers and {len(df['date_id'].unique())} dates...")
        # Pivot: Rows = Date, Columns = Ticker, Values = Price Change
        pivot_df = df.pivot(index='date_id', columns='symbol', values='change_in_price_over_day')

        # Handle missing values if any (though ticker_sql tries to avoid them)
        pivot_df = pivot_df.fillna(0)

        print("Calculating correlation matrix...")
        corr_matrix = pivot_df.corr()

        # Update output filename to include date range
        output_path = Path(output_csv)
        new_filename = f"{output_path.stem}_{actual_start}_{actual_end}{output_path.suffix}"
        final_output_path = output_path.with_name(new_filename)

        print(f"Saving correlation matrix to {final_output_path}...")
        if str(final_output_path).endswith('.parquet'):
            corr_matrix.to_parquet(final_output_path)
        else:
            corr_matrix.to_csv(final_output_path)
        print(f"Done. Correlation matrix for {actual_start} to {actual_end} saved to {final_output_path}.")

    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Calculate ticker correlations based on daily price changes.")
    parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
    parser.add_argument("--output", default="ticker_correlations.parquet", help="Output path (ends in .csv or .parquet)")
    parser.add_argument("--start-date-id", type=str, help="Optional start date ID (absolute, +N for index from start, -N for index from end)")
    parser.add_argument("--end-date-id", type=str, help="Optional end date ID (absolute, +N for index from start, -N for index from end)")

    args = parser.parse_args()
    calculate_correlations(args.dbinfo_file, args.output, args.start_date_id, args.end_date_id)

if __name__ == "__main__":
    main()
