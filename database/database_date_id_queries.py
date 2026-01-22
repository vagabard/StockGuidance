"""Simple helper script to query a range of ``date_id`` values from MySQL.

The primary goal of this script is to provide a small, convenient CLI for
loading the distinct ``date_id`` values that exist in a given inclusive
range, using the existing ``db_connect.get_connection`` helper.

By default we read from the ``raw_stock_data`` table, since that is where
trading dates are normally referenced elsewhere in this project.

Example
-------

.. code-block:: bash

   python database_date_id_queries.py \
	   --user "$MYSQL_USER" \
	   --password "$MYSQL_PASSWORD" \
	   --start-date-id 1000 \
	   --end-date-id 1200

This will print one ``date_id`` per line for all dates in the given
inclusive range that are present in ``raw_stock_data``.
"""

from __future__ import annotations

import argparse
from typing import Optional, List

from database.db_connect import get_connection


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:

	p = argparse.ArgumentParser(
		description="Load distinct date_id values from the database for an inclusive range.",
	)

	# Database config is provided only via a dbinfo file
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")

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




def _build_sql_for_counting_range(table: str, min_date_id:int, max_date_id:int) -> str:
	"""Return the parametrised SQL statement for the given table name.

	We keep the table name separate from the parameters to avoid accidental
	SQL injection via the numeric range values. The table name itself is not
	parametrised; this helper only accepts a simple identifier and performs a
	very small validation to keep things safe.
	"""

	# Very small guard: disallow dangerous characters in table name so that
	# users cannot accidentally inject arbitrary SQL.
	if not table.replace("_", "").isalnum():
		raise ValueError(f"Invalid table name: {table}")
	return f"""SELECT id FROM {table} WHERE id >= {min_date_id} AND id <= {max_date_id} order by id ASC;"""


def _build_sql(table: str, feature_vector_day_size_buffer:Optional[int]=None) -> str:
	"""Return the parametrised SQL statement for the given table name.

	We keep the table name separate from the parameters to avoid accidental
	SQL injection via the numeric range values. The table name itself is not
	parametrised; this helper only accepts a simple identifier and performs a
	very small validation to keep things safe.
	"""

	# Very small guard: disallow dangerous characters in table name so that
	# users cannot accidentally inject arbitrary SQL.
	if not table.replace("_", "").isalnum():
		raise ValueError(f"Invalid table name: {table}")
	if feature_vector_day_size_buffer is None:
		offset_sql = ""
	else:
		offset_sql = f"OFFSET {feature_vector_day_size_buffer}"
	#return f"SELECT min(date_id),max(date_id) FROM {table} "
	return f"""SELECT (SELECT MAX(id) FROM {table}) AS max_value,
    (SELECT id FROM {table} ORDER BY id ASC LIMIT 1 {offset_sql}) AS min_value;
    """

def _build_offset_sql(table: str,base_value:int, offset:int) -> str:
	"""Return the parametrised SQL statement for the given table name.

	We keep the table name separate from the parameters to avoid accidental
	SQL injection via the numeric range values. The table name itself is not
	parametrised; this helper only accepts a simple identifier and performs a
	very small validation to keep things safe.
	"""

	# Very small guard: disallow dangerous characters in table name so that
	# users cannot accidentally inject arbitrary SQL.
	if not table.replace("_", "").isalnum():
		raise ValueError(f"Invalid table name: {table}")

	sql_to_return =  f"""SELECT id FROM {table} WHERE id"""
	if offset>0:
		sql_to_return += f""" > {base_value} ORDER BY id ASC LIMIT 1 OFFSET {offset}"""
	else:
		sql_to_return += f""" < {base_value} ORDER BY id DESC LIMIT 1 OFFSET {-offset}"""
	sql_to_return +=""";
    """
	return sql_to_return


def get_date_ids_in_range(
    *,
    dbinfo_file: str = "dbinfo.id",
	minimum_date_id:int, maximum_date_id:int) -> list[int]:
	"""Query and return the distinct ``date_id`` values in the inclusive range.

	The return value is a sorted list of integer ``date_id`` values present in
	the chosen table.
	"""
	table: str = "transaction_dates"
	sql = _build_sql_for_counting_range (table, minimum_date_id, maximum_date_id)

	#print(sql)

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(sql)
		rows = cur.fetchall()
		all_ids:List[int] = []
		while(rows):
			row = rows.pop(0)
			all_ids.append(
				int(row[0])
			)
		return all_ids
	finally:
		try:
			conn.close()
		except Exception:
			pass


def load_date_id_range(
    *,
    dbinfo_file: str = "dbinfo.id",
	feature_vector_day_size_buffer:Optional[int]=None) -> tuple[int, int]:
	"""Query and return the distinct ``date_id`` values in the inclusive range.

	The return value is a sorted list of integer ``date_id`` values present in
	the chosen table.
	"""
	table: str = "transaction_dates"
	sql = _build_sql(table, feature_vector_day_size_buffer)

	#print(sql)

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(sql)
		rows = cur.fetchall()
		min_date_id = int(rows[0][1])
		max_date_id = int(rows[0][0])
		return min_date_id, max_date_id
	finally:
		try:
			conn.close()
		except Exception:
			pass


def get_offset_date_id(
    *,
    dbinfo_file: str = "dbinfo.id", base_value:int,
	offset:int) -> int:
	"""Query and return the distinct ``date_id`` values in the inclusive range.

	The return value is a sorted list of integer ``date_id`` values present in
	the chosen table.
	"""
	table: str = "transaction_dates"
	sql = _build_offset_sql(table, base_value, offset)
	print("base value ", base_value, "  offset ", offset,"\n" ,  sql)

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(sql)
		rows = cur.fetchall()
		return int(rows[0][0])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def main(argv: Optional[list[str]] = None) -> None:
	args = parse_args(argv)

	date_ids = load_date_id_range(dbinfo_file=args.dbinfo_file)

	offset_date_id = get_offset_date_id(dbinfo_file=args.dbinfo_file, base_value=16022, offset=5)
	offset_date_id2 = get_offset_date_id(dbinfo_file=args.dbinfo_file, base_value=16022, offset=-3)
	print(f"offset_date_id: {offset_date_id}, offset_date_id2: {offset_date_id2}")

	if not date_ids:
		print("No date_id values found in the specified range.")
	else:
		print(f"Found range of distinct date_id values in range :")
		for did in date_ids:
			print(did)

'16022'

if __name__ == "__main__":  # pragma: no cover - simple CLI entry point
	main()
