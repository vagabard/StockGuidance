from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.artifacts.decoding import decode_features
from stockguidance.market_data.identity import decode_source, mapping
from stockguidance.portfolio.policy import AllocationPolicy, CashRestRule
from stockguidance.recommendations.configuration import RefreshMode
from stockguidance.simulation.configuration import EvaluationPolicy
from stockguidance.targets.specification import ExecutionPrice, HoldingHorizon, TargetSpec, TargetTransform


class ReportKind(StrEnum):
	TRAINING = "stockguidance-training-1"
	RECOMMENDATION = "stockguidance-recommendation-1"
	RECOMMENDATION_V2 = "stockguidance-recommendation-2"
	SIMULATION = "stockguidance-simulation-1"
	SIMULATION_V2 = "stockguidance-simulation-2"


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
		_validate_model_semantics(fields)
		if kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
			allocation = mapping(fields["allocation"])
			AllocationPolicy(str(allocation["policy"]))
			CashRestRule(str(allocation["cash_rest"]))
			EvaluationPolicy(str(fields["model_policy"]))
		elif kind in (ReportKind.RECOMMENDATION, ReportKind.RECOMMENDATION_V2):
			RefreshMode(str(fields["refresh_mode"]))
	return ReportDocument(path, kind, fields)


def _validate_model_semantics(fields: dict[str, object]) -> None:
	"""Validate the shared source/feature/target identity in v1 and v2 reports."""

	source = decode_source(fields["source"])
	features = mapping(fields["features"])
	feature_payload = dict(features)
	if "channels" in feature_payload and "schema" not in feature_payload:
		feature_payload["schema"] = "configured-features-v2"
	if "version" in feature_payload:
		feature_payload.pop("version")
	if "layout" in feature_payload and feature_payload.get("schema") != "configured-features-v2":
		feature_payload.pop("layout")
	decoded = decode_features(feature_payload)
	if decoded.adjustment is not source.adjustment or decoded.interval is not source.interval:
		raise ValueError("Report source and features disagree")
	if features.get("version") is not None and features.get("version") != decoded.version:
		raise ValueError("Report feature version disagrees with its specification")
	if features.get("layout") is not None and features.get("layout") != decoded.layout:
		raise ValueError("Report feature layout disagrees with its specification")
	target = mapping(fields["target"])
	if "kind" in target:
		target_payload = dict(target)
		target_payload.setdefault("schema", "targets-v2")
		target_payload.setdefault("return_formula", target_payload.get("formula", TargetSpec().raw_formula))
		target_payload.pop("formula", None)
		target_payload.pop("transform", None)
		target_payload = {key: target_payload[key] for key in ("schema", "kind", "horizon", "entry_price", "exit_price", "return_formula", "rolling_window_sessions")}
		_decode_target_v2(target_payload)
	else:
		target_payload = dict(target)
		formula = target_payload.pop("formula", TargetSpec().raw_formula)
		if formula != TargetSpec().raw_formula:
			raise ValueError("Reports require close-to-next-session-close return timing")
		if target_payload.get("schema") == "targets-v1":
			target_payload.pop("schema")
		TargetSpec(HoldingHorizon(str(target_payload["horizon"])), ExecutionPrice(str(target_payload["entry_price"])), ExecutionPrice(str(target_payload["exit_price"])), TargetTransform(str(target_payload["transform"])))


def _decode_target_v2(value: dict[str, object]) -> object:
	"""Use the strict artifact decoder after accepting report payload aliases."""

	from stockguidance.artifacts.decoding import decode_target

	return decode_target(value)


def object_rows(value: object) -> tuple[dict[str, object], ...]:
	if not isinstance(value, list):
		raise ValueError("Expected explicit report rows")
	return tuple(mapping(row) for row in value)
