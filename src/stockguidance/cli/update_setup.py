import argparse
from datetime import date, timedelta
from pathlib import Path

from stockguidance.calendar.clock import Clock, SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.mysql_sessions import ConnectionFactory, MySQLSessionRepository
from stockguidance.database.table_names import TableNames
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.discovery import CoverageDiscoveryResult, DiscoveryRequest, HistoryOverride, discover_coverage
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance, InventoryIssue, LocalCoverageInventory, UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.providers.contracts import CapabilityState, ProviderCapability
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.repair import load_repair_plan
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data
from stockguidance.workflows.update_result import UpdateResult


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


def _calendar_for_update(arguments: argparse.Namespace, boundary: HistoryBoundary | None, clock: Clock) -> ExchangeCalendar:
	requested_start = None if arguments.start_date is None else date.fromisoformat(arguments.start_date)
	requested_end = None if arguments.end_date is None else date.fromisoformat(arguments.end_date)
	calendar_start = min([date(1970, 1, 1), *(item for item in (requested_start, None if boundary is None else boundary.history_start) if item is not None)])
	calendar_end = max(clock.now().date(), requested_end or clock.now().date()) + timedelta(days=14)
	return ExchangeCalendar("XNYS", calendar_start, calendar_end)


def _local_inventory(source: SourceConfig, calendar: ExchangeCalendar, manifest_inventory: LocalCoverageInventory, connect: ConnectionFactory, table_names: TableNames, *, include_database: bool) -> LocalCoverageInventory:
	inventories = [manifest_inventory]
	if include_database:
		try:
			inventories.append(MySQLSessionRepository(source, connect, table_names).inventory(calendar.start, calendar.end, calendar.calendar_id))
		except Exception:
			inventories.append(LocalCoverageInventory(source.identity, calendar.calendar_id, issues=(InventoryIssue(calendar.calendar_id, "Database history inventory could not be read; retained database observations remain unresolved"),)))
	return LocalCoverageInventory.merge(inventories)


def _query_boundary(source: SourceConfig, calendar: ExchangeCalendar, discovery: CoverageDiscoveryResult, clock: Clock) -> HistoryBoundary:
	values = [value for value in (discovery.effective_start, discovery.local_bounds.first_observed, discovery.fetchable_start) if value is not None]
	if discovery.history_boundary is not None:
		values.append(discovery.history_boundary.history_start)
	if not values:
		raise UnknownHistoryBoundary("No retained or currently evidenced provider session establishes an update scope")
	query_start = min(values)
	if discovery.history_boundary is not None and query_start >= discovery.history_boundary.history_start:
		return discovery.history_boundary
	return HistoryBoundary(
		source.identity, calendar.calendar_id, calendar.session(query_start).session_date,
		HistoryProvenance.SOURCE_HISTORY, "Retained local audit query floor; not a provider coverage claim", clock.now(),
	)


def assemble_and_update(arguments: argparse.Namespace) -> UpdateResult:
	clock = SystemClock()
	identity = SourceIdentity("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment))
	source = SourceConfig(
		identity,
		resolve_cli_path(arguments.data_root, "data"),
		resolve_cli_path(arguments.dbinfo_file, "dbinfo.id"),
		"polygon_fetch.DEFAULT_API_KEY or POLYGON_API_KEY",
	)
	database_configuration = load_database_configuration(source.database_config)
	table_names = database_configuration.load_table_names()
	repair_path = getattr(arguments, "repair_plan", None)
	repair_plan = None if repair_path is None else load_repair_plan(Path(repair_path))
	history_store = JsonHistoryStore(source.data_root / "history")
	boundary = history_store.load(identity)
	calendar = _calendar_for_update(arguments, boundary, clock)
	manifest_store = ManifestStore(source.data_root)
	connect = MySQLConnectionFactory()
	manifest_inventory = manifest_store.inventory(identity, calendar, start=calendar.start, end=calendar.end)
	include_database = arguments.dbinfo_file != "dbinfo.id" or arguments.data_root == "data" or bool(manifest_inventory.observations) or boundary is not None
	local_inventory = _local_inventory(source, calendar, manifest_inventory, connect, table_names, include_database=include_database)
	cached_capability, capability_issue = _capability_from_cache(history_store, identity)
	if capability_issue is not None:
		local_inventory = LocalCoverageInventory.merge((local_inventory, LocalCoverageInventory(identity, calendar.calendar_id, issues=(InventoryIssue(calendar.calendar_id, capability_issue),))))
	provider = ConfiguredPolygonProvider(arguments.max_retries)
	requested_start = None if arguments.start_date is None else date.fromisoformat(arguments.start_date)
	requested_end = None if arguments.end_date is None else date.fromisoformat(arguments.end_date)
	if repair_plan is not None:
		if requested_start is None:
			requested_start = repair_plan.start
		if requested_end is None:
			requested_end = repair_plan.end
	if arguments.history_start is not None:
		if arguments.history_evidence is None:
			raise ValueError("An explicit history start requires --history-evidence")
		history_override = HistoryOverride(identity, date.fromisoformat(arguments.history_start), arguments.history_evidence)
	else:
		history_override = None
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	discovery = discover_coverage(
		identity, calendar, clock, publication,
		request=DiscoveryRequest(identity, requested_start, requested_end, clock.now()),
		local_inventory=local_inventory, provider=provider, provider_capability=cached_capability,
		history_boundary=boundary, history_override=history_override,
	)
	if discovery.effective_start is None or discovery.effective_end is None:
		raise UnknownHistoryBoundary("No supported-history scope is known and no fresh provider capability establishes a fetch boundary")
	start = requested_start or discovery.effective_start
	end = requested_end or discovery.effective_end
	if start is None or end is None or start > end:
		raise ValueError("Update bounds do not intersect discovered exchange-session coverage")
	query_boundary = _query_boundary(source, calendar, discovery, clock)
	capability_resolved = discovery.provider_is_fresh
	if repair_plan is not None:
		repair_plan.validate_for_update(
		identity, start, end, clock.now(), fetchable_start=discovery.fetchable_start,
		fetchable_end=discovery.fetchable_end, capability_resolved=capability_resolved,
	)
	settings = UpdateSettings(
		start, end, publication,
		CorrectionPolicy(
			timedelta(days=arguments.recent_window_days), timedelta(hours=arguments.recent_refresh_hours),
			timedelta(days=arguments.archive_refresh_days), arguments.correction_evidence,
		),
		repair_plan, discovery,
	)
	if arguments.initialize_schema:
		initialize_update_schema(source, connect, table_names)
	if arguments.history_start is not None and boundary is None and discovery.history_boundary is not None:
		history_store.save(discovery.history_boundary)
	result = update_market_data(source, settings, query_boundary, calendar, provider, MySQLMarketDataRepository(source, connect, table_names), clock)
	history_store.save_discovery_metadata(discovery.metadata())
	return result
