import json
from datetime import datetime
from typing import TYPE_CHECKING

from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.market_data.quality import QualityRecord
from stockguidance.database.dates import normalize_date_id

if TYPE_CHECKING:
	from stockguidance.database.repositories.market_data import SessionSnapshot


CURRENT = "stockguidance_row_quality"
AUDIT = "stockguidance_quality_revisions"
OUTPUTS = "stockguidance_output_quality"
SCANS = "stockguidance_quality_scans"


def schema_statements() -> tuple[str, ...]:
	return (
		f"CREATE TABLE IF NOT EXISTS {CURRENT} (date_id INT NOT NULL, ticker_id INT NOT NULL, revision CHAR(64) NOT NULL, flags INT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(date_id, ticker_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {AUDIT} (date_id INT NOT NULL, ticker_id INT NOT NULL, revision CHAR(64) NOT NULL, recorded_at VARCHAR(40) NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(date_id, ticker_id, revision)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {OUTPUTS} (series_key VARCHAR(128) NOT NULL, date_id INT NOT NULL, ticker_id INT NOT NULL, flags INT NOT NULL, gap_length INT NOT NULL, provenance LONGTEXT NOT NULL, PRIMARY KEY(series_key, date_id, ticker_id)) ENGINE=InnoDB",
		f"CREATE TABLE IF NOT EXISTS {SCANS} (scope_key CHAR(64) PRIMARY KEY, last_session VARCHAR(10) NOT NULL, completed_at VARCHAR(40) NOT NULL) ENGINE=InnoDB",
	)


def read_quality(cursor: Cursor, date_ids: tuple[int, ...], *, for_update: bool = False, ticker_ids: tuple[int, ...] | None = None) -> dict[tuple[int, int], QualityRecord]:
	if not date_ids:
		return {}
	placeholders = ",".join("%s" for _ in date_ids)
	ticker_clause = "" if ticker_ids is None else " AND ticker_id IN (" + ",".join("%s" for _ in ticker_ids) + ")"
	lock_clause = " FOR UPDATE" if for_update else ""
	cursor.execute(f"SELECT date_id, ticker_id, payload FROM {CURRENT} WHERE date_id IN ({placeholders}){ticker_clause}{lock_clause}", date_ids + (() if ticker_ids is None else ticker_ids))
	return {(normalize_date_id(row[0]), normalize_date_id(row[1])): QualityRecord.from_payload(json.loads(str(row[2]))) for row in cursor.fetchall()}


def save_quality(cursor: Cursor, date_id: int, ticker_id: int, record: QualityRecord, now: datetime) -> bool:
	cursor.execute(f"SELECT revision, payload FROM {CURRENT} WHERE date_id = %s AND ticker_id = %s FOR UPDATE", (date_id, ticker_id))
	previous = cursor.fetchall()
	if previous:
		old = QualityRecord.from_payload(json.loads(str(previous[0][1])))
		old_payload, new_payload = old.payload(), record.payload()
		old_payload.pop("available_at")
		new_payload.pop("available_at")
		if old_payload == new_payload:
			return False
	payload = json.dumps(record.payload(), sort_keys=True, separators=(",", ":"))
	cursor.execute(f"INSERT IGNORE INTO {AUDIT} (date_id, ticker_id, revision, recorded_at, payload) VALUES (%s,%s,%s,%s,%s)", (date_id, ticker_id, record.revision, now.isoformat(), payload))
	cursor.execute(f"DELETE FROM {CURRENT} WHERE date_id = %s AND ticker_id = %s", (date_id, ticker_id))
	cursor.execute(f"INSERT INTO {CURRENT} (date_id, ticker_id, revision, flags, payload) VALUES (%s,%s,%s,%s,%s)", (date_id, ticker_id, record.revision, int(record.flags), payload))
	return True


def invalidate(cursor: Cursor, names: TableNames, date_ids: tuple[int, ...]) -> None:
	if not date_ids:
		return
	placeholders = ",".join("%s" for _ in date_ids)
	cursor.execute(f"UPDATE {quote_identifier(names.imports)} SET derived_dependency_id = NULL, derived_content_id = NULL WHERE session_date IN (SELECT transaction_date FROM {quote_identifier(names.sessions)} WHERE id IN ({placeholders}))", date_ids)
	cursor.execute(f"DELETE FROM {OUTPUTS} WHERE date_id IN ({placeholders})", date_ids)


def derived_quality(cursor: Cursor, date_id: int, ticker_ids: dict[str, int], snapshots: tuple["SessionSnapshot", ...], current: "SessionSnapshot") -> None:
	cursor.execute(f"DELETE FROM {OUTPUTS} WHERE series_key = %s AND date_id = %s", ("legacy-derived", date_id))
	if not any(snapshot.quality for snapshot in snapshots):
		return
	for bar in current.records:
		lineage = {}
		flags, gap = 0, 0
		for column in range(16):
			dependencies: list[QualityRecord] = []
			for snapshot in snapshots:
				position = snapshot.session.sequence - current.session.sequence
				if (column < 7 and position in (-1, 0)) or (column == 7 and position == 0) or (column >= 8 and position in (0, 1)):
					dependencies.extend(record for record in snapshot.quality if record.observation.ticker == bar.ticker or (column in (12, 14) and position == 1))
			if dependencies:
				lineage[str(column)] = [{"revision": record.revision, "flags": int(record.flags), "gap_length": record.gap_length} for record in dependencies]
				for record in dependencies:
					flags |= int(record.flags)
					gap = max(gap, record.gap_length)
		if lineage:
			cursor.execute(f"INSERT INTO {OUTPUTS} (series_key, date_id, ticker_id, flags, gap_length, provenance) VALUES (%s,%s,%s,%s,%s,%s)", ("legacy-derived", date_id, ticker_ids[bar.ticker], flags, gap, json.dumps(lineage, sort_keys=True)))
