import argparse
import json
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.reporting.artifacts import artifact_payload
from stockguidance.reporting.documents import ReportKind, load_report
from stockguidance.reporting.metrics import compare_simulations, simulation_summary


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Inspect explicit current JSON reports or an identified inference artifact; no directory discovery or database access.")
	commands = parser.add_subparsers(dest="command", required=True)
	for command in ("summary", "compare"):
		subparser = commands.add_parser(command)
		subparser.add_argument("reports", nargs="+", type=Path)
	artifact = commands.add_parser("artifact")
	artifact.add_argument("--artifact", type=Path, required=True)
	artifact.add_argument("--artifact-id", required=True)
	arguments = parser.parse_args(argv)
	try:
		if arguments.command == "artifact":
			payload: object = artifact_payload(ArtifactReference(arguments.artifact, arguments.artifact_id))
		else:
			documents = tuple(load_report(path) for path in arguments.reports)
			if arguments.command == "compare":
				payload = [summary.payload() for summary in compare_simulations(documents)]
			else:
				payload = [simulation_summary(document).payload() if document.kind in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2) else {"report": str(document.path), "schema": document.kind.value, "successful": document.payload["successful"], "details": {key: document.payload[key] for key in ("training_mae", "training_metric_scope", "evaluation_kind", "evaluation_mae", "artifact", "artifact_id", "model_policy", "refresh_mode", "entry_session", "exit_session", "fills_recorded", "score_units", "eligible_count", "median_eligible_score", "median_returned_score", "scores", "ranking") if key in document.payload}} for document in documents]
		print(json.dumps({"successful": True, "results": payload}, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Invalid, incompatible or unavailable report/artifact"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
