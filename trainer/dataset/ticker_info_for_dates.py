from __future__ import annotations
from datetime import date
from typing import List, Tuple, Optional
from database.db_connect import get_connection

class TickerInfoForDates:
    """
    Given a start and end date, creates a list of ticker labels and ticker ids
    for the time, and a position value.
    The ticker ids are sorted in order, then assigned numbers 0 through number of tickers - 1.
    """

    def __init__(self, start_date: date, end_date: date, dbinfo_file: str = "dbinfo.id"):
        self.start_date = start_date
        self.end_date = end_date
        self.dbinfo_file = dbinfo_file
        self.ticker_labels: List[str] = []
        self.ticker_ids: List[int] = []
        self.ticker_positions: List[int] = []
        self._compile_list()

    def _compile_list(self):
        conn = get_connection(dbinfo_file=self.dbinfo_file)
        try:
            cursor = conn.cursor()

            # Query all ticker_ids that show up from the start date to the end date
            # We need to join with transaction_dates to filter by date
            sql = """
                SELECT DISTINCT r.ticker_id, s.label
                FROM raw_stock_data r
                JOIN transaction_dates t ON r.date_id = t.id
                JOIN ticker_symbol s ON r.ticker_id = s.id
                WHERE t.transaction_date >= %s AND t.transaction_date <= %s
                ORDER BY r.ticker_id ASC
            """
            cursor.execute(sql, (self.start_date, self.end_date))
            rows = cursor.fetchall()

            for i, (ticker_id, label) in enumerate(rows):
                self.ticker_ids.append(ticker_id)
                self.ticker_labels.append(label)
                self.ticker_positions.append(i)

        finally:
            conn.close()

    def get_ticker_list(self) -> List[Tuple[str, int, int]]:
        """Returns a list of (label, ticker_id, position)."""
        return list(zip(self.ticker_labels, self.ticker_ids, self.ticker_positions))
