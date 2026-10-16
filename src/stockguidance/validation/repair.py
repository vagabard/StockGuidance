from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_enum
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


class RepairAction(StrEnum):
	REFRESH_SOURCE = "refresh_source"
	REIMPORT = "reimport"
	REBUILD_DERIVED = "rebuild_legacy_derived"
	COMPLETE_MANIFEST = "complete_manifest"
	REVIEW_SCHEMA = "review_schema"
	REVIEW_RECORDS = "review_records"
	ESTABLISH_POLICY = "establish_publication_policy"
	CAPTURE_OPENING = "capture_opening_evidence"


AUTOMATIC_ACTIONS = frozenset({RepairAction.REFRESH_SOURCE, RepairAction.REIMPORT, RepairAction.REBUILD_DERIVED, RepairAction.COMPLETE_MANIFEST})


@dataclass(frozen=True)
class RepairItem:
	action: RepairAction
	session_date: date | None
	check: ValidationCheck
	tickers: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairPlan:
	source: SourceIdentity
	start: date
	end: date
	as_of: datetime
	items: tuple[RepairItem, ...]

	def __post_init__(self) -> None:
		require_aware_datetime(self.as_of, "repair plan cutoff")
		if not isinstance(self.source, SourceIdentity) or type(self.start) is not date or type(self.end) is not date or self.start > self.end or not isinstance(self.items, tuple):
			raise ValueError("Invalid repair plan scope")
		for item in self.items:
			if not isinstance(item, RepairItem):
				raise ValueError("Invalid repair item")
			require_enum(item.action, RepairAction, "repair action")
			require_enum(item.check, ValidationCheck, "repair check")
			if item.session_date is not None and (type(item.session_date) is not date or (item.action in AUTOMATIC_ACTIONS and not self.start <= item.session_date <= self.end)):
				raise ValueError("Repair session is outside the declared request")
			if item.action in AUTOMATIC_ACTIONS and item.session_date is None:
				raise ValueError("Automatic repair requires an exact session")
			if not isinstance(item.tickers, tuple) or any(not isinstance(ticker, str) or not ticker for ticker in item.tickers):
				raise ValueError("Invalid repair ticker layout")

	def validate_for_update(self, source: SourceIdentity, start: date, end: date, cutoff: datetime) -> None:
		if self.source != source or self.start != start or self.end != end or self.as_of > cutoff:
			raise ValueError("Repair plan does not match the update source, bounds, or clock")
		if any(item.action not in AUTOMATIC_ACTIONS for item in self.items):
			raise ValueError("Repair plan requires manual schema, integrity, or policy review before updating")

	def payload(self) -> dict[str, object]:
		return {"schema_version": 1, "source": asdict(self.source), "start": self.start.isoformat(), "end": self.end.isoformat(), "as_of": self.as_of.isoformat(), "items": [{"action": item.action.value, "session_date": None if item.session_date is None else item.session_date.isoformat(), "check": item.check.value, "tickers": list(item.tickers)} for item in self.items]}


def plan_repairs(source: SourceIdentity, start: date, end: date, cutoff: datetime, findings: tuple[ValidationFinding, ...]) -> RepairPlan:
	items: list[RepairItem] = []
	for finding in findings:
		if not finding.blocking:
			continue
		if finding.check in {ValidationCheck.FEATURE, ValidationCheck.TARGET} and any(other.blocking and other.check not in {ValidationCheck.FEATURE, ValidationCheck.TARGET} and other.session_date in {None, finding.dependency_session_date or finding.session_date} for other in findings):
			continue
		if finding.check is ValidationCheck.OPENING:
			action = RepairAction.CAPTURE_OPENING
		elif finding.session_date is not None and not start <= finding.session_date <= end:
			action = RepairAction.REVIEW_RECORDS
		elif finding.check is ValidationCheck.SCHEMA:
			action = RepairAction.REVIEW_SCHEMA
		elif finding.check in {ValidationCheck.DUPLICATE, ValidationCheck.ORPHAN} or finding.session_date is None:
			action = RepairAction.REVIEW_RECORDS
		elif finding.check is ValidationCheck.SESSION and finding.category is FindingCategory.UNVERIFIED:
			action = RepairAction.ESTABLISH_POLICY
		elif finding.check is ValidationCheck.SOURCE and finding.category is FindingCategory.INCOMPLETE:
			action = RepairAction.COMPLETE_MANIFEST
		elif finding.check in {ValidationCheck.SOURCE, ValidationCheck.SESSION}:
			action = RepairAction.REFRESH_SOURCE
		elif finding.check in {ValidationCheck.RAW_KEYS, ValidationCheck.RAW_VALUES, ValidationCheck.IMPORT}:
			action = RepairAction.REIMPORT
		elif finding.check in {ValidationCheck.FEATURE, ValidationCheck.TARGET}:
			action = RepairAction.REVIEW_RECORDS
		else:
			action = RepairAction.REBUILD_DERIVED
		item = RepairItem(action, finding.session_date, finding.check, finding.tickers)
		if item not in items:
			items.append(item)
	return RepairPlan(source, start, end, cutoff, tuple(items))


def load_repair_plan(path: Path) -> RepairPlan:
	payload = mapping(json.loads(path.read_text(encoding="utf-8")))
	if "repair_plan" in payload:
		payload = mapping(payload["repair_plan"])
	values = payload.get("items")
	if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1 or not isinstance(values, list):
		raise ValueError("Unsupported repair plan")
	items: list[RepairItem] = []
	for value in values:
		row = mapping(value)
		tickers = row["tickers"]
		if not isinstance(tickers, list) or any(not isinstance(ticker, str) for ticker in tickers):
			raise ValueError("Invalid repair tickers")
		items.append(RepairItem(RepairAction(text_field(row, "action")), None if row["session_date"] is None else date.fromisoformat(text_field(row, "session_date")), ValidationCheck(text_field(row, "check")), tuple(tickers)))
	return RepairPlan(decode_source(payload["source"]), date.fromisoformat(text_field(payload, "start")), date.fromisoformat(text_field(payload, "end")), datetime.fromisoformat(text_field(payload, "as_of")), tuple(items))
