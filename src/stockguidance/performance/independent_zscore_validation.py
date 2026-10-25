from pathlib import Path
from typing import Any
import json

from stockguidance.config.source import DEFAULT_CALENDAR_ID
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.performance.vectorized_zscore_validation import PipelineOptions, run_pipeline
from stockguidance.market_data.identity import content_id


def _validate_metadata(connection: Any, names: Any, manifest: dict[str, Any], target_by_id: dict[int, dict[str, Any]],
		expected_counts: dict[tuple[int, str, int], dict[int, int]], ticker_count: int,
		published_metadata: list[dict[str, Any]] | None = None) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {quote_identifier(names.table(TableRole.ZSCORE_SERIES))} ORDER BY series_key", ())
		series_rows = [list(row) for row in cursor.fetchall()]
		cursor.execute(f"SELECT series_key, date_id, row_count, ready_count, status_counts, content_digest, dependency_digest, availability_digest, actual_input_available FROM {quote_identifier(names.table(TableRole.ZSCORE_SESSIONS))} ORDER BY series_key, date_id", ())
		metadata_rows = [list(row) for row in cursor.fetchall()]
	finally:
		cursor.close()
	source = manifest.get("source")
	archive_source = source.get("archive_source") if isinstance(source, dict) else None
	if not isinstance(archive_source, dict):
		raise ValueError("Maintenance workload has no source scope")
	source_scope = content_id(archive_source)
	calendar_id = str(manifest.get("calendar_id", DEFAULT_CALENDAR_ID))
	expected_series: dict[str, tuple[str, int, tuple[Any, ...]]] = {}
	for family, window in ((family, window) for family in ("price", "price_delta") for window in (20, 60)):
		definition = {"schema_version": 1, "source_scope": source_scope, "calendar_id": calendar_id, "family": family, "window": window,
			"calculation_version": "rolling-population-zscore-v1", "delta_definition": "close_ratio_to_prior_minus_one" if family == "price_delta" else "not_applicable",
			"precision": "float32", "missing_value_policy": "null_status_v1"}
		fields = tuple(definition[field] for field in ("schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy"))
		expected_series[content_id(definition)] = (family, window, fields)
	series_rows = [row for row in series_rows if str(row[0]) in expected_series]
	metadata_rows = [row for row in metadata_rows if str(row[0]) in expected_series and int(row[1]) in target_by_id]
	series_findings = []
	for row in series_rows:
		expected = expected_series.get(str(row[0]))
		if expected is None or tuple(row[1:]) != expected[2]:
			series_findings.append({"series_key": row[0], "finding": "series_definition_mismatch"})
	published = {(item["session"], item["family"], int(item["window"])): item for item in published_metadata or ()}
	metadata_findings = []
	for row in metadata_rows:
		series_key, date_id = str(row[0]), int(row[1])
		series = expected_series.get(series_key)
		if series is None or date_id not in target_by_id:
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "out_of_scope_metadata"})
			continue
		family, window, _ = series
		try:
			payload = str(row[4])
			counts = {int(code): int(count) for code, count in json.loads(payload).items()} if payload.startswith("{") else {int(part.split(":", 1)[0]): int(part.split(":", 1)[1]) for part in payload.split(",") if ":" in part}
		except (TypeError, ValueError):
			counts = {}
		expected = expected_counts[(date_id, family, window)]
		if int(row[2]) != ticker_count or int(row[3]) != expected[0] or counts != expected or any(not isinstance(row[index], str) or len(row[index]) != 64 for index in (5, 6, 7)):
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "metadata_count_or_digest_shape_mismatch"})
		reference = published.get((target_by_id[date_id]["session"], family, window))
		if reference is None or any(str(row[index]) != reference.get(field) for index, field in ((5, "content_digest"), (6, "dependency_digest"), (7, "availability_digest"))):
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "published_digest_binding_mismatch"})
	if len(series_rows) != len(expected_series) or len(metadata_rows) != len(target_by_id) * len(expected_series):
		metadata_findings.append({"finding": "missing_or_extra_series_metadata"})
	return {"validation_version": 1, "series_rows": len(series_rows), "metadata_rows": len(metadata_rows), "expected_series": len(expected_series),
		"expected_metadata": len(target_by_id) * len(expected_series), "series_findings": series_findings, "metadata_findings": metadata_findings}


def validate(connection: Any, names: Any, workload: dict[str, Any], directory: Path,
		published_metadata: list[dict[str, Any]], binding: dict[str, str]) -> dict[str, Any]:
	sessions = tuple(workload["input_sessions"]) if workload.get("input_mode") == "database" else tuple(str(item["session"]) for item in workload["files"] if item.get("valid") is True)
	if not sessions:
		raise ValueError("Independent z-score validation has no source sessions")
	placeholders = ",".join("%s" for _ in sessions)
	cursor = connection.cursor()
	try:
		cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())
		cursor.execute("START TRANSACTION READ ONLY", ())
		cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date, id", sessions)
		identifiers: dict[str, int] = {}
		for identifier, session in cursor.fetchall():
			key = str(session)
			if key in identifiers:
				raise ValueError(f"Duplicate session mapping during z-score validation: {key}")
			identifiers[key] = int(identifier)
		cursor.execute(f"SELECT session_date, raw_id, available_at FROM {quote_identifier(names.imports)} WHERE session_date IN ({placeholders}) ORDER BY session_date", sessions)
		receipts = {str(session): (str(raw_id), available_at) for session, raw_id, available_at in cursor.fetchall()}
	finally:
		cursor.close()
	verified = {session: bool(session in receipts and receipts[session][1] and str(receipts[session][1]).strip()) for session in sessions}
	result = run_pipeline(connection, names, workload, identifiers,
		{identifier: verified.get(session, False) for session, identifier in identifiers.items()}, directory,
		binding, PipelineOptions(sessions_per_batch=20), published_metadata=published_metadata,
		metadata_validator=_validate_metadata, progress_label="Independent z-score validation")
	metadata = result["metadata"]
	result["successful"] = result["status"] == "complete" and not result["mismatch_count"] and not metadata["series_findings"] and not metadata["metadata_findings"]
	return result
