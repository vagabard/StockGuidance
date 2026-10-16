import argparse
import json
from pathlib import Path

from stockguidance.cli.recommendation_configuration import load_configuration
from stockguidance.cli.recommendation_setup import assemble_and_recommend
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.reporting.recommendations import recommendation_payload, render_recommendations
from stockguidance.workflows.failures import StageExecutionError


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Refresh and validate previous-close data, then rank close-to-next-session-close suggestions.")
	parser.add_argument("--config", required=True, type=Path, help="Explicit recommendation JSON; see docs/recommendations.md")
	parser.add_argument("--policy", required=True, choices=[policy.value for policy in ModelRefreshPolicy])
	parser.add_argument("--artifact", required=True, type=Path, help="Explicit current artifact or retraining output file")
	parser.add_argument("--artifact-id", help="Required SHA256 identity in current mode")
	parser.add_argument("--artifact-kind", choices=[kind.value for kind in ArtifactKind], default=ArtifactKind.MODEL.value)
	parser.add_argument("--max-retries", type=int, default=5)
	parser.add_argument("--format", choices=("json", "text"), default="json")
	capital = parser.add_mutually_exclusive_group()
	capital.add_argument("--portfolio-state", type=Path, help="Actual portfolio JSON snapshot for cash-rest order planning")
	capital.add_argument("--capital", help="Actual initial cash at the allocation anchor, as a decimal string")
	parser.add_argument("--fee-per-fill", default="0.00", help="Fixed fee for each simulated/planned fill, as whole cents")
	return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = build_parser()
	arguments = parser.parse_args(argv)
	try:
		ExecutionPolicy(decimal_text(arguments.fee_per_fill))
		if arguments.capital is not None and decimal_text(arguments.capital) <= 0:
			raise ValueError("Nonpositive capital")
	except Exception:
		parser.error("Capital and fees require valid decimal strings; fees use nonnegative whole cents")
	if arguments.max_retries <= 0:
		parser.error("--max-retries must be positive")
	if arguments.policy == ModelRefreshPolicy.CURRENT.value and (arguments.artifact_id is None or len(arguments.artifact_id) != 64 or any(character not in "0123456789abcdef" for character in arguments.artifact_id)):
		parser.error("current requires --artifact-id as a SHA256 digest")
	if arguments.policy == ModelRefreshPolicy.RETRAIN.value and (arguments.artifact_id is not None or arguments.artifact_kind != ArtifactKind.MODEL.value):
		parser.error("retrain produces one model artifact; omit --artifact-id and --artifact-kind ensemble")
	return arguments


def main(argv: list[str] | None = None) -> int:
	arguments = parse_args(argv)
	try:
		configuration = load_configuration(arguments.config)
		if (configuration.retraining is not None) != (arguments.policy == ModelRefreshPolicy.RETRAIN.value):
			raise ValueError("Policy and training configuration differ")
	except Exception as error:
		message = "entry_open is reserved for Stage 10 and is not implemented" if str(error) == "entry_open is reserved for Stage 10 and is not implemented" else "Invalid recommendation configuration"
		print(json.dumps({"successful": False, "stage": "request", "failure": "invalid_request", "message": message}))
		return 2
	try:
		result = assemble_and_recommend(configuration, arguments)
		output = json.dumps(recommendation_payload(result), sort_keys=True, allow_nan=False) if arguments.format == "json" else render_recommendations(result)
		print(output)
		return 0
	except StageExecutionError as error:
		print(json.dumps({"successful": False, "stage": error.failure.stage.value, "failure": error.failure.code.value, "message": error.failure.message}))
		return 1
	except Exception:
		print(json.dumps({"successful": False, "stage": "request", "failure": "recommendation_failed", "message": "Required recommendation setup or reporting failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
