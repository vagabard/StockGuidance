from pathlib import Path

from stockguidance.reporting.documents import ReportKind, load_report
from stockguidance.reporting.metrics import compare_simulations


def main() -> None:
	import streamlit as st

	st.set_page_config(page_title="StockGuidance reports", layout="wide")
	st.title("Close-to-close run reports")
	paths = st.text_area("Explicit report JSON paths, one per line")
	if not paths.strip():
		return
	try:
		documents = tuple(load_report(Path(value.strip())) for value in paths.splitlines() if value.strip())
		simulations = tuple(document for document in documents if document.kind is ReportKind.SIMULATION)
		if simulations:
			st.dataframe([summary.payload() for summary in compare_simulations(simulations)])
		for document in documents:
			st.subheader(str(document.path))
			fields = document.payload
			st.write({name: fields[name] for name in ("schema", "successful", "complete", "model_policy", "artifact_id", "training_metric_scope", "training_mae", "evaluation_kind", "evaluation_mae", "entry_session", "exit_session", "decision_cutoff", "allocation", "fills_recorded", "ranking") if name in fields})
			if document.kind is ReportKind.SIMULATION:
				st.dataframe(fields["equity"])
				st.dataframe(fields["benchmarks"])
			elif document.kind is ReportKind.TRAINING:
				st.dataframe(fields["epochs"])
	except Exception:
		st.error("Select valid current reports with matching specifications for comparison.")


if __name__ == "__main__":
	main()
