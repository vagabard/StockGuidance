from dataclasses import asdict

from stockguidance.market_data.legacy_derived import LEGACY_DERIVED_SPEC
from stockguidance.market_data.providers.contracts import ResponseState
from stockguidance.validation.findings import ValidationCheck
from stockguidance.validation.result import ValidationResult
from stockguidance.validation.sample_reporting import sample_payload
from stockguidance.datasets.sample import TargetState
from stockguidance.targets.specification import TargetSpec


def _discovery_payload(result: ValidationResult) -> dict[str, object]:
	discovery = result.discovery
	if discovery is None:
		return {
			"local_observed_bounds": None,
			"verified_retained_coverage": None,
			"provider_capability": None,
			"effective_scope": None,
			"latest_expected_published_session": None,
			"unresolved_limits": [],
		}
	return {
		"local_observed_bounds": {
			"first": None if discovery.local_bounds.first_observed is None else discovery.local_bounds.first_observed.isoformat(),
			"last": None if discovery.local_bounds.last_observed is None else discovery.local_bounds.last_observed.isoformat(),
			"first_quality": None if discovery.local_bounds.first_quality is None else discovery.local_bounds.first_quality.value,
			"last_quality": None if discovery.local_bounds.last_quality is None else discovery.local_bounds.last_quality.value,
			"observed_sessions": [item.isoformat() for item in discovery.local_bounds.observed_sessions],
		},
		"verified_retained_coverage": {
			"first": None if not discovery.retained.verified_sessions else discovery.retained.verified_sessions[0].isoformat(),
			"last": None if not discovery.retained.verified_sessions else discovery.retained.verified_sessions[-1].isoformat(),
			"verified_sessions": [item.isoformat() for item in discovery.retained.verified_sessions],
			"missing_sessions": [item.isoformat() for item in discovery.retained.missing_sessions],
			"unverified_sessions": [item.isoformat() for item in discovery.retained.unverified_sessions],
			"holes": [item.isoformat() for item in discovery.retained.holes],
		},
		"provider_capability": {
			"state": discovery.provider.state.value,
			"checked_at": discovery.provider.checked_at.isoformat(),
			"provenance": discovery.provider.provenance,
			"detail": discovery.provider.detail,
			"fresh": discovery.provider_is_fresh,
			"freshness_seconds": None if discovery.provider.freshness is None else discovery.provider.freshness.total_seconds(),
			"earliest": None if discovery.provider.earliest_session is None else discovery.provider.earliest_session.isoformat(),
			"latest": None if discovery.provider.latest_session is None else discovery.provider.latest_session.isoformat(),
			"earliest_observed_success": None if discovery.provider.earliest_observed_success is None else discovery.provider.earliest_observed_success.isoformat(),
			"fetchable_start": None if discovery.fetchable_start is None else discovery.fetchable_start.isoformat(),
			"fetchable_end": None if discovery.fetchable_end is None else discovery.fetchable_end.isoformat(),
		},
		"effective_scope": {
			"start": None if discovery.effective_start is None else discovery.effective_start.isoformat(),
			"end": None if discovery.effective_end is None else discovery.effective_end.isoformat(),
		},
		"latest_expected_published_session": None if discovery.latest_expected_published_session is None else discovery.latest_expected_published_session.session_date.isoformat(),
		"unresolved_limits": [{"code": item.code, "severity": item.severity.value, "detail": item.detail} for item in discovery.diagnostics],
	}


