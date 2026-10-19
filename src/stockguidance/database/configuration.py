from dataclasses import dataclass, field
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.config.validation import require_positive_int, require_text
from stockguidance.config.source import DataConfiguration, load_data_configuration
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
	data_configuration_file: Path | None = None

	def __post_init__(self) -> None:
		for name in ("host", "database", "user"):
			require_text(getattr(self, name), name)
		require_positive_int(self.port, "database port")
		if self.port > 65535 or not isinstance(self.password, str) or not isinstance(self.driver, MySQLDriver):
			raise ValueError("Invalid database configuration")
		for name in ("table_names_file", "data_configuration_file"):
			if getattr(self, name) is not None and not isinstance(getattr(self, name), Path):
				raise ValueError(f"Invalid {name} configuration")

	def load_table_names(self) -> TableNames:
		return load_table_names(self.table_names_file)

	def load_data_configuration(self) -> DataConfiguration:
		if self.data_configuration_file is None:
			raise ValueError("An explicit data_configuration_file is required")
		return load_data_configuration(self.data_configuration_file)


def load_database_configuration(path: Path) -> DatabaseConfiguration:
	try:
		fields = mapping(json.loads(path.read_text(encoding="utf-8")))
		if set(fields) - {"host", "database", "schema", "user", "password", "port", "driver", "table_names_file", "data_configuration_file"}:
			raise ValueError("Unknown database configuration fields")
		database = fields.get("schema") or fields.get("database")
		if fields.get("database") and fields.get("schema") and fields["database"] != fields["schema"]:
			raise ValueError("Conflicting database names")
		host, user, password = fields.get("host", "localhost"), fields.get("user"), fields.get("password", "")
		port, driver = fields.get("port", 3306), fields.get("driver", "auto")
		table_names_file = fields.get("table_names_file")
		data_configuration_file = fields.get("data_configuration_file")
		if not all(isinstance(value, str) for value in (host, database, user, password, driver)) or not isinstance(port, int) or isinstance(port, bool):
			raise ValueError("Invalid database configuration types")
		resolved_paths: dict[str, Path | None] = {}
		for name, value in (("table_names_file", table_names_file), ("data_configuration_file", data_configuration_file)):
			if value is not None:
				if not isinstance(value, str) or not value.strip():
					raise ValueError(f"Invalid {name} configuration")
				resolved = Path(value)
				if not resolved.is_absolute():
					resolved = path.resolve().parent / resolved
				resolved_paths[name] = resolved
			else:
				resolved_paths[name] = None
		return DatabaseConfiguration(str(host), str(database), str(user), str(password), port, MySQLDriver(str(driver)), resolved_paths["table_names_file"], resolved_paths["data_configuration_file"])
	except Exception:
		raise ValueError("Cannot read the explicit database JSON configuration") from None
