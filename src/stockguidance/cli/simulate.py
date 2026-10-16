import argparse
import json
from pathlib import Path

from stockguidance.simulation.jobs import load_configuration
from stockguidance.workflows.simulation_runtime import assemble_and_simulate
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.reporting.simulation import render_simulation, simulation_payload
from stockguidance.simulation.configuration import EvaluationPolicy
from stockguidance.simulation.options import SimulationOptions


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Chronological entry-close to next-session-close simulation with mandatory cash rest.")
	parser.add_argument("--config", type=Path, required=True, help="Explicit simulation JSON; see docs/simulation.md")
	parser.add_argument("--model-policy", choices=[item.value for item in EvaluationPolicy], required=True)
	parser.add_argument("--artifact", type=Path, help="Explicit pre-period fixed model or ensemble")
	parser.add_argument("--artifact-id", help="SHA256 identity of the fixed artifact")
	parser.add_argument("--artifact-kind", choices=[item.value for item in ArtifactKind], default=ArtifactKind.MODEL.value)
	parser.add_argument("--artifacts-directory", type=Path, help="Explicit output directory for chronological retraining artifacts")
	parser.add_argument("--format", choices=("json", "text"), default="json")
	return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = build_parser()
	arguments = parser.parse_args(argv)
	if arguments.model_policy == EvaluationPolicy.FIXED.value:
		if arguments.artifact is None or arguments.artifact_id is None or len(arguments.artifact_id) != 64 or any(character not in "0123456789abcdef" for character in arguments.artifact_id) or arguments.artifacts_directory is not None:
			parser.error("fixed requires --artifact and its SHA256 --artifact-id; omit --artifacts-directory")
	elif arguments.artifact is not None or arguments.artifact_id is not None or arguments.artifact_kind != ArtifactKind.MODEL.value or arguments.artifacts_directory is None:
		parser.error("chronological_retrain requires --artifacts-directory and no existing artifact")
	return arguments


def main(argv: list[str] | None = None) -> int:
	arguments = parse_args(argv)
	try:
		configuration = load_configuration(arguments.config)
		if (configuration.retraining is not None) != (arguments.model_policy == EvaluationPolicy.CHRONOLOGICAL_RETRAIN.value):
			raise ValueError("Model policy and training configuration differ")
	except Exception as error:
		message = "entry_open is reserved for Stage 10 and is not implemented" if str(error) == "entry_open is reserved for Stage 10 and is not implemented" else "Invalid simulation configuration"
		print(json.dumps({"successful": False, "stage": "request", "message": message}))
		return 2
	try:
		options = SimulationOptions(EvaluationPolicy(arguments.model_policy), arguments.artifact, arguments.artifact_id, ArtifactKind(arguments.artifact_kind), arguments.artifacts_directory)
		result = assemble_and_simulate(configuration, options)
		output = json.dumps(simulation_payload(result), sort_keys=True, allow_nan=False) if arguments.format == "json" else render_simulation(result)
		print(output)
		return 0 if result.successful and result.complete else 2 if result.successful else 1
	except Exception:
		print(json.dumps({"successful": False, "stage": "simulation", "message": "Required historical data, model, execution or reporting failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
