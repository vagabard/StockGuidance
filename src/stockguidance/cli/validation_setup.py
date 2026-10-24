import argparse
from datetime import date, datetime, timedelta

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import DataConfiguration, PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.mysql_sessions import MySQLSessionRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.features.specification import CloseFeature, FeatureSpec, SignalMode
from stockguidance.market_data.discovery import DiscoveryRequest, HistoryOverride, discover_coverage
from stockguidance.market_data.history import InventoryIssue, LocalCoverageInventory, UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.providers.contracts import CapabilityState, ProviderCapability
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.result import ValidationResult
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def _capability_from_cache(store: JsonHistoryStore, identity: SourceIdentity) -> tuple[ProviderCapability | None, str | None]:
	try:
		metadata = store.load_discovery_metadata(identity)
	except ValueError as error:
		return None, str(error)
	if metadata is None:
		return None, None
	try:
		return ProviderCapability(
			identity, CapabilityState(metadata.provider_state), metadata.provider_checked_at,
			metadata.provider_provenance, "Cached provider capability evidence",
			metadata.provider_earliest, metadata.provider_latest, metadata.provider_freshness,
			metadata.provider_earliest_observed_success,
		), None
	except (TypeError, ValueError) as error:
		return None, f"Invalid cached provider capability: {error}"


def assemble_and_validate(arguments: argparse.Namespace) -> ValidationResult:
	clock = SystemClock()
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	database_configuration = load_database_configuration(database_path)
	if database_configuration.data_configuration_file is None:
		data_configuration = DataConfiguration("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment) is PriceAdjustment.ADJUSTED, False)
	else:
		data_configuration = database_configuration.load_data_configuration()
	identity = data_configuration.to_source_identity()
	source = SourceConfig(
		identity,
		resolve_cli_path(arguments.data_root, "data"),
		database_path,
		"unused-by-read-only-validation",
		_configuration=data_configuration,
	)
	table_names = database_configuration.load_table_names()
	if (arguments.ticker_ids is None) != (arguments.lookback_sessions is None):
		raise ValueError("Feature validation requires both ticker IDs and lookback")
	mode = SignalMode(arguments.signal_mode)
	if mode is SignalMode.ENTRY_OPEN and arguments.ticker_ids is None:
		raise ValueError("Opening validation requires the full ordered universe and lookback")
	order = (CloseFeature.RELATIVE_CLOSE,) if mode is SignalMode.PREVIOUS_CLOSE else (CloseFeature.RELATIVE_CLOSE, CloseFeature.ENTRY_OPEN_RETURN)
	features = None if arguments.ticker_ids is None else FeatureSpec(tuple(int(value.strip()) for value in arguments.ticker_ids.split(",")), arguments.lookback_sessions, signal_mode=mode, feature_order=order, adjustment=identity.adjustment)
	cutoff = datetime.fromisoformat(arguments.as_of) if arguments.as_of else clock.now()
	requested_start = date.fromisoformat(arguments.start_date) if arguments.start_date else None
	requested_end = date.fromisoformat(arguments.end_date) if arguments.end_date else None
	calendar_start = requested_start or date(1970, 1, 1)
	calendar_end = max(cutoff.date(), requested_end or cutoff.date()) + timedelta(days=14)
	calendar = ExchangeCalendar("XNYS", calendar_start, calendar_end)
	store = JsonHistoryStore(source.data_root / "history")
	boundary = store.load(identity)
	local_inventories: list[LocalCoverageInventory] = []
	manifest_store = ManifestStore(source.data_root)
	manifest_inventory = manifest_store.inventory(identity, calendar, start=calendar.start, end=calendar.end)
	local_inventories.append(manifest_inventory)
	if arguments.dbinfo_file != "dbinfo.id" or arguments.data_root == "data" or manifest_inventory.observations:
		try:
			local_inventories.append(MySQLSessionRepository(source, MySQLConnectionFactory(), table_names).inventory(calendar.start, calendar.end, calendar.calendar_id))
		except Exception:
			local_inventories.append(LocalCoverageInventory(identity, calendar.calendar_id, issues=(InventoryIssue(calendar.calendar_id, "Database history inventory could not be read; schema inspection remains unresolved"),)))
	local_inventory = LocalCoverageInventory.merge(local_inventories)
	capability, capability_issue = _capability_from_cache(store, identity)
	if capability_issue is not None:
		local_inventory = LocalCoverageInventory.merge((local_inventory, LocalCoverageInventory(identity, calendar.calendar_id, issues=(InventoryIssue(calendar.calendar_id, capability_issue),))))
	if arguments.history_start:
		if not arguments.history_evidence:
			raise ValueError("Explicit history requires evidence")
		history_override = HistoryOverride(identity, date.fromisoformat(arguments.history_start), arguments.history_evidence)
	else:
		history_override = None
	if arguments.publication_delay_minutes is not None and not arguments.publication_evidence:
		raise ValueError("Known publication delay requires provenance")
	publication = DailyPublicationPolicy(None if arguments.publication_delay_minutes is None else timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence or "Publication policy unknown")
	discovery = discover_coverage(
		identity, calendar, clock, publication,
		request=DiscoveryRequest(identity, requested_start, requested_end, cutoff),
		local_inventory=local_inventory, provider_capability=capability,
		history_boundary=boundary, history_override=history_override,
	)
	if discovery.effective_start is None or discovery.effective_end is None:
		raise UnknownHistoryBoundary("Supported history is unknown")
	settings = ValidationSettings(requested_start, requested_end, publication, cutoff, features, zscore_windows=getattr(arguments, "zscore_windows", None))
	zscore_repository = None if settings.zscore_windows is None else MySQLZScoreRepository(source, MySQLConnectionFactory(), table_names, ManifestStore(source.data_root))
	return validate_database(source, settings, boundary, calendar, MySQLValidationRepository(source, MySQLConnectionFactory(), table_names), clock, discovery=discovery, zscore_repository=zscore_repository)
