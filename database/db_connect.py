from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

import mysql
import pymysql

try:
	# Local relative import within the database package
	from .db_info import DbInfo, load_dbinfo
except Exception:  # pragma: no cover - soft dependency
	DbInfo = None  # type: ignore
	def load_dbinfo(*args, **kwargs):  # type: ignore
		return None


def _try_import_drivers(prefer: str = "auto") -> Tuple[str, mysql.connector | pymysql]:
	"""Return (driver_name, module) for an available driver.

	prefer: "auto" | "mysqlconnector" | "pymysql"
	"""
	prefer = prefer.lower()

	if prefer in ("mysqlconnector", "mysql-connector", "mysql.connector"):
		try:
			import mysql.connector as mc  # type: ignore
			return ("mysqlconnector", mc)
		except Exception as e:
			raise RuntimeError("Requested driver 'mysqlconnector' is not available: " + str(e))
	if prefer in ("pymysql",):
		try:
			import pymysql as pm  # type: ignore
			return ("pymysql", pm)
		except Exception as e:
			raise RuntimeError("Requested driver 'pymysql' is not available: " + str(e))

	# auto-detect: try mysql-connector, then PyMySQL
	try:
		import mysql.connector as mc  # type: ignore
		return ("mysqlconnector", mc)
	except Exception:
		pass

	try:
		import pymysql as pm  # type: ignore
		return ("pymysql", pm)
	except Exception:
		pass

	raise RuntimeError(
		"No MySQL driver found. Please install either 'mysql-connector-python' or 'pymysql'."
	)


def get_connection(
	db_info: Optional["DbInfo"] = None,
	dbinfo_file: str = "dbinfo.id",
):
	"""Create and return a live MySQL connection using a DbInfo object or file.

	Only two inputs are supported:
	- ``db_info``: a populated ``DbInfo`` object
	- ``dbinfo_file``: path to a config file (JSON or KEY=VALUE) defaulting to ``dbinfo.id``

	The function loads configuration from the object if provided, otherwise from
	the specified file.
	"""

	current_directory = Path(os.getcwd())
	test_db_id_file = os.path.join(current_directory, dbinfo_file)
	while not os.path.exists(test_db_id_file):
		print(f"dbinfo_file not found: {test_db_id_file} current directory: {current_directory}   parent directory: {current_directory.parent}")
		current_directory = current_directory.parent
		test_db_id_file = os.path.join(current_directory, dbinfo_file)
	os.chdir(current_directory)

	# Load configuration from object or file
	if db_info is not None and hasattr(db_info, "with_env_defaults"):
		info = db_info.with_env_defaults()
	else:
		loaded = load_dbinfo(dbinfo_file)
		if loaded is None:
			raise FileNotFoundError(f"Database info file not found or invalid: {dbinfo_file}")
		info = loaded.with_env_defaults()

	drv_name, drv_mod = _try_import_drivers(info.driver)

	if drv_name == "mysqlconnector":
		# mysql-connector-python
		conn = drv_mod.connect(
			host=info.host,
			database=info.schema or info.database,
			user=info.user,
			password=info.password,
			port=info.port,
			autocommit=True,
			buffered=True,
		)
		return conn
	elif drv_name == "pymysql":
		conn = drv_mod.connect(
			host=info.host,
			db=info.schema or info.database,
			user=info.user,
			password=info.password,
			port=info.port,
			autocommit=True,
			charset="utf8mb4",
		)
		return conn
	else:
		# Should not happen
		raise RuntimeError(f"Unsupported driver selected: {drv_name}")
