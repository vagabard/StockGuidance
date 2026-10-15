from dataclasses import asdict
from datetime import timezone

from stockguidance.config.source import SourceConfig
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.identity import encode_json, source_key
from stockguidance.market_data.legacy_derived import LEGACY_DERIVED_SPEC
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.workflows.update_result import UpdateResult


def persist_update_result(source: SourceConfig, publication: DailyPublicationPolicy, corrections: CorrectionPolicy, result: UpdateResult) -> None:
	request = result.coverage.request
	metadata = {
		"schema_version": 1, "source": asdict(source.identity), "start": request.start.isoformat(), "end": request.end.isoformat(),
		"as_of": request.as_of.isoformat(), "calendar_version": result.plan.calendar_version,
		"history_start": result.plan.boundary.history_start.isoformat(), "data_revision": result.data_revision,
		"derived_spec": LEGACY_DERIVED_SPEC, "successful": result.successful,
		"publication_policy": {"after_close_seconds": None if publication.after_close is None else publication.after_close.total_seconds(), "provenance": publication.provenance},
		"correction_policy": {"recent_window_seconds": corrections.recent_window.total_seconds(), "recent_recheck_seconds": corrections.recent_recheck.total_seconds(), "archive_recheck_seconds": corrections.archive_recheck.total_seconds(), "provenance": corrections.provenance},
		"sessions": [{"date": item.session.session_date.isoformat(), "state": item.state.value} for item in result.coverage.sessions],
		"failures": [{"code": failure.code.value, "stage": failure.stage.value} for failure in result.failures],
	}
	stamp = request.as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
	path = source.data_root / "market_data" / source_key(source.identity) / "runs" / f"{stamp}-{result.data_revision}.json"
	atomic_write(path, encode_json(metadata))
