from stockguidance.cli.simulate import main as simulate_main, parse_args
from stockguidance.simulation.configuration import EvaluationPolicy


def main(argv: list[str] | None = None) -> int:
	arguments = parse_args(argv)
	if arguments.model_policy != EvaluationPolicy.CHRONOLOGICAL_RETRAIN.value:
		from stockguidance.cli.simulate import build_parser

		build_parser().error("Incremental evaluation requires --model-policy chronological_retrain")
	return simulate_main(argv)


if __name__ == "__main__":
	raise SystemExit(main())
