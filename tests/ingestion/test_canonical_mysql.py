from dataclasses import replace
from pathlib import Path
import json
import os
import re
from uuid import uuid4

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.workflows.update_market_data import update_market_data


@pytest.mark.skipif(os.environ.get("STOCKGUIDANCE_MYSQL_INTEGRATION") != "1", reason="Explicit disposable MySQL integration opt-in")
def test_canonical_mysql_import_reuse_and_partial_recovery(harness, calendar, tmp_path, monkeypatch):
	base_path = Path("dbinfo.id")
	base = load_database_configuration(base_path)
	connect = MySQLConnectionFactory()
	admin = connect(base_path)
	cursor = admin.cursor()
	schema = "stockguidance_disposable_archive_" + uuid4().hex[:12]
	assert re.fullmatch(r"stockguidance_disposable_archive_[0-9a-f]{12}", schema) and schema != base.database
	cursor.execute(f"CREATE DATABASE `{schema}`", ())
	try:
		configuration = tmp_path / "native.id"
		configuration.write_text(json.dumps({"host": base.host, "database": schema, "port": base.port, "user": base.user, "password": base.password}))
		source = replace(harness.source, identity=replace(harness.source.identity, adjustment=PriceAdjustment.UNADJUSTED), _configuration=None, data_root=tmp_path / "native-data", database_config=configuration)
		initialize_update_schema(source, connect)
		repository = MySQLMarketDataRepository(source, connect)
		boundary = replace(harness.boundary, source=source.identity)
		settings = replace(harness.settings, database_first=True)
		sessions = calendar.sessions(settings.start, settings.end)
		for session in sessions:
			harness.provider.responses[session.session_date] = replace(harness.response(), source_timestamps=(("AAA", int(session.opens_at.timestamp() * 1000)),))
		def run():
			return update_market_data(source, settings, boundary, calendar, harness.provider, repository, harness.clock)
		assert run().successful
		harness.provider.calls.clear()
		assert run().successful
		assert harness.provider.calls == []
		connection = connect(configuration)
		test_cursor = connection.cursor()
		try:
			test_cursor.execute("INSERT INTO stockguidance_daily_import_in_progress (session_date, started_at) VALUES (%s, %s)", (sessions[0].session_date, harness.clock.now().isoformat()))
			test_cursor.execute("DELETE r FROM raw_stock_data r JOIN transaction_dates s ON s.id = r.date_id WHERE s.transaction_date = %s", (sessions[0].session_date,))
			connection.commit()
		finally:
			test_cursor.close()
			connection.close()
		assert run().successful
		assert harness.provider.calls == []
		query = CloseQuery(SessionQuery(boundary, sessions, harness.clock.now()), (1,))
		closes = VerifiedCloseRepository(source, MySQLValidationRepository(source, connect))
		original_read = Path.read_bytes
		def no_archive_read(path):
			if path.suffix == ".ndjson":
				pytest.fail("Completed close reads must not open archives")
			return original_read(path)
		monkeypatch.setattr(Path, "read_bytes", no_archive_read)
		assert len(closes.read(query).observations) == len(sessions)
	finally:
		cursor.execute(f"DROP DATABASE `{schema}`", ())
		cursor.close()
		admin.close()
