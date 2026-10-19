from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
from importlib import resources
from pathlib import Path
import re
from typing import Any, cast


class TableRole(StrEnum):
	SOURCE = "source"
	SESSIONS = "sessions"
	TICKERS = "tickers"
	PRICES = "prices"
	DELTAS = "deltas"
	IMPORTS = "imports"
	ZSCORE_SERIES = "zscore_series"
	ZSCORE_SESSIONS = "zscore_sessions"


_TABLE_ROLES = tuple(TableRole)
_TEMPLATE_NAMES = ("price", "price_delta")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FORMAT_EXPRESSION = re.compile(r"\{[^{}]*\}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("Duplicate JSON object field")
		result[key] = value
	return result


def validate_identifier(value: object, name: str = "identifier") -> str:
	if not isinstance(value, str) or not value or len(value) > 64 or _IDENTIFIER.fullmatch(value) is None:
		raise ValueError(f"Invalid {name}")
	return value


def quote_identifier(value: object, name: str = "identifier") -> str:
	return f"`{validate_identifier(value, name)}`"


def _validate_windows(windows: Iterable[int]) -> tuple[int, ...]:
	values = tuple(windows)
	if any(type(window) is not int or window < 2 for window in values):
		raise ValueError("Z-score windows must be distinct integers of at least 2")
	if len(set(values)) != len(values):
		raise ValueError("Z-score windows must be distinct integers of at least 2")
	return values


def _validate_template(value: object, name: str) -> str:
	if not isinstance(value, str) or not value or value.count("{window}") != 1 or _FORMAT_EXPRESSION.findall(value) != ["{window}"]:
		raise ValueError(f"Invalid {name} z-score template")
	if "{" in value.replace("{window}", "") or "}" in value.replace("{window}", ""):
		raise ValueError(f"Invalid {name} z-score template")
	return value


@dataclass(frozen=True)
class ResolvedZScoreTables:
	price: str
	price_delta: str


@dataclass(frozen=True)
class TableNames:
	source: str
	sessions: str
	tickers: str
	prices: str
	deltas: str
	imports: str
	zscore_series: str
	zscore_sessions: str
	price_template: str
	price_delta_template: str

	def __post_init__(self) -> None:
		for role in _TABLE_ROLES:
			validate_identifier(getattr(self, role.value), f"{role.value} table name")
		if len({getattr(self, role.value).casefold() for role in _TABLE_ROLES}) != len(_TABLE_ROLES):
			raise ValueError("Table names must be unique case-insensitively")
		_validate_template(self.price_template, "price")
		_validate_template(self.price_delta_template, "price_delta")

	def table(self, role: TableRole) -> str:
		if not isinstance(role, TableRole):
			raise ValueError("Unknown table role")
		return cast(str, getattr(self, role.value))

	def tables(self) -> dict[str, str]:
		return {role.value: getattr(self, role.value) for role in _TABLE_ROLES}

	def templates(self) -> dict[str, str]:
		return {"price": self.price_template, "price_delta": self.price_delta_template}

	def resolve_zscore_tables(self, windows: Iterable[int]) -> dict[int, ResolvedZScoreTables]:
		values = _validate_windows(windows)
		resolved: dict[int, ResolvedZScoreTables] = {}
		used = {name.casefold() for name in self.tables().values()}
		for window in values:
			price = validate_identifier(self.price_template.replace("{window}", str(window)), "price z-score table name")
			price_delta = validate_identifier(self.price_delta_template.replace("{window}", str(window)), "price_delta z-score table name")
			if price.casefold() in used or price_delta.casefold() in used or price.casefold() == price_delta.casefold():
				raise ValueError("Z-score table names collide with an existing table")
			used.update((price.casefold(), price_delta.casefold()))
			resolved[window] = ResolvedZScoreTables(price, price_delta)
		return resolved


def _mapping(value: object, name: str) -> Mapping[str, object]:
	if not isinstance(value, dict):
		raise ValueError(f"{name} must be an object")
	return value


def _load_payload(text: str) -> TableNames:
	try:
		payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
		top = _mapping(payload, "Table-names JSON")
		if set(top) != {"schema_version", "tables", "zscore_templates"}:
			raise ValueError("Table-names JSON fields are incomplete or unknown")
		if type(top["schema_version"]) is not int or top["schema_version"] != 1:
			raise ValueError("Unsupported table-names schema version")
		tables = _mapping(top["tables"], "tables")
		roles = {role.value for role in _TABLE_ROLES}
		if set(tables) != roles:
			raise ValueError("Table-names table roles are incomplete or unknown")
		templates = _mapping(top["zscore_templates"], "zscore_templates")
		if set(templates) != set(_TEMPLATE_NAMES):
			raise ValueError("Z-score templates are incomplete or unknown")
		validated_tables = tuple(validate_identifier(tables[role.value], f"{role.value} table name") for role in _TABLE_ROLES)
		return TableNames(
			source=validated_tables[0], sessions=validated_tables[1], tickers=validated_tables[2], prices=validated_tables[3],
			deltas=validated_tables[4], imports=validated_tables[5], zscore_series=validated_tables[6], zscore_sessions=validated_tables[7],
			price_template=_validate_template(templates["price"], "price"),
			price_delta_template=_validate_template(templates["price_delta"], "price_delta"),
		)
	except (TypeError, ValueError, json.JSONDecodeError):
		raise ValueError("Invalid table-names JSON") from None


def load_table_names(path: Path | None = None) -> TableNames:
	try:
		if path is None:
			text = resources.files("stockguidance.database").joinpath("table_names.json").read_text(encoding="utf-8")
		else:
			text = path.read_text(encoding="utf-8")
	except (OSError, UnicodeError):
		raise ValueError("Cannot read the selected table-names file") from None
	return _load_payload(text)
