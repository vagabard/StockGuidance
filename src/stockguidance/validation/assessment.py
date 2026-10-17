from datetime import datetime

from stockguidance.config.source import SourceIdentity
from stockguidance.database.repositories.market_data import SessionSnapshot
from stockguidance.market_data.coverage import SessionUpdate
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, legacy_rows
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.market_data.providers.contracts import ResponseState
from stockguidance.market_data.records import records_id
from stockguidance.market_data.update_planning import SnapshotIndex, reconciled
from stockguidance.validation.coverage import CoverageState
from stockguidance.validation.derived import differing_columns
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


def assess_session(source: SourceIdentity, item: SessionUpdate, manifest: SourceManifest | None, snapshot: SessionSnapshot | None, index: SnapshotIndex, cutoff: datetime) -> tuple[ValidationFinding, ...]:
	session_date = item.session.session_date
	findings: list[ValidationFinding] = []

	def add(category: FindingCategory, check: ValidationCheck, detail: str, tickers: tuple[str, ...] = ()) -> None:
		findings.append(ValidationFinding(category, check, detail, session_date, tickers))

	if item.state is CoverageState.NOT_YET_AVAILABLE or (manifest is not None and manifest.download.available_at > cutoff) or (snapshot is not None and snapshot.receipt is not None and snapshot.receipt.available_at > cutoff):
		add(FindingCategory.NOT_YET_AVAILABLE, ValidationCheck.SESSION, "Daily observation or actual receipt is after the cutoff; values are not assessed")
		return tuple(findings)
	if item.expected_available_at is None:
		add(FindingCategory.UNVERIFIED, ValidationCheck.SESSION, "Provider publication policy is unknown; no data query is authorized")
		if manifest is None and snapshot is None:
			return tuple(findings)
	if manifest is None:
		add(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Validated request-scoped source evidence is missing or corrupt")
	elif manifest.source != source or manifest.download.session != item.session:
		add(FindingCategory.INVALID, ValidationCheck.SOURCE, "Source evidence has a different request or session identity")
	elif manifest.download.response.state is ResponseState.NOT_YET_PUBLISHED:
		add(FindingCategory.NOT_YET_AVAILABLE, ValidationCheck.SOURCE, "Provider explicitly reports data not yet published")
		add(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Expected publication has passed; required provider data remains unverified")
		return tuple(findings)
	elif manifest.download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
		add(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Provider count, continuation, or response evidence does not establish completeness")
	if snapshot is None:
		add(FindingCategory.MISSING, ValidationCheck.SESSION, "Expected published session has no usable database mapping or records")
		return tuple(findings)
	raw = {bar.ticker for bar in snapshot.records}
	if manifest is not None and manifest.source == source:
		expected = {bar.ticker for bar in manifest.download.response.records}
		missing, extra = tuple(sorted(expected - raw)), tuple(sorted(raw - expected))
		if missing:
			add(FindingCategory.MISSING, ValidationCheck.RAW_KEYS, "Source record keys are missing from raw storage", missing)
		if extra:
			add(FindingCategory.INVALID, ValidationCheck.RAW_KEYS, "Raw storage contains keys absent from the declared provider response", extra)
		if not missing and not extra and records_id(manifest.download.response.records) != snapshot.raw_content_id:
			expected_bars = {bar.ticker: bar for bar in manifest.download.response.records}
			add(FindingCategory.INVALID, ValidationCheck.RAW_VALUES, "Raw financial content differs from the validated provider response", tuple(bar.ticker for bar in snapshot.records if bar != expected_bars[bar.ticker]))
	if not snapshot.raw_verified:
		add(FindingCategory.INCOMPLETE, ValidationCheck.IMPORT, "Raw import receipt is absent or its revision differs from stored content", tuple(sorted(raw)))
	derived = {row.ticker: row for row in snapshot.derived}
	if raw - derived.keys():
		add(FindingCategory.MISSING, ValidationCheck.DERIVED, "Required legacy derived rows are missing", tuple(sorted(raw - derived.keys())))
	if derived.keys() - raw:
		add(FindingCategory.INVALID, ValidationCheck.ORPHAN, "Derived rows have no corresponding raw key", tuple(sorted(derived.keys() - raw)))
	if not snapshot.derived_verified(index.dependency(snapshot)):
		add(FindingCategory.INCOMPLETE, ValidationCheck.DERIVED, "Derived import is interrupted or content/dependency receipts are stale", tuple(sorted(raw)))
	prior, following = index.neighbors(snapshot)
	for required in legacy_rows(prior, snapshot.records, following):
		actual = derived.get(required.ticker)
		if actual is None:
			continue
		for column_index in differing_columns(actual, required):
			check = ValidationCheck.OUTCOME if column_index >= 8 else ValidationCheck.DERIVED
			category = FindingCategory.MISSING if actual.values[column_index] is None else FindingCategory.INVALID
			add(category, check, f"Legacy column {DERIVED_COLUMNS[column_index]} differs from available chronological dependencies", (required.ticker,))
	if not findings and not reconciled(source, manifest, snapshot, index, cutoff):
		add(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Source, import, and derived evidence could not be reconciled")
	return tuple(findings)


def coverage_state(findings: tuple[ValidationFinding, ...]) -> CoverageState:
	categories = {finding.category for finding in findings if finding.category is not FindingCategory.NOT_YET_AVAILABLE or finding.check in {ValidationCheck.SESSION, ValidationCheck.SOURCE}}
	for category, state in ((FindingCategory.FAILED, CoverageState.INCONSISTENT), (FindingCategory.INVALID, CoverageState.INCONSISTENT), (FindingCategory.UNVERIFIED, CoverageState.UNVERIFIED), (FindingCategory.MISSING, CoverageState.MISSING), (FindingCategory.INCOMPLETE, CoverageState.INCOMPLETE), (FindingCategory.NOT_YET_AVAILABLE, CoverageState.NOT_YET_AVAILABLE)):
		if category in categories:
			return state
	return CoverageState.COMPLETE
