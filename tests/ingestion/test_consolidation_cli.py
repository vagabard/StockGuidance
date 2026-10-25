from datetime import date
from hashlib import sha256
import json

from stockguidance.cli.consolidate_market_data import main
from stockguidance.config.source import DataConfiguration, VerificationMode
from stockguidance.market_data.archive import archive_content
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.identity import encode_json


def configuration(tmp_path):
	data_path = tmp_path / "data-configuration.json"
	configuration = DataConfiguration("polygon", "/v2/aggs/grouped/locale/us/market/stocks/{date}", "us-stocks", "us-stocks-excluding-otc", False, False)
	data_path.write_bytes(encode_json({"schema_version": 1, "source_family": configuration.source_family, "endpoint": configuration.endpoint,
		"market": configuration.market, "universe": configuration.universe, "raw_price_split_adjusted": False,
		"raw_price_dividend_adjusted": False, "raw_price_vendor_defined_adjustment": None, "interval": "1d",
		"verification_policy": {"source_family_mismatch": VerificationMode.BLOCK.value, "endpoint_mismatch": VerificationMode.ADVISORY.value, "source_family_sample_days": 100, "endpoint_sample_pulls": 50}}))
	database_path = tmp_path / "replacement.id"
	database_path.write_bytes(encode_json({"host": "unused", "user": "unused", "database": "replacement", "data_configuration_file": str(data_path)}))
	return database_path


def test_migration_dry_run_verifies_sources_without_database_access(tmp_path, harness, calendar, monkeypatch, capsys):
	from stockguidance.cli import consolidate_market_data
	monkeypatch.setattr(consolidate_market_data, "MySQLConnectionFactory", lambda: (_ for _ in ()).throw(AssertionError("Dry run must not access a database")))
	session = calendar.session(date(2024, 7, 1))
	from dataclasses import replace
	response = replace(harness.response(), source_timestamps=(("AAA", int(session.opens_at.timestamp() * 1000)),))
	content = archive_content(DownloadedSession(session, response, harness.clock.now()))
	path = tmp_path / "verified.ndjson"
	path.write_bytes(content)
	decisions = tmp_path / "decisions.json"
	decision = {"date": "2024-07-01", "path": str(path), "sha256": sha256(content).hexdigest(), "adjustment": "unadjusted", "complete": True,
		"available_at": harness.clock.now().isoformat(), "evidence": "Verified fixture response"}
	decisions.write_text(json.dumps([decision]))
	arguments = ["--dbinfo-file", str(configuration(tmp_path)), "--decisions", str(decisions), "--data-root", str(tmp_path / "data")]
	assert main(arguments) == 0
	assert json.loads(capsys.readouterr().out) == {"validated_dates": 1, "applied": False}
	decision["adjustment"] = "adjusted"
	decisions.write_text(json.dumps([decision]))
	assert main(arguments) == 1
	assert not (tmp_path / "data").exists()


def test_apply_without_decisions_never_changes_archives(tmp_path):
	assert main(["--apply", "--data-root", str(tmp_path)]) == 1
	assert list(tmp_path.iterdir()) == []
