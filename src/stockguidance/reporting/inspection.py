"""Database-free inspection and performance views for saved runs.

The inspection layer deliberately consumes only a saved manifest and its
versioned report.  It does not load an artifact, reconstruct a model, contact
the database/provider, or call replay/rerun services.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from stockguidance.market_data.identity import mapping
from stockguidance.reporting.documents import ReportDocument, ReportKind, load_report, object_rows
from stockguidance.runs.contracts import RunManifest, RunOperation, RunStatus, RunSummary
from stockguidance.runs.service import RunDirectoryService


_MISSING = "[missing]"


@dataclass(frozen=True)
class ConfigurationDifference:
	"""One changed persisted configuration field between two attempts."""

	path: str
	left: object
	right: object

	def to_mapping(self) -> dict[str, object]:
		return {"path": self.path, "left": self.left, "right": self.right}


@dataclass(frozen=True)
class PerformanceSeries:
	"""Presentation-ready equity, drawdown, cash, and benchmark series."""

	equity: tuple[dict[str, object], ...]
	drawdown: tuple[dict[str, object], ...]
	subportfolios: tuple[dict[str, object], ...]
	benchmarks: tuple[dict[str, object], ...]
	incomplete_marks: tuple[dict[str, object], ...]
	missing_data: tuple[dict[str, object], ...]
	open_positions: tuple[dict[str, object], ...]

	def to_mapping(self) -> dict[str, object]:
		return {
			"equity": list(self.equity),
			"drawdown": list(self.drawdown),
			"subportfolios": list(self.subportfolios),
			"benchmarks": list(self.benchmarks),
			"incomplete_marks": list(self.incomplete_marks),
			"missing_data": list(self.missing_data),
			"open_positions": list(self.open_positions),
		}


@dataclass(frozen=True)
class RunInspection:
	"""Typed manifest/report pair plus derived, side-effect-free display data."""

	manifest: RunManifest
	report: ReportDocument | None
	report_path: Path | None
	report_error: str | None
	children: tuple[RunSummary, ...]
	performance: PerformanceSeries | None

	@property
	def report_available(self) -> bool:
		return self.report is not None

	def to_mapping(self) -> dict[str, object]:
		return {
			"manifest": self.manifest.to_mapping(),
			"report": None if self.report is None else self.report.payload,
			"report_path": None if self.report_path is None else str(self.report_path),
			"report_error": self.report_error,
			"children": [
				{
					"run_id": child.run_id,
					"path": str(child.path),
					"valid": child.valid,
					"status": None if child.manifest is None else child.manifest.status.value,
					"operation": None if child.manifest is None else child.manifest.operation.value,
					"error": child.error,
				}
				for child in self.children
			],
			"view": run_view(self),
		}


@dataclass(frozen=True)
class RunComparison:
	"""Compatibility result for comparing saved attempts.

	Differences are always returned.  Performance aggregation is only exposed
	when the saved specifications are compatible, preventing mixed target units
	or model contracts from being presented as one result.
	"""

	inspections: tuple[RunInspection, ...]
	compatible: bool
	differences: tuple[ConfigurationDifference, ...]

	@property
	def aggregatable(self) -> bool:
		return self.compatible and all(item.performance is not None for item in self.inspections)

	def to_mapping(self) -> dict[str, object]:
		return {
			"compatible": self.compatible,
			"aggregatable": self.aggregatable,
			"run_ids": [item.manifest.run_id for item in self.inspections],
			"differences": [item.to_mapping() for item in self.differences],
			"summaries": [run_view(item) for item in self.inspections],
		}


def _mapping(value: object, name: str = "value") -> dict[str, object]:
	if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
		raise ValueError(f"{name} must be a string-keyed mapping")
	return dict(value)


def _decimal(value: object, name: str) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError(f"{name} must be numeric")
	try:
		result = Decimal(str(value))
	except (InvalidOperation, ValueError) as error:
		raise ValueError(f"{name} must be numeric") from error
	if not result.is_finite():
		raise ValueError(f"{name} must be finite")
	return result


def _text(value: object, name: str) -> str:
	if not isinstance(value, str):
		raise ValueError(f"{name} must be text")
	return value


def _safe_report_path(root: Path, value: object) -> Path:
	path_text = _text(value, "report path")
	if "\\" in path_text:
		raise ValueError("Saved report paths must use POSIX separators")
	relative = PurePosixPath(path_text)
	if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts) or not relative.parts or relative.parts[0] != "reports":
		raise ValueError("Saved report path must stay below reports/")
	path = (root / Path(*relative.parts)).resolve()
	try:
		path.relative_to(root.resolve())
	except ValueError as error:
		raise ValueError("Saved report path escapes its run directory") from error
	return path


def _report_reference(manifest: RunManifest, root: Path) -> Path | None:
	reports = manifest.results.get("reports")
	if reports is None:
		return None
	fields = _mapping(reports, "manifest reports")
	return _safe_report_path(root, fields.get("path"))


def list_saved_runs(
	service: RunDirectoryService,
	*,
	operation: RunOperation | None = None,
	status: RunStatus | None = None,
	parent_run_id: str | None = None,
	include_invalid: bool = True,
) -> tuple[RunSummary, ...]:
	"""List/filter attempts without opening reports or executing workflows."""

	items = service.list_runs(include_invalid=include_invalid)
	return tuple(
		item
		for item in items
		if item.manifest is None
		or ((operation is None or item.manifest.operation is operation)
		and (status is None or item.manifest.status is status)
		and (parent_run_id is None or item.manifest.parent_run_id == parent_run_id))
	)


def inspect_saved_run(service: RunDirectoryService, run_id: str, *, validate: bool = False) -> RunInspection:
	"""Open one manifest and its explicitly referenced typed report.

	Missing reports are represented for failed attempts and old manifest-only
	runs.  A present but corrupt report is surfaced as a stable inspection error;
	the corrupt payload is never echoed.
	"""

	manifest = service.validate(run_id) if validate else service.read(run_id)
	handle = service.handle(run_id)
	report_path: Path | None = None
	report: ReportDocument | None = None
	report_error: str | None = None
	try:
		report_path = _report_reference(manifest, handle.directory)
	except ValueError:
		report_error = "Invalid saved report reference"
	if report_path is not None:
		if not report_path.is_file():
			report_error = "Saved report file is missing"
		else:
			try:
				report = load_report(report_path)
			except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
				report_error = "Saved report is invalid or incompatible"
	children = service.children(run_id, include_invalid=True)
	try:
		performance = None if report is None else performance_series(report)
	except (ValueError, KeyError, TypeError, AttributeError):
		performance = None
		if report_error is None:
			report_error = "Saved report is invalid or incompatible"
	return RunInspection(manifest, report, report_path, report_error, children, performance)


def open_saved_run(service: RunDirectoryService, run_id: str, *, validate: bool = False) -> RunInspection:
	"""Alias used by viewer/CLI adapters for explicit run opening."""

	return inspect_saved_run(service, run_id, validate=validate)


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
	if isinstance(value, Mapping):
		result: dict[str, object] = {}
		for key in sorted(value):
			child = f"{prefix}.{key}" if prefix else key
			result.update(_flatten(value[key], child))
		return result
	if isinstance(value, list):
		return {prefix: value}
	return {prefix: value}


def configuration_differences(left: RunInspection | RunManifest, right: RunInspection | RunManifest) -> tuple[ConfigurationDifference, ...]:
	"""Compare user/resolved/specification fields while ignoring run identity."""

	left_manifest = left.manifest if isinstance(left, RunInspection) else left
	right_manifest = right.manifest if isinstance(right, RunInspection) else right
	left_value = {"original": left_manifest.original_configuration, "resolved": left_manifest.resolved_configuration, "identity": left_manifest.identity, "resolution": left_manifest.resolution, "universe": left_manifest.universe, "specifications": left_manifest.specifications}
	right_value = {"original": right_manifest.original_configuration, "resolved": right_manifest.resolved_configuration, "identity": right_manifest.identity, "resolution": right_manifest.resolution, "universe": right_manifest.universe, "specifications": right_manifest.specifications}
	left_flat, right_flat = _flatten(left_value), _flatten(right_value)
	paths = sorted(set(left_flat) | set(right_flat))
	return tuple(ConfigurationDifference(path, left_flat.get(path, _MISSING), right_flat.get(path, _MISSING)) for path in paths if left_flat.get(path, _MISSING) != right_flat.get(path, _MISSING))


def _comparison_payload(inspection: RunInspection) -> dict[str, object]:
	manifest = inspection.manifest
	value: dict[str, object] = {
		"identity": {key: item for key, item in manifest.identity.items() if key not in ("execution_revision",)},
		"resolution": dict(manifest.resolution),
		"universe": dict(manifest.universe),
		"specifications": dict(manifest.specifications),
	}
	if inspection.report is not None:
		fields = inspection.report.payload
		for name in ("source", "features", "target", "calendar", "start_session", "end_session", "decision_lead_seconds", "execution", "eligibility", "top_k"):
			if name in fields:
				value[name] = fields[name]
		initial_portfolio = fields.get("initial_portfolio")
		if isinstance(initial_portfolio, Mapping):
			value["initial_capital"] = _mapping(initial_portfolio, "initial portfolio").get("initial_capital")
		allocation = fields.get("allocation")
		if isinstance(allocation, Mapping):
			allocation_value = dict(allocation)
			allocation_value.pop("policy", None)
			allocation_value.pop("cash_rest", None)
			allocation_value.pop("rebalance", None)
			value["allocation_anchor"] = allocation_value
	# Schedule policy is intentionally a comparison dimension, not a model
	# compatibility dimension.  It remains visible in configuration_differences.
	specifications = _mapping(value.get("specifications", {}), "specifications")
	without_schedule = dict(specifications)
	without_schedule.pop("allocation", None)
	value["specifications"] = without_schedule
	return value


def compare_saved_runs(inspections: Sequence[RunInspection]) -> RunComparison:
	"""Compare saved runs and prevent incompatible performance aggregation."""

	items = tuple(inspections)
	if len(items) < 2:
		raise ValueError("Run comparison requires at least two runs")
	baseline = _comparison_payload(items[0])
	differences: list[ConfigurationDifference] = []
	for item in items[1:]:
		candidate = _comparison_payload(item)
		left_flat, right_flat = _flatten(baseline), _flatten(candidate)
		for path in sorted(set(left_flat) | set(right_flat)):
			if left_flat.get(path, _MISSING) != right_flat.get(path, _MISSING):
				differences.append(ConfigurationDifference(path, left_flat.get(path, _MISSING), right_flat.get(path, _MISSING)))
	compatible = not differences
	return RunComparison(items, compatible, tuple(differences))


def compare_saved_run_ids(service: RunDirectoryService, run_ids: Sequence[str], *, validate: bool = False) -> RunComparison:
	return compare_saved_runs(tuple(inspect_saved_run(service, run_id, validate=validate) for run_id in run_ids))


def _positions(row: Mapping[str, object]) -> tuple[dict[str, object], ...]:
	value = row.get("positions", [])
	if not isinstance(value, list):
		return ()
	return tuple(_mapping(item, "position") for item in value)


def performance_series(document: ReportDocument) -> PerformanceSeries | None:
	"""Derive chart/table series from a simulation report, including v1/v2."""

	if document.kind not in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
		return None
	fields = document.payload
	equity_rows = object_rows(fields.get("equity"))
	initial = _decimal(_mapping(fields.get("initial_portfolio"), "initial portfolio").get("initial_capital"), "initial capital")
	peak = initial
	equity: list[dict[str, object]] = []
	drawdown: list[dict[str, object]] = []
	subportfolios: list[dict[str, object]] = []
	incomplete_marks: list[dict[str, object]] = []
	open_positions: list[dict[str, object]] = []
	for row in equity_rows:
		session = _text(row.get("session"), "equity session")
		total = _decimal(row.get("total_equity"), "total equity")
		peak = max(peak, total)
		current_drawdown = Decimal(0) if peak == 0 else (peak - total) / peak
		marks_current = row.get("marks_current") is True
		equity.append({"session": session, "total_equity": str(total), "marks_current": marks_current})
		drawdown.append({"session": session, "drawdown": str(current_drawdown), "marks_current": marks_current})
		allocations = row.get("allocations")
		if isinstance(allocations, list):
			for allocation_value in allocations:
				allocation = _mapping(allocation_value, "allocation")
				available = _decimal(allocation.get("available_cash"), "available cash")
				resting = _decimal(allocation.get("resting_proceeds"), "resting proceeds")
				subportfolios.append({"session": session, "name": allocation.get("name"), "available_cash": str(available), "resting_proceeds": str(resting), "cash": str(available + resting), "equity": str(_decimal(allocation.get("equity"), "subportfolio equity")), "positions_value": str(_decimal(allocation.get("positions_value"), "positions value"))})
		for position in _positions(row):
			if position.get("mark_current") is not True:
				incomplete_marks.append({"session": session, "allocation": position.get("allocation"), "ticker_id": position.get("ticker_id"), "reason": "position mark is not current"})
		if not marks_current:
			incomplete_marks.append({"session": session, "reason": "valuation contains incomplete marks"})
		positions = sum(len(_positions(_mapping(item, "allocation"))) for item in allocations) if isinstance(allocations, list) else 0
		if positions:
			open_positions.append({"session": session, "count": positions})
	missing_data: list[dict[str, object]] = []
	frames = fields.get("execution_frames", fields.get("execution_evidence", []))
	if isinstance(frames, list):
		for frame_value in frames:
			frame = _mapping(frame_value, "execution frame")
			issues = frame.get("issues")
			for issue_value in issues if isinstance(issues, list) else []:
				issue = _mapping(issue_value, "execution issue")
				missing_data.append({"session": frame.get("session"), **issue})
	benchmark_values = fields.get("benchmarks")
	benchmark_items: list[object] = benchmark_values if isinstance(benchmark_values, list) else []
	benchmarks = tuple(_mapping(item, "benchmark") for item in benchmark_items)
	final = _mapping(fields.get("final_portfolio"), "final portfolio")
	final_allocations = final.get("allocations")
	allocation_values: list[object] = final_allocations if isinstance(final_allocations, list) else []
	for allocation_value in allocation_values:
		allocation = _mapping(allocation_value, "final allocation")
		for position in _positions(allocation):
			open_positions.append({"session": equity[-1]["session"] if equity else None, "allocation": allocation.get("name"), "ticker_id": position.get("ticker_id"), "quantity": position.get("quantity"), "marked_price": position.get("marked_price"), "mark_current": position.get("mark_current")})
	return PerformanceSeries(tuple(equity), tuple(drawdown), tuple(subportfolios), benchmarks, tuple(incomplete_marks), tuple(missing_data), tuple(open_positions))


def _report_scores(report: ReportDocument) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...], dict[str, object]]:
	fields = report.payload
	if report.kind in (ReportKind.RECOMMENDATION, ReportKind.RECOMMENDATION_V2):
		return object_rows(fields.get("scores")), object_rows(fields.get("ranking")), {key: fields.get(key) for key in ("score_units", "top_k", "eligible_count", "median_eligible_score", "median_returned_score")}
	if report.kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
		full: list[dict[str, object]] = []
		top: list[dict[str, object]] = []
		decisions = fields.get("decisions")
		for decision_value in decisions if isinstance(decisions, list) else []:
			decision = _mapping(decision_value, "decision")
			entry = decision.get("entry_session")
			for score in object_rows(decision.get("scores")):
				full.append({"entry_session": entry, **score})
			for score in object_rows(decision.get("ranking")):
				top.append({"entry_session": entry, **score})
		target = fields.get("target")
		target_fields = _mapping(target, "target") if isinstance(target, Mapping) else {}
		kind = target_fields.get("kind")
		return tuple(full), tuple(top), {"score_units": "fractional_return" if kind in (None, "raw_return") else "z_score", "top_k": fields.get("top_k")}
	return (), (), {}


def _training_metrics(report: ReportDocument | None, manifest: RunManifest) -> dict[str, object]:
	if report is not None and report.kind is ReportKind.TRAINING:
		return {key: report.payload.get(key) for key in ("training_mae", "training_rmse", "training_objective_loss", "evaluation_mae", "evaluation_rmse", "evaluation_objective_loss", "target_metric_units", "objective_loss_units", "training_metric_scope", "evaluation_kind", "evaluation_samples", "raw_return_metrics_available") if key in report.payload}
	if report is not None and report.kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
		training = []
		training_values = report.payload.get("training_runs")
		training_items: list[object] = training_values if isinstance(training_values, list) else []
		for item in training_items:
			row = _mapping(item, "training run")
			if isinstance(row.get("result"), Mapping):
				training.append(row["result"])
		return {"training": training}
	metrics = manifest.results.get("metrics")
	return {} if metrics is None else _mapping(metrics, "manifest metrics")


def run_view(inspection: RunInspection) -> dict[str, object]:
	"""Build a JSON-safe view containing every result family needed by viewers."""

	manifest = inspection.manifest
	report = inspection.report
	fields = {} if report is None else report.payload
	full_scores, top_scores, score_statistics = ((), (), {}) if report is None else _report_scores(report)
	resolved_dates: dict[str, object] = dict(manifest.resolution)
	if not resolved_dates:
		for key in ("start_session", "end_session", "entry_session", "exit_session", "decision_cutoff", "timing"):
			if key in fields:
				resolved_dates[key] = fields[key]
	universe: dict[str, object] = dict(manifest.universe)
	if not universe and isinstance(fields.get("features"), Mapping):
		features = _mapping(fields["features"], "features")
		universe = {"ticker_ids": features.get("ticker_ids"), "ticker_labels": [row.get("symbol") for row in full_scores if row.get("symbol") is not None]}
	results = _mapping(manifest.results, "manifest results")
	target_value = fields.get("target", manifest.specifications.get("target"))
	score_units = fields.get("score_units")
	if score_units is None and isinstance(target_value, Mapping):
		target_fields = _mapping(target_value, "target")
		kind = target_fields.get("kind")
		score_units = "fractional_return" if kind in (None, "raw_return") else "z_score"
	exclusions = results.get("exclusions") or []
	if not exclusions:
		if report is not None and report.kind in (ReportKind.RECOMMENDATION, ReportKind.RECOMMENDATION_V2):
			exclusions = [row for row in object_rows(fields.get("eligibility")) if row.get("state") != "eligible"]
		elif report is not None and report.kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
			decisions = fields.get("decisions")
			exclusions = [row for decision in decisions if isinstance(decision, Mapping) for row in decision.get("eligibility", []) if isinstance(row, Mapping) and row.get("state") != "eligible"] if isinstance(decisions, list) else []
	return {
		"run_id": manifest.run_id,
		"operation": manifest.operation.value,
		"status": manifest.status.value,
		"report_state": manifest.report_state.value,
		"resolved_dates": resolved_dates,
		"universe": universe,
		"configuration": {"original": dict(manifest.original_configuration), "resolved": None if manifest.resolved_configuration is None else dict(manifest.resolved_configuration)},
		"specifications": dict(manifest.specifications),
		"feature_target_units": {"score_units": score_units, "target": target_value, "features": fields.get("features", manifest.specifications.get("features"))},
		"normalization_loss": {"preprocessing": _mapping(manifest.specifications.get("preprocessing"), "preprocessing") if isinstance(manifest.specifications.get("preprocessing"), Mapping) else None, "loss": _mapping(manifest.specifications.get("loss"), "loss") if isinstance(manifest.specifications.get("loss"), Mapping) else None},
		"metrics": _training_metrics(report, manifest),
		"full_scores": list(full_scores),
		"top_scores": list(top_scores),
		"score_statistics": score_statistics,
		"exclusions": exclusions,
		"trades": fields.get("realized_trades", results.get("trades", [])),
		"equity": fields.get("equity", results.get("equity", [])),
		"benchmarks": fields.get("benchmarks", results.get("benchmarks", [])),
		"performance": None if inspection.performance is None else inspection.performance.to_mapping(),
		"failure": None if manifest.failure is None else manifest.failure.to_mapping(),
		"incomplete_report": None if manifest.incomplete_report is None else dict(manifest.incomplete_report),
	}


__all__ = [
	"ConfigurationDifference",
	"PerformanceSeries",
	"RunComparison",
	"RunInspection",
	"compare_saved_run_ids",
	"compare_saved_runs",
	"configuration_differences",
	"inspect_saved_run",
	"list_saved_runs",
	"open_saved_run",
	"performance_series",
	"run_view",
]
