"""A recorded validation pass for the current data and implementation."""

from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.market_data.identity import content_id


GATE_DDL = "CREATE TABLE IF NOT EXISTS stockguidance_validation_gate (date_id INT NOT NULL, phase VARCHAR(16) NOT NULL, data_version CHAR(64) NOT NULL, design_version CHAR(64) NOT NULL, passed_at VARCHAR(40) NOT NULL, PRIMARY KEY(date_id,phase)) ENGINE=InnoDB"


@lru_cache(maxsize=1)
def design_version() -> str:
	root = Path(__file__).resolve().parents[1]
	paths = ("database/validation_gate.py", "database/numerical_rebuild.py", "database/raw_rebuild.py", "database/simplified_runtime.py", "workflows/simplified_readiness.py", "market_data/legacy_derived.py", "zscores/calculation.py")
	digest = sha256(b"current-history-session-chronology-v1")
	for name in paths:
		digest.update(name.encode())
		digest.update((root / name).read_bytes())
	return digest.hexdigest()


def data_versions(cursor, names, date_ids) -> dict[int, str]:
	if not date_ids:
		return {}
	marks = ",".join("%s" for _ in date_ids)
	sessions = quote_identifier(names.table(TableRole.SESSIONS))
	repairs = quote_identifier(names.table(TableRole.REPAIR_METADATA))
	cursor.execute(f"SELECT id,raw_complete,raw_revision,raw_completed_at FROM {sessions} WHERE id IN ({marks}) ORDER BY id", tuple(date_ids))
	rows = cursor.fetchall()
	cursor.execute(f"SELECT date_id,ticker_id,revision FROM {repairs} WHERE date_id IN ({marks}) ORDER BY date_id,ticker_id,revision", tuple(date_ids))
	changes = defaultdict(list)
	for day, ticker, revision in cursor.fetchall():
		changes[int(day)].append((int(ticker),str(revision)))
	return {int(row[0]):content_id([str(v) for v in row] + [changes[int(row[0])]]) for row in rows}


def require_raw_pass(cursor, names, date_ids) -> None:
	versions = data_versions(cursor, names, date_ids)
	marks = ",".join("%s" for _ in date_ids)
	cursor.execute(f"SELECT date_id,data_version,design_version FROM stockguidance_validation_gate WHERE phase='raw' AND date_id IN ({marks})", tuple(date_ids))
	passed = {int(day): (data, design) for day,data,design in cursor.fetchall()}
	if any(passed.get(int(day)) != (versions.get(int(day)), design_version()) for day in date_ids):
		raise ValueError("Current data/design version has no validation pass; validate the session before use")


def record_raw_passes(source, connect, versions, *, phase="raw") -> None:
	if not versions:
		return
	connection = connect(source.database_config)
	cursor = connection.cursor()
	try:
		for day, version in versions.items():
			cursor.execute("DELETE FROM stockguidance_validation_gate WHERE date_id=%s AND phase=%s", (day,phase))
			cursor.execute("INSERT INTO stockguidance_validation_gate (date_id,phase,data_version,design_version,passed_at) VALUES (%s,%s,%s,%s,%s)", (day,phase,version,design_version(),datetime.now(timezone.utc).isoformat()))
		connection.commit()
	except Exception:
		connection.rollback()
		raise
	finally:
		cursor.close()
		connection.close()
