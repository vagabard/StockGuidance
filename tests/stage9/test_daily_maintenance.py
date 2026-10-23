from datetime import date
from types import SimpleNamespace

from stockguidance.cli.maintain_daily import LOOKBACK_YEARS, ZSCORE_WINDOWS, maintenance_range, maintain, parser
from stockguidance.cli.update_arguments import DEFAULT_ARCHIVE_REFRESH_DAYS, DEFAULT_CORRECTION_EVIDENCE, DEFAULT_PUBLICATION_DELAY_MINUTES, DEFAULT_PUBLICATION_EVIDENCE, DEFAULT_RECENT_REFRESH_HOURS, DEFAULT_RECENT_WINDOW_DAYS


def test_daily_publication_policy_has_overridable_defaults():
	arguments = parser().parse_args([
		"--recent-window-days", "30", "--recent-refresh-hours", "1", "--archive-refresh-days", "30",
		"--correction-evidence", "operator policy",
	])
	assert arguments.publication_delay_minutes == DEFAULT_PUBLICATION_DELAY_MINUTES
	assert arguments.publication_evidence == DEFAULT_PUBLICATION_EVIDENCE
	assert arguments.recent_window_days == DEFAULT_RECENT_WINDOW_DAYS
	assert arguments.recent_refresh_hours == DEFAULT_RECENT_REFRESH_HOURS
	assert arguments.archive_refresh_days == DEFAULT_ARCHIVE_REFRESH_DAYS
	assert arguments.correction_evidence == DEFAULT_CORRECTION_EVIDENCE


def test_maintenance_range_uses_two_calendar_years_and_handles_leap_day():
	assert LOOKBACK_YEARS == 2
	assert maintenance_range(date(2026, 10, 23)) == (date(2024, 10, 23), date(2026, 10, 23))
	assert maintenance_range(date(2024, 2, 29)) == (date(2022, 2, 28), date(2024, 2, 29))


def test_daily_maintenance_reuses_the_same_bounded_range_for_both_stages():
	arguments = parser().parse_args([
		"--publication-delay-minutes", "15", "--publication-evidence", "fixture",
		"--recent-window-days", "30", "--recent-refresh-hours", "1", "--archive-refresh-days", "30",
		"--correction-evidence", "fixture",
	])
	calls = []

	def update(update_arguments):
		calls.append(("market", update_arguments.start_date, update_arguments.end_date))
		return SimpleNamespace(successful=True, downloaded_sessions=1, stale_refreshes=(), failures=())

	def update_zscores(zscore_arguments):
		calls.append(("zscores", zscore_arguments.start_date, zscore_arguments.end_date))
		return SimpleNamespace(exit_code=0, complete=True, written_sessions=(), rebuilt_results=(), unavailable_count=0, failures=())

	payload = maintain(arguments, update=update, update_zscores=update_zscores)
	assert payload["successful"]
	assert payload["lookback_years"] == 2
	assert calls[0][1:] == calls[1][1:]
	assert arguments.windows == ZSCORE_WINDOWS


def test_daily_maintenance_does_not_update_zscores_after_reconciliation_failure():
	arguments = parser().parse_args([
		"--publication-delay-minutes", "15", "--publication-evidence", "fixture",
		"--recent-window-days", "30", "--recent-refresh-hours", "1", "--archive-refresh-days", "30",
		"--correction-evidence", "fixture",
	])
	zscore_calls = []

	def update(_):
		return SimpleNamespace(successful=False, downloaded_sessions=0, stale_refreshes=(), failures=("fixture failure",))

	def update_zscores(_):
		zscore_calls.append(True)
		raise AssertionError("dependent update must not run")

	payload = maintain(arguments, update=update, update_zscores=update_zscores)
	assert not payload["successful"]
	assert payload["zscores"]["skipped"]
	assert not zscore_calls
