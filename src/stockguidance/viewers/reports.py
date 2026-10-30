from pathlib import Path

from stockguidance.reporting.documents import ReportKind, load_report
from stockguidance.reporting.inspection import compare_saved_runs, inspect_saved_run, list_saved_runs
from stockguidance.reporting.metrics import compare_simulations
from stockguidance.runs.contracts import RunOperation, RunStatus
from stockguidance.runs.service import RunDirectoryService


def _show_performance(st: object, document: object) -> None:
	"""Render saved performance data when the optional chart API is present."""
	if not hasattr(document, "kind") or getattr(document, "kind") not in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
		return
	from stockguidance.reporting.inspection import performance_series

	performance = performance_series(document)  # type: ignore[arg-type]
	if performance is None:
		return
	dataframe = getattr(st, "dataframe", None)
	if callable(dataframe):
		dataframe(list(performance.equity))
		dataframe(list(performance.subportfolios))
		dataframe(list(performance.drawdown))
		dataframe(list(performance.benchmarks))
	chart = getattr(st, "line_chart", None)
	if callable(chart) and performance.equity:
		chart(list(performance.equity))
	if callable(chart) and performance.drawdown:
		chart(list(performance.drawdown))
	write = getattr(st, "write", None)
	if callable(write):
		write({"incomplete_marks": list(performance.incomplete_marks), "missing_data": list(performance.missing_data), "open_positions": list(performance.open_positions)})


def _show_saved_runs(st: object) -> None:
	"""Optional runs-root surface; omitted by small explicit-report test doubles."""
	text_input = getattr(st, "text_input", None)
	selectbox = getattr(st, "selectbox", None)
	if not callable(text_input) or not callable(selectbox):
		return
	runs_root = Path(str(text_input("Runs root", "runs")))
	operation_value = str(selectbox("Operation", ["all", *(item.value for item in RunOperation)]))
	status_value = str(selectbox("Status", ["all", *(item.value for item in RunStatus)]))
	service = RunDirectoryService(runs_root)
	items = list_saved_runs(service, operation=None if operation_value == "all" else RunOperation(operation_value), status=None if status_value == "all" else RunStatus(status_value))
	if not items:
		info = getattr(st, "info", None)
		if callable(info):
			info("No saved runs match the selected filters.")
		return
	selected_id = str(selectbox("Saved run", [item.run_id for item in items]))
	inspection = inspect_saved_run(service, selected_id)
	write = getattr(st, "write", None)
	if callable(write):
		write(inspection.to_mapping()["view"])
	multiselect = getattr(st, "multiselect", None)
	if callable(multiselect) and len(items) > 1:
		selected_ids = tuple(str(value) for value in multiselect("Compare saved runs", [item.run_id for item in items], default=[selected_id]))
		if len(selected_ids) > 1:
			comparison = compare_saved_runs(tuple(inspect_saved_run(service, run_id) for run_id in selected_ids))
			if callable(write):
				write(comparison.to_mapping())
	if inspection.report_error is not None:
		error = getattr(st, "error", None)
		if callable(error):
			error(inspection.report_error)
	if inspection.children:
		child_table = [{"run_id": child.run_id, "status": None if child.manifest is None else child.manifest.status.value, "operation": None if child.manifest is None else child.manifest.operation.value, "valid": child.valid} for child in inspection.children]
		dataframe = getattr(st, "dataframe", None)
		if callable(dataframe):
			dataframe(child_table)
	if inspection.report is not None:
		_show_performance(st, inspection.report)


def main() -> None:
	import streamlit as st

	st.set_page_config(page_title="StockGuidance reports", layout="wide")
	st.title("Close-to-close run reports")
	paths = st.text_area("Explicit report JSON paths, one per line")
	if not paths.strip():
		return
	try:
		documents = tuple(load_report(Path(value.strip())) for value in paths.splitlines() if value.strip())
		simulations = tuple(document for document in documents if document.kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2))
		if simulations:
			st.dataframe([summary.payload() for summary in compare_simulations(simulations)])
		for document in documents:
			st.subheader(str(document.path))
			fields = document.payload
			st.write({name: fields[name] for name in ("schema", "successful", "complete", "model_policy", "refresh_mode", "artifact_id", "training_metric_scope", "training_mae", "evaluation_kind", "evaluation_mae", "score_units", "eligible_count", "median_eligible_score", "median_returned_score", "entry_session", "exit_session", "decision_cutoff", "allocation", "fills_recorded", "ranking") if name in fields})
			if document.kind is ReportKind.SIMULATION:
				st.dataframe(fields["equity"])
				st.dataframe(fields["benchmarks"])
				_show_performance(st, document)
			elif document.kind is ReportKind.SIMULATION_V2:
				_show_performance(st, document)
			elif document.kind is ReportKind.TRAINING:
				st.dataframe(fields["epochs"])
			elif document.kind in (ReportKind.RECOMMENDATION, ReportKind.RECOMMENDATION_V2):
				st.dataframe(fields.get("scores", []))
				st.dataframe(fields.get("ranking", []))
	except Exception:
		st.error("Select valid current reports with matching specifications for comparison.")
	_show_saved_runs(st)


if __name__ == "__main__":
	main()
