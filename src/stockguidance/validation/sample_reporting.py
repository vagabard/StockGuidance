from dataclasses import asdict

from stockguidance.datasets.sample import Sample


def sample_payload(sample: Sample, *, assess_features: bool) -> dict[str, object]:
	feature_state = sample.feature_state.value if sample.features.values is not None or not sample.features.issues else "unavailable"
	return {
		"entry_session": sample.timing.entry_session.session_date.isoformat(),
		"exit_session": sample.timing.exit_session.session_date.isoformat(),
		"decision_cutoff": sample.timing.decision_cutoff.isoformat(),
		"label_cutoff": None if sample.label_cutoff is None else sample.label_cutoff.isoformat(),
		"ticker_ids": sample.features.specification.ticker_ids,
		"history_sessions": [session.session_date.isoformat() for session in sample.features.sessions] if assess_features else [],
		"feature_specification": asdict(sample.features.specification) if assess_features else None,
		"feature_version": sample.features.specification.version if assess_features else None,
		"layout": sample.features.specification.layout if assess_features else None,
		"features": sample.features.values if assess_features else None,
		"feature_state": feature_state if assess_features else "not_assessed",
		"raw_targets": sample.targets.raw, "target_state": sample.target_state.value,
		"target_specification": asdict(sample.target_specification),
		"data_revision": sample.data_revision, "cache_key": sample.cache_key,
	}
