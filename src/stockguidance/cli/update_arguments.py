import argparse


def add_update_policy_arguments(arguments: argparse.ArgumentParser) -> None:
	arguments.add_argument("--history-start")
	arguments.add_argument("--history-evidence")
	arguments.add_argument("--adjustment", choices=("adjusted", "unadjusted"), default="adjusted")
	arguments.add_argument("--publication-delay-minutes", type=float, required=True)
	arguments.add_argument("--publication-evidence", required=True)
	arguments.add_argument("--recent-window-days", type=float, required=True)
	arguments.add_argument("--recent-refresh-hours", type=float, required=True)
	arguments.add_argument("--archive-refresh-days", type=float, required=True)
	arguments.add_argument("--correction-evidence", required=True)
	arguments.add_argument("--initialize-schema", action="store_true", help="Explicitly create missing updater tables; existing tables are never altered")