def report_payload(result: ValidationResult) -> dict[str, object]:
	plan = result.plan
	manifests = {manifest.download.session.session_date: manifest for manifest in result.manifests}
	snapshots = {snapshot.session.session_date: snapshot for snapshot in result.snapshots}
	queried_dates = {session.session_date for session in result.queried_sessions}
	counts = {count.session_date: count for count in result.record_counts}
	samples = {sample.timing.entry_session.session_date: sample for sample in result.samples}
	discovery_payload = _discovery_payload(result)
	zscore_payload = None if result.zscore_windows is None else {
		"windows": list(result.zscore_windows),
		"families": ["price", "price_delta"],
		"audit_records": [record.payload() for record in result.zscore_audit],
		"audit_contract": "verified_inputs_recomputed_independently; float32-aware rtol=1e-5 atol=1e-6",
	}
	schema_findings = [finding for finding in result.findings if finding.check is ValidationCheck.SCHEMA]
	sessions: list[dict[str, object]] = []
	for item in result.coverage.sessions:
		session_date = item.session.session_date
		manifest, snapshot = manifests.get(session_date), snapshots.get(session_date)
		available = session_date in queried_dates and not schema_findings and (manifest is None or manifest.download.available_at <= plan.request.as_of) and (snapshot is None or snapshot.receipt is None or snapshot.receipt.available_at <= plan.request.as_of)
		count = counts.get(session_date)
		checks = [ValidationCheck.SESSION.value]
		if available:
			checks.extend([ValidationCheck.SOURCE.value, ValidationCheck.DUPLICATE.value, ValidationCheck.ORPHAN.value, ValidationCheck.RAW_VALUES.value])
			if snapshot is not None and (manifest is None or manifest.download.response.state is not ResponseState.NOT_YET_PUBLISHED):
				checks.extend([ValidationCheck.IMPORT.value, ValidationCheck.DERIVED.value, ValidationCheck.OUTCOME.value, ValidationCheck.HISTORY.value])
				if manifest is not None:
					checks.append(ValidationCheck.RAW_KEYS.value)
			sample = samples.get(session_date)
			if sample is not None and sample.target_state is TargetState.READY:
				checks.append(ValidationCheck.TARGET.value)
			if sample is not None and result.feature_specification is not None and sample.features.values is not None:
				checks.append(ValidationCheck.FEATURE.value)
		sessions.append({"date": session_date.isoformat(), "state": item.state.value, "checks": checks, "source_keys": [] if manifest is None or not available else [bar.ticker for bar in manifest.download.response.records], "raw_valid_keys": [] if snapshot is None or not available else [bar.ticker for bar in snapshot.records], "derived_valid_keys": [] if snapshot is None or not available else [row.ticker for row in snapshot.derived], "source_count": None if manifest is None or not available else len(manifest.download.response.records), "raw_count": None if count is None or not available else count.raw_count, "derived_count": None if count is None or not available else count.derived_count, "source_state": None if manifest is None else manifest.download.response.state.value, "provider_counts": None if manifest is None or not available else dict(manifest.download.response.counts), "available_at": None if manifest is None else manifest.download.available_at.isoformat()})
	reported_history_start = None if result.discovery is not None and result.discovery.history_boundary is None else plan.boundary.history_start
	reported_history_provenance = "unresolved" if result.discovery is not None and result.discovery.history_boundary is None else plan.boundary.provenance.value
	return {
		"complete": result.complete, "failed": result.failed, "exit_code": result.exit_code,
		"source": asdict(plan.request.source), "start": plan.request.start.isoformat(), "end": plan.request.end.isoformat(), "as_of": plan.request.as_of.isoformat(),
		"history_start": None if reported_history_start is None else reported_history_start.isoformat(), "history_provenance": reported_history_provenance, "calendar_version": plan.calendar_version,
		**discovery_payload,
		"observed_first_in_scope": None if plan.observed_first_in_scope is None else plan.observed_first_in_scope.isoformat(), "observed_last_in_scope": None if plan.observed_last_in_scope is None else plan.observed_last_in_scope.isoformat(),
		"excluded_before_history": plan.excluded_before_history, "excluded_after_cutoff": plan.excluded_after_cutoff,
		"derived_spec": LEGACY_DERIVED_SPEC, "completeness_scope": "Declared provider request only; provider omissions do not establish ticker lifetimes or independent market completeness",
		"target_specification": asdict(TargetSpec()), "target_storage": "computed_from_verified_raw_closes; legacy derived columns are not close-to-close targets",
		"feature_specification": None if result.feature_specification is None else asdict(result.feature_specification),
		"zscore_validation": zscore_payload,
		"samples": [sample_payload(sample, assess_features=result.feature_specification is not None) for sample in result.samples],
		"schema_state": "failed" if result.failed else "unverified" if schema_findings else "verified" if result.queried_sessions else "not_assessed",
		"inspected_sessions": [session.session_date.isoformat() for session in result.queried_sessions], "unassigned_orphan_scope": "All records with no date mapping; no historical date bounds are inferred for these records",
		"sessions": sessions,
		"findings": [{"category": finding.category.value, "check": finding.check.value, "detail": finding.detail, "session_date": None if finding.session_date is None else finding.session_date.isoformat(), "dependency_session_date": None if finding.dependency_session_date is None else finding.dependency_session_date.isoformat(), "tickers": list(finding.tickers), "record_ids": list(finding.record_ids), "blocking": finding.blocking} for finding in result.findings],
		"repair_plan": result.repair_plan.payload(),
	}


def render_text(result: ValidationResult) -> str:
	status = "failed" if result.failed else "complete" if result.complete else "incomplete"
	discovery = _discovery_payload(result)
	local = discovery["local_observed_bounds"]
	provider = discovery["provider_capability"]
	effective = discovery["effective_scope"]
	history = "unresolved" if result.discovery is not None and result.discovery.history_boundary is None else str(result.plan.boundary.history_start)
	lines = [f"Database validation: {status} (exit {result.exit_code})", f"Source: {result.plan.request.source.provider} {result.plan.request.source.endpoint}; {result.plan.request.source.universe}; {result.plan.request.source.adjustment.value}", f"Auditable request: {result.plan.request.start} through {result.plan.request.end}; cutoff {result.plan.request.as_of.isoformat()}", f"Local observed bounds: {local}", f"Verified retained coverage: {discovery['verified_retained_coverage']}", f"Provider capability: {provider}", f"Effective scope: {effective}; latest expected published session: {discovery['latest_expected_published_session']}", f"Unresolved limits: {discovery['unresolved_limits']}", f"Supported history: {history}; derived specification: {LEGACY_DERIVED_SPEC}"]
	if result.zscore_windows is not None:
		lines.append(f"Z-score validation: windows {result.zscore_windows}; audit records {len(result.zscore_audit)}; Python-only read-only audit")
	for session in result.coverage.sessions:
		lines.append(f"{session.session.session_date}: {session.state.value}")
	for snapshot in result.snapshots:
		lines.append(f"Assessed database keys {snapshot.session.session_date}: {','.join(bar.ticker for bar in snapshot.records) or '(none)'}")
	for sample in result.samples:
		lines.append(f"Close target {sample.timing.entry_session.session_date} -> {sample.timing.exit_session.session_date}: {sample.target_state.value}; tickers {sample.features.specification.ticker_ids}; raw returns {sample.targets.raw}")
		if result.feature_specification is not None:
			lines.append(f"Previous-close features: {'ready' if sample.features.values is not None else 'unavailable'}; exact history {[session.session_date.isoformat() for session in sample.features.sessions]}")
	for finding in result.findings:
		tickers = ",".join(finding.tickers)
		lines.append(f"{finding.category.value}: {finding.session_date or 'request'} {finding.check.value} {tickers}: {finding.detail}")
	lines.append(f"Repair proposals: {len(result.repair_plan.items)}; validation performs no repairs")
	return "\n".join(lines)
