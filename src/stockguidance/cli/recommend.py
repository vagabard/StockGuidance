from stockguidance.database.prepared_history import PreparationRequired
import argparse
import json
from pathlib import Path
import re
import sys
from dataclasses import replace
from decimal import Decimal,InvalidOperation
from stockguidance.universe.endpoints import EndpointSelection

from stockguidance.workflows.flexible_recommendation import load_setup, assemble_flexible_recommendation

from stockguidance.recommendations.jobs import load_configuration
from stockguidance.workflows.recommendation_runtime import assemble_and_recommend
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.recommendations.configuration import ArtifactKind, RefreshMode
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.reporting.recommendations import recommendation_payload, render_recommendations
from stockguidance.workflows.failures import StageExecutionError


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Refresh and validate explicit previous_close or entry_open signals, then rank close-to-next-session-close suggestions.")
	parser.add_argument("--config", type=Path, help="Optional recommendation/model settings JSON")
	parser.add_argument("--start-date", help="ISO date or offset from end, e.g. -1y or -30d")
	parser.add_argument("--end-date", help="ISO data endpoint; defaults to today")
	parser.add_argument("--tickers", nargs="+", help="Ordered symbols, separated by spaces or commas")
	parser.add_argument("--trim-dollar-volume-percent", type=str, help="Remove the bottom percentage by average shares times close; default 0")
	parser.add_argument("--volume-sample-sessions", type=int, help="Explicit evenly spaced sample size, at least 2; default all sessions")
	parser.add_argument("--minimum-transactions", type=int, help="Optional previous-session trade-count minimum; default 0")
	parser.add_argument("--dbinfo-file", type=Path)
	parser.add_argument("--data-root", type=Path)
	parser.add_argument("--runs-root", type=Path)
	parser.add_argument("--policy", default="retrain", choices=[policy.value for policy in ModelRefreshPolicy])
	parser.add_argument("--artifact", type=Path, help="Explicit current artifact or optional retraining destination")
	parser.add_argument("--artifact-id", help="Required SHA256 identity in current mode")
	parser.add_argument("--artifact-kind", choices=[kind.value for kind in ArtifactKind], default=ArtifactKind.MODEL.value)
	parser.add_argument("--refresh-mode", choices=[mode.value for mode in RefreshMode], default=RefreshMode.REFRESH.value, help="refresh source data or validate already updated data without provider calls")
	parser.add_argument("--max-retries", type=int, default=5)
	parser.add_argument("--format", choices=("json", "text"), default="json")
	capital = parser.add_mutually_exclusive_group()
	capital.add_argument("--portfolio-state", type=Path, help="Actual portfolio JSON snapshot for cash-rest order planning")
	capital.add_argument("--capital", help="Actual initial cash at the allocation anchor, as a decimal string")
	parser.add_argument("--fee-per-fill", default="0.00", help="Fixed fee for each simulated/planned fill, as whole cents")
	return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = build_parser()
	values = list(sys.argv[1:] if argv is None else argv)
	# argparse otherwise interprets -1y as an option rather than a date value.
	for index in range(len(values)-2,-1,-1):
		if values[index]=="--start-date" and re.fullmatch(r"-\d+[yd]",values[index+1]):
			values[index:index+2] = [f"--start-date={values[index+1]}"]
	arguments = parser.parse_args(values)
	if arguments.config is None and arguments.start_date is None:
		parser.error("Provide --start-date or --config")
	try:
		EndpointSelection(tickers=None if arguments.tickers is None else tuple(v for item in arguments.tickers for v in item.split(",") if v),trim_percent=Decimal(arguments.trim_dollar_volume_percent or "0"),sample_sessions=arguments.volume_sample_sessions)
		if arguments.minimum_transactions is not None and arguments.minimum_transactions<0:
			raise ValueError("Minimum transactions must be nonnegative")
	except (ValueError,InvalidOperation) as error:
		parser.error(str(error))
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
	if arguments.policy == ModelRefreshPolicy.CURRENT.value and arguments.artifact is None:
		parser.error("current requires --artifact")
	if arguments.policy == ModelRefreshPolicy.RETRAIN.value and (arguments.artifact_id is not None or arguments.artifact_kind != ArtifactKind.MODEL.value):
		parser.error("retrain produces one model artifact; omit --artifact-id and --artifact-kind ensemble")
	return arguments


def main(argv: list[str] | None = None) -> int:
	arguments = parse_args(argv)
	try:
		template = {} if arguments.config is None else json.loads(arguments.config.read_text(encoding="utf-8"))
		flexible = "entry_date" not in template or any(getattr(arguments,name) is not None for name in ("start_date","end_date","tickers","trim_dollar_volume_percent","volume_sample_sessions","minimum_transactions","dbinfo_file","data_root"))
		if flexible:
			tickers = None if arguments.tickers is None else tuple(v for item in arguments.tickers for v in item.split(",") if v)
			setup = load_setup(arguments.config,start=arguments.start_date,end=arguments.end_date,tickers=tickers,trim=arguments.trim_dollar_volume_percent,sample=arguments.volume_sample_sessions,minimum_transactions=arguments.minimum_transactions,dbinfo=arguments.dbinfo_file,data_root=arguments.data_root,runs_root=arguments.runs_root,automatic_artifact=arguments.artifact is None)
		else:
			configuration = load_configuration(arguments.config)
			if (configuration.retraining is not None) != (arguments.policy == ModelRefreshPolicy.RETRAIN.value):
				raise ValueError("Policy and training configuration differ")
			if arguments.runs_root is not None:
				configuration = replace(configuration,runs_root=arguments.runs_root)
			if arguments.artifact is None:
				configuration = replace(configuration,resolution_metadata={"automatic_artifact":True})
			print("Dollar-volume filtering: 0.000s; disabled for the legacy explicit configuration",file=sys.stderr)
	except Exception as error:
		message = "Invalid recommendation configuration; entry_open requires its explicit opening feature" if "entry_open" in str(error) else "Invalid recommendation configuration"
		print(json.dumps({"successful": False, "stage": "request", "failure": "invalid_request", "message": message}))
		return 2
	try:
		options = RecommendationOptions(ModelRefreshPolicy(arguments.policy), arguments.artifact or Path("model.sgmodel"), arguments.artifact_id, ArtifactKind(arguments.artifact_kind), arguments.max_retries, arguments.portfolio_state, arguments.capital, arguments.fee_per_fill, RefreshMode(arguments.refresh_mode))
		result = assemble_flexible_recommendation(setup,options) if flexible else assemble_and_recommend(configuration, options)
		output = json.dumps(recommendation_payload(result), sort_keys=True, allow_nan=False) if arguments.format == "json" else render_recommendations(result)
		print(output)
		return 0
	except PreparationRequired as error:
		print(json.dumps({"successful":False,"stage":"data","failure":"database_preparation_required","message":str(error)}))
		return 1
	except StageExecutionError as error:
		print(json.dumps({"successful": False, "stage": error.failure.stage.value, "failure": error.failure.code.value, "message": error.failure.message}))
		return 1
	except Exception:
		print(json.dumps({"successful": False, "stage": "request", "failure": "recommendation_failed", "message": "Required recommendation setup or reporting failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
