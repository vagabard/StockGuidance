import argparse


DEFAULT_PUBLICATION_DELAY_MINUTES = 15.0
DEFAULT_PUBLICATION_EVIDENCE = "Default operational policy; replace with provider-specific evidence when known"
DEFAULT_RECENT_WINDOW_DAYS = 30.0
DEFAULT_RECENT_REFRESH_HOURS = 0.0
DEFAULT_ARCHIVE_REFRESH_DAYS = 30.0
DEFAULT_CORRECTION_EVIDENCE = "Default daily maintenance correction policy"


def add_update_policy_arguments(arguments: argparse.ArgumentParser, *, include_schema_initialization: bool = True, include_history_boundary: bool = True) -> None:
	if include_history_boundary:
		arguments.add_argument("--history-start")
		arguments.add_argument("--history-evidence")
	arguments.add_argument("--adjustment", choices=("adjusted", "unadjusted"), default="adjusted")
	arguments.add_argument("--publication-delay-minutes", type=float, default=DEFAULT_PUBLICATION_DELAY_MINUTES)
	arguments.add_argument("--publication-evidence", default=DEFAULT_PUBLICATION_EVIDENCE)
	arguments.add_argument("--recent-window-days", type=float, default=DEFAULT_RECENT_WINDOW_DAYS)
	arguments.add_argument("--recent-refresh-hours", type=float, default=DEFAULT_RECENT_REFRESH_HOURS)
	arguments.add_argument("--archive-refresh-days", type=float, default=DEFAULT_ARCHIVE_REFRESH_DAYS)
	arguments.add_argument("--correction-evidence", default=DEFAULT_CORRECTION_EVIDENCE)
	if include_schema_initialization:
		arguments.add_argument("--initialize-schema", action="store_true", help="Explicitly create missing updater tables; existing tables are never altered")
