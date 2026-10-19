from dataclasses import dataclass, field
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.config.validation import require_positive_int, require_text
from stockguidance.database.table_names import TableNames, load_table_names
from stockguidance.market_data.identity import mapping


class MySQLDriver(StrEnum):
	AUTO = "auto"
	CONNECTOR = "mysqlconnector"
	PYMYSQL = "pymysql"


@dataclass(frozen=True)
class DatabaseConfiguration:
	host: str
	database: str
	user: str = field(repr=False)
	password: str = field(repr=False)
	port: int = 3306
	driver: MySQLDriver = MySQLDriver.AUTO
	table_names_file: Path | None = None

	def __post_init__(self) -> None:
		for name in ("host", "database", "user"):
			require_text(getattr(self, name), name)
		require_positive_int(self.port, "database port")
		if self.port > 65535 or not isinstance(self.password, str) or not isinstance(self.driver, MySQLDriver):
			raise ValueError("Invalid database configuration")
		if self.table_names_file is not None and not isinstance(self.table_names_file, Path):
			raise ValueError("Invalid table-names configuration")

	def load_table_names(self) -> TableNames:
		return load_table_names(self.table_names_file)


def load_database_configuration(path: Path) -> DatabaseConfiguration:
	try:
		fields = mapping(json.loads(path.read_text(encoding="utf-8")))
		if set(fields) - {"host", "database", "schema", "user", "password", "port", "driver", "table_names_file"}:
			raise ValueError("Unknown database configuration fields")
		database = fields.get("schema") or fields.get("database")
		if fields.get("database") and fields.get("schema") and fields["database"] != fields["schema"]:
			raise ValueError("Conflicting database names")
		host, user, password = fields.get("host", "localhost"), fields.get("user"), fields.get("password", "")
		port, driver = fields.get("port", 3306), fields.get("driver", "auto")
		table_names_file = fields.get("table_names_file")
		if not all(isinstance(value, str) for value in (host, database, user, password, driver)) or not isinstance(port, int) or isinstance(port, bool):
			raise ValueError("Invalid database configuration types")
		if table_names_file is not None:
			if not isinstance(table_names_file, str) or not table_names_file.strip():
				raise ValueError("Invalid table-names configuration")
			table_names_file = Path(table_names_file)
			if not table_names_file.is_absolute():
				table_names_file = path.resolve().parent / table_names_file
		return DatabaseConfiguration(str(host), str(database), str(user), str(password), port, MySQLDriver(str(driver)), table_names_file)
	except Exception:
		raise ValueError("Cannot read the explicit database JSON configuration") from None
