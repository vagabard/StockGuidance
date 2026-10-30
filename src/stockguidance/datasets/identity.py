from dataclasses import asdict
from datetime import datetime

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceIdentity
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2, FeatureSpecification
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.identity import content_id
from stockguidance.targets.specification import TargetSpec


def feature_identity(source: SourceIdentity, specification: FeatureSpecification, timing: DecisionTiming, revision: str, calendar_version: str, availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, data_snapshot: datetime | None = None) -> str:
	return content_id({
		"schema": "configured-features-2" if isinstance(specification, FeatureSpecV2) else "close-features-1", "source": asdict(source), "features": asdict(specification),
		"feature_version": specification.version, "layout": specification.layout,
		"entry": timing.entry_session.session_date.isoformat(), "exit": timing.exit_session.session_date.isoformat(),
		"decision_cutoff": timing.decision_cutoff.isoformat(), "calendar": timing.entry_session.calendar_id,
		"calendar_version": calendar_version, "data_revision": revision, "availability_policy": availability_policy.value,
		"data_snapshot": None if not availability_policy.retrospective or data_snapshot is None else data_snapshot.isoformat(),
	})


def sample_identity(feature_key: str, specification: TargetSpec, label_cutoff: datetime | None, revision: str, availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, data_snapshot: datetime | None = None) -> str:
	return content_id({"schema": "close-sample-1", "features": feature_key, "target": asdict(specification), "label_cutoff": None if label_cutoff is None else label_cutoff.isoformat(), "data_revision": revision, "availability_policy": availability_policy.value, "data_snapshot": None if not availability_policy.retrospective or data_snapshot is None else data_snapshot.isoformat()})
