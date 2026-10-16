from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.artifacts.decoding import decode_features, exact_mapping
from stockguidance.market_data.identity import decode_source, mapping
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.policy import AllocationPolicy, CashRestRule
from stockguidance.simulation.configuration import EvaluationPolicy
from stockguidance.targets.specification import ExecutionPrice, HoldingHorizon, TargetSpec, TargetTransform


class ReportKind(StrEnum):
	TRAINING = "stockguidance-training-1"
	RECOMMENDATION = "stockguidance-recommendation-1"
	SIMULATION = "stockguidance-simulation-1"


@dataclass(frozen=True)
class ReportDocument:
	path: Path
	kind: ReportKind
	payload: dict[str, object]


def reject_constant(value: str) -> object:
	raise ValueError("Reports require finite JSON numbers")


def load_report(path: Path) -> ReportDocument:
	fields = mapping(json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant))
	kind = ReportKind(str(fields.get("schema")))
	if type(fields.get("successful")) is not bool:
		raise ValueError("A report must state its success status")
	if kind is not ReportKind.TRAINING:
		source = decode_source(fields["source"])
		features = mapping(fields["features"])
		decoded = decode_features({key: value for key, value in features.items() if key not in ("version", "layout")})
		if decoded.adjustment is not source.adjustment or decoded.interval is not source.interval or features.get("version") != decoded.version or features.get("layout") != decoded.layout:
			raise ValueError("Report source and features disagree")
		target = exact_mapping(fields["target"], ("horizon", "entry_price", "exit_price", "transform", "formula"))
		TargetSpec(HoldingHorizon(str(target["horizon"])), ExecutionPrice(str(target["entry_price"])), ExecutionPrice(str(target["exit_price"])), TargetTransform(str(target["transform"])))
		if target["formula"] != TargetSpec().raw_formula:
			raise ValueError("Reports require raw close-to-next-session-close returns")
		allocation = mapping(fields["allocation"])
		AllocationPolicy(str(allocation["policy"]))
		CashRestRule(str(allocation["cash_rest"]))
		if kind is ReportKind.SIMULATION:
			EvaluationPolicy(str(fields["model_policy"]))
		elif kind is ReportKind.RECOMMENDATION:
			ModelRefreshPolicy(str(fields["model_policy"]))
	return ReportDocument(path, kind, fields)


def object_rows(value: object) -> tuple[dict[str, object], ...]:
	if not isinstance(value, list):
		raise ValueError("Expected explicit report rows")
	return tuple(mapping(row) for row in value)
