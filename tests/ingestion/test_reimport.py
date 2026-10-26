from datetime import date, timedelta
from dataclasses import replace

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.market_data.archive import archive_content, archive_path
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.reimport import apply_reimport, plan


def test_reimport_plan_uses_market_summary_and_reports_missing_dates(tmp_path, harness, calendar):
	data_root = tmp_path / "data"
	session = calendar.session(date(2024, 7, 30))
	response = replace(harness.response(), source_timestamps=(("AAA", int(session.opens_at.timestamp() * 1000)),))
	path = archive_path(data_root, session.session_date)
	path.parent.mkdir(parents=True)
	path.write_bytes(archive_content(DownloadedSession(session, response, session.closes_at + timedelta(minutes=1))))
	result = plan(data_root, date(2024, 7, 30), date(2024, 8, 2), calendar)
	assert [item.day.isoformat() for item in result.files] == ["2024-07-30"]
	assert [item.isoformat() for item in result.missing_dates] == ["2024-07-31", "2024-08-01", "2024-08-02"]
	assert result.start == date(2024, 7, 30)


def test_reimport_apply_requires_complete_canonical_coverage(tmp_path, harness):
	from stockguidance.market_data.reimport import ReimportPlan
	from datetime import date
	plan_value = ReimportPlan(date(2024, 7, 30), date(2024, 7, 31), (), (date(2024, 7, 31),), (date(2024, 7, 30), date(2024, 7, 31)))
	try:
		apply_reimport(harness.source, lambda _: None, None, "reimport", "backup completed", plan_value)
	except ValueError as error:
		assert "canonical dates are missing" in str(error)
	else:
		assert False, "apply must reject missing canonical dates"
