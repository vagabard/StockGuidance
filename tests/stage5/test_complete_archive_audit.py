from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from stockguidance.database.table_names import load_table_names
from stockguidance.legacy_migration import audit
from stockguidance.legacy_migration.precision import scan_archive
from stockguidance.market_data.identity import data_configuration_key


class AuditCursor:
	def __init__(self, connection):
		self.connection = connection
		self.pending = []

	def execute(self, query, parameters=()):
		assert not self.pending
		self.connection.queries.append((query, parameters))
		if query.startswith("SELECT configuration_key"):
			configuration = audit.ARCHIVE_SOURCE.to_data_configuration()
			key = data_configuration_key(configuration)
			self.pending = [(key, key, configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value, 1, 0, None)]
		elif "information_schema.TABLES" in query:
			self.pending = [(name, "InnoDB") for name in parameters]
		elif "information_schema.COLUMNS" in query:
			self.pending = [("fixture", "id", "int", "NO", None)]
		elif query.startswith("SELECT @@server_uuid"):
			self.pending = [("fixture-server", "fixture", 1)]
		elif query.startswith("SELECT transaction_date"):
			self.pending = [(session,) for session in self.connection.dates]
		elif query.startswith("SELECT COUNT(*)"):
			self.pending = [(0, 0, 0)]
		elif query.startswith("SELECT t.transaction_date"):
			self.pending = [row for row in self.connection.rows if not parameters or parameters[0] <= row[0] <= parameters[-1]]
		else:
			assert query.startswith(("SET TRANSACTION", "START TRANSACTION READ ONLY"))

	def fetchall(self):
		rows, self.pending = self.pending, []
		return rows

	def fetchmany(self, size):
		if self.connection.fail_fetch:
			raise RuntimeError("original stream failure")
		rows, self.pending = self.pending[:size], self.pending[size:]
		return rows

	def close(self):
		if self.pending:
			raise RuntimeError("Unread result found")


class AuditConnection:
	def __init__(self, rows, dates=None, fail_fetch=False):
		self.rows = rows
		self.dates = dates or sorted({row[0] for row in rows})
		self.fail_fetch = fail_fetch
		self.queries = []
		self.closed = False
		self.rolled_back = False

	def cursor(self):
		return AuditCursor(self)

	def rollback(self):
		self.rolled_back = True

	def close(self):
		self.closed = True


def write_archive(root: Path, session: str, ticker="AAp", price="1.000001") -> Path:
	path = root / "market_summary" / session[:4] / f"{session}.ndjson"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps({"T": ticker, "o": price, "h": price, "l": price, "c": price, "v": "1.25", "t": 1719864000000 if session == "2024-07-01" else 1719950400000}) + "\n", encoding="utf-8")
	return path


def database_row(session, price="1.000001", ticker="AAp"):
	return (date.fromisoformat(session), ticker, *(Decimal(price) for _ in range(4)), Decimal("1.25"), None, None, 7)


def test_complete_audit_reports_invalid_database_prices_without_aborting(tmp_path, monkeypatch):
	write_archive(tmp_path, "2024-07-01")
	connection = AuditConnection([database_row("2024-07-01", "0")])
	monkeypatch.setattr(audit, "streaming_connection", lambda path: connection)
	monkeypatch.setattr(audit, "load_database_configuration", lambda path: type("Config", (), {"load_table_names": lambda self: load_table_names()})())
	report = audit.audit_archive(tmp_path, tmp_path / "config.json")
	assert report["complete"] is True
	assert report["matches"] is False
	assert report["database_row_count"] == 1
	assert report["invalid_database_row_count"] == 1
	session = report["sessions"][0]
	assert session["invalid_database_row_examples"][0]["values"]["open"] == "0"
	assert session["field_difference_counts"]["open"] == 1
	assert connection.closed and connection.rolled_back
	assert sum(query.startswith("SELECT COUNT(*)") for query, _ in connection.queries) == 2
	assert next(query for query, _ in connection.queries if query.startswith("SELECT t.transaction_date")).endswith("ORDER BY t.transaction_date")


def test_exact_comparison_does_not_round_six_decimal_prices(tmp_path, monkeypatch):
	write_archive(tmp_path, "2024-07-01")
	connection = AuditConnection([database_row("2024-07-01", "1.000002")])
	monkeypatch.setattr(audit, "streaming_connection", lambda path: connection)
	monkeypatch.setattr(audit, "load_database_configuration", lambda path: type("Config", (), {"load_table_names": lambda self: load_table_names()})())
	report = audit.audit_archive(tmp_path, tmp_path / "config.json")
	assert report["complete"] is True
	assert report["sessions"][0]["difference_examples"][0]["fields"]["open"] == {"archive": "1.000001", "database": "1.000002"}


def test_failed_stream_closes_connection_and_preserves_original_exception(tmp_path, monkeypatch):
	write_archive(tmp_path, "2024-07-01")
	connection = AuditConnection([database_row("2024-07-01")], fail_fetch=True)
	monkeypatch.setattr(audit, "streaming_connection", lambda path: connection)
	monkeypatch.setattr(audit, "load_database_configuration", lambda path: type("Config", (), {"load_table_names": lambda self: load_table_names()})())
	with pytest.raises(RuntimeError, match="original stream failure"):
		audit.audit_archive(tmp_path, tmp_path / "config.json")
	assert connection.closed


def test_preloaded_inventory_is_filtered_and_database_only_dates_remain_explicit(tmp_path, monkeypatch):
	write_archive(tmp_path, "2024-07-01")
	write_archive(tmp_path, "2024-07-02")
	archive = scan_archive(tmp_path)
	connection = AuditConnection([database_row("2024-07-01")], dates=[date(2024, 7, 1), date(2024, 7, 3)])
	monkeypatch.setattr(audit, "streaming_connection", lambda path: connection)
	monkeypatch.setattr(audit, "load_database_configuration", lambda path: type("Config", (), {"load_table_names": lambda self: load_table_names()})())
	report = audit.audit_archive(tmp_path, tmp_path / "config.json", archive_inventory=archive, start=date(2024, 7, 1), end=date(2024, 7, 3))
	assert report["complete"] is True
	assert report["database_only_dates"] == ["2024-07-03"]
	bounded = audit.audit_archive(tmp_path, None, archive_inventory=archive, start=date(2024, 7, 2), end=date(2024, 7, 2))
	assert bounded["archive_file_count"] == 1
	assert bounded["sessions"][0]["session"] == "2024-07-02"


def test_changed_quarantined_file_and_corrupt_evidence_block_completion(tmp_path, monkeypatch):
	path = write_archive(tmp_path, "2024-07-01", price="0")
	archive = scan_archive(tmp_path)
	path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
	connection = AuditConnection([database_row("2024-07-01")])
	monkeypatch.setattr(audit, "streaming_connection", lambda path: connection)
	monkeypatch.setattr(audit, "load_database_configuration", lambda path: type("Config", (), {"load_table_names": lambda self: load_table_names()})())
	report = audit.audit_archive(tmp_path, tmp_path / "config.json", archive_inventory=archive)
	assert report["complete"] is False
	assert report["sessions"][0]["state"] == "changed_file"
	write_archive(tmp_path, "2024-07-01")
	evidence_root = tmp_path / "evidence"
	evidence_root.mkdir()
	report = audit.audit_archive(tmp_path, tmp_path / "config.json", evidence_path=evidence_root)
	assert report["complete"] is False
	assert report["sessions"][0]["state"] == "invalid_evidence"
