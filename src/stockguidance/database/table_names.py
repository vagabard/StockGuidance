from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
from importlib import resources
from pathlib import Path
import re
from typing import Any, cast


class TableRole(StrEnum):
	DATA_CONFIGURATION = "source"
	SOURCE = "source"
	SCHEMA = "schema"
	SESSIONS = "sessions"
	TICKERS = "tickers"
	PRICES = "prices"
	DELTAS = "deltas"
	IMPORTS = "imports"
	DATA_STATES = "data_states"
	PENDING_UPDATES = "pending_updates"
	REPAIR_METADATA = "repair_metadata"
	ZSCORE_SERIES = "zscore_series"
	ZSCORE_SESSIONS = "zscore_sessions"
	VERIFICATIONS = "data_verifications"
	VERIFICATION_SAMPLES = "data_verification_samples"


_TABLE_ROLES = tuple(TableRole)
_TARGET_ROLES = (
	TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES,
	TableRole.DELTAS, TableRole.SCHEMA, TableRole.DATA_STATES,
	TableRole.PENDING_UPDATES, TableRole.REPAIR_METADATA,
	TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS,
)
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
	volume: str


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
	volume_template: str = "zscores_" + "volume_{window}"
	data_verifications: str = "stockguidance_data_verifications"
	data_verification_samples: str = "stockguidance_data_verification_samples"
	schema: str = "stockguidance_schema_version"
	data_states: str = "stockguidance_data_states"
	pending_updates: str = "stockguidance_pending_updates"
	repair_metadata: str = "stockguidance_repair_metadata"

	def __post_init__(self) -> None:
		for role in _TABLE_ROLES:
			field_name = "source" if role in (TableRole.DATA_CONFIGURATION, TableRole.SOURCE) else role.value
			validate_identifier(getattr(self, field_name), f"{role.value} table name")
		physical = {"source" if role in (TableRole.DATA_CONFIGURATION, TableRole.SOURCE) else role.value for role in _TABLE_ROLES}
		if len({getattr(self, field).casefold() for field in physical}) != len(physical):
			raise ValueError("Table names must be unique case-insensitively")
		_validate_template(self.price_template, "price")
		_validate_template(self.price_delta_template, "price_delta")
		_validate_template(self.volume_template, "volume")

	def table(self, role: TableRole) -> str:
		if not isinstance(role, TableRole):
			raise ValueError("Unknown table role")
		field_name = "source" if role in (TableRole.DATA_CONFIGURATION, TableRole.SOURCE) else role.value
		return cast(str, getattr(self, field_name))

	def tables(self, *, include_verification: bool = False, include_simplified: bool = False) -> dict[str, str]:
		"""Return the historical registry shape used by old manifests.

		The legacy shape is intentionally retained for migration fixtures and
		artifact identity. Target-schema code must use :meth:`target_tables`.
		"""
		legacy = (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS, TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS)
		replacement = (TableRole.SCHEMA, TableRole.DATA_STATES, TableRole.PENDING_UPDATES, TableRole.REPAIR_METADATA)
		verification = (TableRole.VERIFICATIONS, TableRole.VERIFICATION_SAMPLES) if include_verification else ()
		roles = legacy + (replacement if include_simplified else ()) + verification
		return {role.value: self.table(role) for role in roles}

	def target_tables(self) -> dict[str, str]:
		"""Return only tables created and consumed by the versioned target schema."""
		return {role.value: self.table(role) for role in _TARGET_ROLES}

	def templates(self) -> dict[str, str]:
		return {"price": self.price_template, "price_delta": self.price_delta_template}

	def preparation_templates(self) -> dict[str, str]:
		"""Return additive preparation templates without changing legacy identity payloads."""
		return {"volume": self.volume_template}

	def resolve_zscore_tables(self, windows: Iterable[int]) -> dict[int, ResolvedZScoreTables]:
		values = _validate_windows(windows)
		resolved: dict[int, ResolvedZScoreTables] = {}
		used = {name.casefold() for name in self.target_tables().values()}
		for window in values:
			price = validate_identifier(self.price_template.replace("{window}", str(window)), "price z-score table name")
			price_delta = validate_identifier(self.price_delta_template.replace("{window}", str(window)), "price_delta z-score table name")
			volume = validate_identifier(self.volume_template.replace("{window}", str(window)), "volume z-score table name")
			if len({price.casefold(), price_delta.casefold(), volume.casefold()}) != 3 or any(name.casefold() in used for name in (price, price_delta, volume)):
				raise ValueError("Z-score table names collide with an existing table")
			used.update((price.casefold(), price_delta.casefold(), volume.casefold()))
			resolved[window] = ResolvedZScoreTables(price, price_delta, volume)
		return resolved


