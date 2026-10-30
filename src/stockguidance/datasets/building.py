from datetime import datetime

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime
from stockguidance.database.repositories.closes import CloseData, CloseQuery, CloseRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.datasets.sample import Sample, TargetState
from stockguidance.features.channels import FeatureChannelData, FeatureChannelQuery, FeatureChannelRepository
from stockguidance.features.construction import FeatureValues, construct_configured_features, construct_features
from stockguidance.features.opening_values import opening_values
from stockguidance.features.history_window import WindowState, plan_feature_history
from stockguidance.features.specification import FeatureChannel, FeatureSpec, FeatureSpecV2, FeatureSpecification, SignalMode
from stockguidance.market_data.closes import PriceState
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.construction import TargetValues, construct_targets
from stockguidance.targets.specification import TargetSpec


def build_sample(
	specification: FeatureSpecification, target: TargetSpec, timing: DecisionTiming,
	boundary: HistoryBoundary, calendar: SessionCalendar, publication: DailyPublicationPolicy,
	repository: CloseRepository | None, *, label_cutoff: datetime | None = None,
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, data_snapshot: datetime | None = None,
	channel_repository: FeatureChannelRepository | None = None,
) -> Sample:
	"""One construction path for inference and historical training/evaluation.

	Only an explicit label cutoff authorizes outcome reads. Features always
	retain their original decision cutoff, even during later training.
	"""
	if not isinstance(specification, (FeatureSpec, FeatureSpecV2)) or not isinstance(target, TargetSpec) or not isinstance(timing, DecisionTiming):
		raise ValueError("Sample construction requires explicit specifications and decision timing")
	boundary.validate_for(boundary.source, calendar)
	if not isinstance(availability_policy, AvailabilityPolicy):
		raise ValueError("Sample construction requires a typed availability policy")
	availability_policy.validate_features(specification)
	if specification.adjustment is not boundary.source.adjustment or specification.interval is not boundary.source.interval:
		raise ValueError("Feature adjustment/interval does not match the data source")
	if any(session != calendar.session(session.session_date) for session in (timing.previous_session, timing.entry_session, timing.exit_session)):
		raise ValueError("Sample timing does not match the authoritative calendar")
	if label_cutoff is not None:
		require_aware_datetime(label_cutoff, "label cutoff")
		if label_cutoff < timing.decision_cutoff:
			raise ValueError("Training cutoff precedes the sample decision")
	cutoff = timing.decision_cutoff if label_cutoff is None else label_cutoff
	query_as_of = availability_policy.query_as_of(cutoff, data_snapshot)
	window = plan_feature_history(calendar, boundary, specification, timing, publication)
	sessions: set[TradingSession] = set()
	if window.state is WindowState.EXPECTED_AVAILABLE and (not isinstance(specification, FeatureSpecV2) or specification.close_channels or specification.requires_entry_open):
		sessions.update(window.dependency_sessions if isinstance(specification, FeatureSpecV2) else window.sessions)
	target_state = TargetState.NOT_REQUESTED
	if label_cutoff is not None:
		expected = tuple(publication.expected_at(session) for session in (timing.entry_session, timing.exit_session))
		if timing.entry_session.session_date < boundary.history_start:
			target_state = TargetState.UNAVAILABLE
		elif any(available_at is None for available_at in expected):
			target_state = TargetState.UNVERIFIED
		elif any(session.closes_at > label_cutoff for session in (timing.entry_session, timing.exit_session)) or any(available_at is not None and available_at > label_cutoff for available_at in expected):
			target_state = TargetState.NOT_YET_AVAILABLE
		else:
			target_state = TargetState.READY
			sessions.update((timing.entry_session, timing.exit_session))
	data = None
	channel_data: FeatureChannelData | None = None
	if isinstance(specification, FeatureSpecV2) and window.state is WindowState.EXPECTED_AVAILABLE and specification.external_channels:
		if channel_repository is None:
			raise ValueError("Configured stored channels require a feature channel repository")
		channel_query = FeatureChannelQuery(boundary.source, specification, window.dependency_sessions, window.sessions, query_as_of)
		channel_data = channel_repository.read(channel_query)
		if not isinstance(channel_data, FeatureChannelData) or channel_data.query != channel_query:
			raise ValueError("Feature channel repository changed the requested source, sessions, cutoff, or layout")
	openings = None
	if window.state is WindowState.EXPECTED_AVAILABLE and specification.signal_mode is SignalMode.ENTRY_OPEN:
		if repository is None:
			raise ValueError("Entry-open features require a close repository")
		openings = opening_values(specification, timing, boundary, repository)
	if sessions:
		if repository is None:
			raise ValueError("Close-backed features or labels require a close repository")
		query = CloseQuery(SessionQuery(boundary, tuple(sorted(sessions, key=lambda session: session.sequence)), query_as_of), specification.ticker_ids)
		data = repository.read(query)
		if not isinstance(data, CloseData) or data.query != query:
			raise ValueError("Close repository changed the requested source, sessions, cutoff, or ticker layout")
	if window.state is WindowState.EXPECTED_AVAILABLE and isinstance(specification, FeatureSpecV2):
		features = construct_configured_features(specification, timing, window.sessions, data, channel_data, openings, availability_policy, publication)
	elif window.state is WindowState.EXPECTED_AVAILABLE and data is not None:
		assert isinstance(specification, FeatureSpec)
		features = construct_features(specification, timing, window.sessions, data, openings, availability_policy, publication)
	else:
		features = FeatureValues(specification, window.sessions, None)
	values = (None,) * len(specification.ticker_ids)
	targets = TargetValues(values, values)
	if target_state is TargetState.READY and data is not None:
		targets = construct_targets(target, specification.ticker_ids, timing.entry_session, timing.exit_session, data)
		if not targets.ready:
			if any(issue.state is PriceState.NOT_YET_AVAILABLE for issue in targets.issues):
				target_state = TargetState.NOT_YET_AVAILABLE
			elif any(issue.state is PriceState.UNVERIFIED for issue in targets.issues):
				target_state = TargetState.UNVERIFIED
			else:
				target_state = TargetState.UNAVAILABLE
	feature_revision = content_id({"window_state": window.state.value, "history_start": boundary.history_start.isoformat(), "dependency_sessions": [session.session_date.isoformat() for session in window.dependency_sessions]})
	if data is not None and window.state is WindowState.EXPECTED_AVAILABLE:
		feature_query = CloseQuery(SessionQuery(boundary, window.sessions, query_as_of), specification.ticker_ids)
		feature_revision = CloseData(feature_query, tuple(item for item in data.observations if item.timing.session in window.sessions), tuple(issue for issue in data.issues if issue.session in window.sessions)).revision
	if openings is not None:
		feature_revision = content_id({"closes": feature_revision, "openings": openings.revision})
	if channel_data is not None:
		feature_revision = content_id({"closes": feature_revision, "channels": channel_data.revision_with_readiness})
	if channel_data is not None:
		revision = content_id({"closes": None if data is None else data.revision, "features": feature_revision, "openings": None if openings is None else openings.revision})
	else:
		revision = content_id({"closes": feature_revision if data is None else data.revision, "openings": None if openings is None else openings.revision}) if openings is not None else feature_revision if data is None else data.revision
	return Sample(boundary.source, timing, features, window.state, target, targets, target_state, label_cutoff, feature_revision, revision, calendar.version, availability_policy, query_as_of)
