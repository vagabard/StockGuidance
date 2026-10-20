from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stockguidance.database.table_names import TableNames, TableRole, quote_identifier
from stockguidance.legacy_migration.precision import ArchiveInventory


@dataclass(frozen=True)
class TickerIdentity:
	identifier: int
	label: str

	def __post_init__(self) -> None:
		if type(self.identifier) is not int or self.identifier <= 0:
			raise ValueError("Ticker ID must be a positive integer")
		if not isinstance(self.label, str) or not self.label or len(self.label) > 16 or any(character.isspace() for character in self.label):
			raise ValueError("Ticker label must be nonempty ticker text")


def identity_migration_ddl(table_names: TableNames) -> tuple[str, ...]:
	tickers = quote_identifier(table_names.table(TableRole.TICKERS))
	return (
		f"ALTER TABLE {tickers} MODIFY label VARCHAR(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL",
		f"ALTER TABLE {tickers} DROP INDEX label, ADD UNIQUE INDEX label (label)",
	)


def identity_fixtures() -> tuple[dict[str, Any], ...]:
	return (
		{"keys": ("AAP", "AAp"), "expected": "distinct_exact_keys"},
		{"keys": ("ABKPZ", "ABKpZ"), "expected": "distinct_preferred_series_keys"},
	)


def identity_report(inventory: ArchiveInventory, existing: tuple[TickerIdentity, ...] = (), table_names: TableNames | None = None) -> dict[str, object]:
	rows = tuple((item.identifier, item.label) for item in existing)
	result = inventory.identity(rows)
	selected = table_names
	result.update(
		{
			"operation": "case_sensitive_ticker_identity",
			"database_inventory_available": bool(existing),
			"database_inventory_note": None if existing else "Existing ticker IDs were not supplied; no database mutation is authorized in Stage 5.",
			"migration_ddl": list(identity_migration_ddl(selected)) if selected is not None else [],
			"fixtures": list(identity_fixtures()),
			"exact_case_rule": "Ticker labels are compared as exact Unicode strings; case-folded values are collision diagnostics only.",
		}
	)
	return result
