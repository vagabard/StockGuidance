from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from stockguidance.database.configuration import MySQLDriver, load_database_configuration
from stockguidance.database.write_connections import WriteConnection


class _Connect(Protocol):
	def __call__(self, **parameters: object) -> WriteConnection: ...


class MySQLConnectionFactory:
	def __call__(self, database_config: Path) -> WriteConnection:
		configuration = load_database_configuration(database_config)
		drivers = (MySQLDriver.CONNECTOR, MySQLDriver.PYMYSQL) if configuration.driver is MySQLDriver.AUTO else (configuration.driver,)
		for driver in drivers:
			try:
				module = import_module("mysql.connector" if driver is MySQLDriver.CONNECTOR else "pymysql")
			except ImportError:
				continue
			parameters: dict[str, object] = {"host": configuration.host, "database": configuration.database, "user": configuration.user, "password": configuration.password, "port": configuration.port, "autocommit": False}
			parameters.update({"buffered": True} if driver is MySQLDriver.CONNECTOR else {"charset": "utf8mb4"})
			try:
				return cast(_Connect, module.connect)(**parameters)
			except Exception:
				raise RuntimeError("Configured MySQL connection failed") from None
		raise RuntimeError("Install mysql-connector-python or pymysql in the configured environment")