def _mapping(value: object, name: str) -> Mapping[str, object]:
	if not isinstance(value, dict):
		raise ValueError(f"{name} must be an object")
	return value


def _load_payload(text: str) -> TableNames:
	try:
		payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
		top = _mapping(payload, "Table-names JSON")
		if set(top) not in ({"schema_version", "tables", "zscore_templates"}, {"schema_version", "tables", "zscore_templates", "preparation_templates"}):
			raise ValueError("Table-names JSON fields are incomplete or unknown")
		if type(top["schema_version"]) is not int or top["schema_version"] != 1:
			raise ValueError("Unsupported table-names schema version")
		tables = dict(_mapping(top["tables"], "tables"))
		required_roles = {role.value for role in _TABLE_ROLES if role not in (TableRole.VERIFICATIONS, TableRole.VERIFICATION_SAMPLES)}
		allowed_roles = required_roles | {TableRole.VERIFICATIONS.value, TableRole.VERIFICATION_SAMPLES.value}
		legacy_roles = {TableRole.SOURCE.value, TableRole.SESSIONS.value, TableRole.TICKERS.value, TableRole.PRICES.value, TableRole.DELTAS.value, TableRole.IMPORTS.value, TableRole.ZSCORE_SERIES.value, TableRole.ZSCORE_SESSIONS.value}
		if not legacy_roles.issubset(tables) or set(tables) - allowed_roles:
			raise ValueError("Table-names table roles are incomplete or unknown")
		templates = _mapping(top["zscore_templates"], "zscore_templates")
		if set(templates) != set(_TEMPLATE_NAMES):
			raise ValueError("Z-score templates are incomplete or unknown")
		preparation_templates = _mapping(top.get("preparation_templates", {"volume": "zscores_" + "volume_{window}"}), "preparation_templates")
		if set(preparation_templates) != {"volume"}:
			raise ValueError("Preparation templates are incomplete or unknown")
		defaults = {
			TableRole.SCHEMA.value: "stockguidance_schema_version",
			TableRole.DATA_STATES.value: "stockguidance_data_states",
			TableRole.PENDING_UPDATES.value: "stockguidance_pending_updates",
			TableRole.REPAIR_METADATA.value: "stockguidance_repair_metadata",
		}
		for role, default in defaults.items():
			if role not in tables:
				tables[role] = default
		validated_tables = {
			role.value: validate_identifier(tables[role.value], f"{role.value} table name")
			for role in _TABLE_ROLES if role not in (TableRole.VERIFICATIONS, TableRole.VERIFICATION_SAMPLES)
		}
		verification_table = validate_identifier(tables.get(TableRole.VERIFICATIONS.value, "stockguidance_data_verifications"), "data_verifications table name")
		sample_table = validate_identifier(tables.get(TableRole.VERIFICATION_SAMPLES.value, "stockguidance_data_verification_samples"), "data_verification_samples table name")
		return TableNames(
			source=validated_tables[TableRole.SOURCE.value], sessions=validated_tables[TableRole.SESSIONS.value],
			tickers=validated_tables[TableRole.TICKERS.value], prices=validated_tables[TableRole.PRICES.value],
			deltas=validated_tables[TableRole.DELTAS.value], imports=validated_tables[TableRole.IMPORTS.value],
			zscore_series=validated_tables[TableRole.ZSCORE_SERIES.value], zscore_sessions=validated_tables[TableRole.ZSCORE_SESSIONS.value],
			price_template=_validate_template(templates["price"], "price"),
			price_delta_template=_validate_template(templates["price_delta"], "price_delta"),
			volume_template=_validate_template(preparation_templates["volume"], "volume"),
			data_verifications=verification_table, data_verification_samples=sample_table,
			schema=validated_tables[TableRole.SCHEMA.value], data_states=validated_tables[TableRole.DATA_STATES.value],
			pending_updates=validated_tables[TableRole.PENDING_UPDATES.value], repair_metadata=validated_tables[TableRole.REPAIR_METADATA.value],
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
