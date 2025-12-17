"""Helper script to get the latest ``date_id`` from ``daily_delta_stock_data``.

This small utility connects to the database using :func:`db_connect.get_connection`,
finds the most recent ``date_id`` present in ``daily_delta_stock_data``, looks up
the corresponding ``transaction_date`` in ``transaction_dates``, and returns/
prints both values.

Typical usage from the command line::

    python load_latest_daily_delta_date.py \
        --user "$MYSQL_USER" \
        --password "$MYSQL_PASSWORD"

The script will print a single line of the form::

    latest date_id: 12345, transaction_date: 2024-05-17

If no rows are present in ``daily_delta_stock_data``, it prints a message and
exits with code 1.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional, Tuple

from database.db_connect import get_connection


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the helper script."""

    p = argparse.ArgumentParser(
        description=(
            "Load the most recent date_id from daily_delta_stock_data and its "
            "corresponding transaction_date from transaction_dates."
        ),
    )

    # Database configuration via dbinfo file only
    p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")

    return p.parse_args(argv)


def get_latest_raw_stock_data(*, dbinfo_file: str = "dbinfo.id") -> Optional[Tuple[int, object]]:
    """Return the latest ``(date_id, transaction_date)`` pair, or ``None``.

    The function queries ``daily_delta_stock_data`` for the maximum ``date_id``
    and joins it with ``transaction_dates`` to obtain the human-readable date.
    ``transaction_date`` is returned as whatever Python type the driver uses
    for DATE columns (typically :class:`datetime.date`).
    """

    sql = (
        "SELECT d.date_id, t.transaction_date "
        "FROM raw_stock_data d "
        "JOIN transaction_dates t ON d.date_id = t.id "
        "ORDER BY d.date_id DESC "
        "LIMIT 1"
    )

    conn = get_connection(dbinfo_file=dbinfo_file)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        if not row:
            return None
        date_id = int(row[0])
        transaction_date = row[1]
        return date_id, transaction_date
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    result = get_latest_raw_stock_data(dbinfo_file=args.dbinfo_file)

    if result is None:
        print("No rows found in daily_delta_stock_data.")
        return 1

    date_id, transaction_date = result
    print(f"{date_id}, {transaction_date}")
    return 0


if __name__ == "__main__":  # pragma: no cover - simple CLI entry point
    raise SystemExit(main())
