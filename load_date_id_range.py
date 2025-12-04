"""Simple helper script to query a range of ``date_id`` values from MySQL.

The primary goal of this script is to provide a small, convenient CLI for
loading the distinct ``date_id`` values that exist in a given inclusive
range, using the existing ``db_connect.get_connection`` helper.

By default we read from the ``raw_stock_data`` table, since that is where
trading dates are normally referenced elsewhere in this project.

Example
-------

.. code-block:: bash

   python load_date_id_range.py \
	   --user "$MYSQL_USER" \
	   --password "$MYSQL_PASSWORD" \
	   --start-date-id 1000 \
	   --end-date-id 1200

This will print one ``date_id`` per line for all dates in the given
inclusive range that are present in ``raw_stock_data``.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional, List

from db_connect import get_connection


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:

	p = argparse.ArgumentParser(
		description="Load distinct date_id values from the database for an inclusive range.",
	)

	# Connection options mirror the style used in other loader scripts.
	p.add_argument("--host", default=os.getenv("MYSQL_HOST", "localhost"), help="MySQL host")
	p.add_argument(
		"--database",
		"--db",
		dest="database",
		default=os.getenv("MYSQL_DB", "stockforecasting"),
		help="Database/schema name",
	)
	p.add_argument("--user", default=os.getenv("MYSQL_USER"), help="MySQL user (default: from MYSQL_USER env)")
	p.add_argument(
		"--password",
		default=os.getenv("MYSQL_PASSWORD"),
		help="MySQL password (default: from MYSQL_PASSWORD env)",
	)
	p.add_argument(
		"--port",
		type=int,
		default=int(os.getenv("MYSQL_PORT", "3306")),
		help="MySQL port (default: 3306 or env MYSQL_PORT)",
	)
	p.add_argument(
		"--driver",
		default="auto",
		choices=["auto", "mysqlconnector", "pymysql"],
		help="DB driver to use (default: auto)",
	)

	# Query controls
	p.add_argument(
		"--table",
		default="raw_stock_data",
		help=(
			"Name of the table that contains a numeric date_id column (default: raw_stock_data). "
			"The script selects DISTINCT date_id from this table."
		),
	)

	return p.parse_args(argv)


def _build_sql(table: str) -> str:
	"""Return the parametrised SQL statement for the given table name.

	We keep the table name separate from the parameters to avoid accidental
	SQL injection via the numeric range values. The table name itself is not
	parametrised; this helper only accepts a simple identifier and performs a
	very small validation to keep things safe.
	"""

	# Very small guard: disallow dangerous characters in table name so that
	# users cannot accidentally inject arbitrary SQL.
	if not table.replace("_", "").isalnum():
		raise ValueError(f"Invalid table name: {table!r}")

	return f"SELECT min(date_id),max(date_id) FROM {table} "


def load_date_id_range(
	*,
	host: str,
	database: str,
	user: Optional[str],
	password: Optional[str],
	port: int,
	driver: str,
	table: str = "raw_stock_data",
) -> tuple[int, int]:
	"""Query and return the distinct ``date_id`` values in the inclusive range.

	The return value is a sorted list of integer ``date_id`` values present in
	the chosen table.
	"""

	sql = _build_sql(table)

	conn = get_connection(host=host, database=database, user=user, password=password, port=port, driver=driver)
	try:
		cur = conn.cursor()
		cur.execute(sql)
		rows = cur.fetchall()
		return int(rows[0][0]), int(rows[0][1])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def main(argv: Optional[list[str]] = None) -> None:
	args = parse_args(argv)

	date_ids = load_date_id_range(
		host=args.host,
		database=args.database,
		user=args.user,
		password=args.password,
		port=args.port,
		driver=args.driver,
		table=args.table,
	)

	if not date_ids:
		print("No date_id values found in the specified range.")
	else:
		print(f"Found range of distinct date_id values in range :")
		for did in date_ids:
			print(did)



if __name__ == "__main__":  # pragma: no cover - simple CLI entry point
	main()
