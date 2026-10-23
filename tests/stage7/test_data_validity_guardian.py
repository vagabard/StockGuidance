from datetime import date

import pytest

from stage7.test_zscore_validation import _populate_zscores

from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.validity import DataValidityWarning, MINIMUM_VALID_SESSION, enforce_minimum_valid_session
from stockguidance.validation.findings import ValidationCheck


def test_production_data_floor_rejects_earlier_ranges(harness):
	with pytest.raises(ValueError, match="2006-11-16"):
		enforce_minimum_valid_session(harness.source, date(2006, 11, 15))
	enforce_minimum_valid_session(harness.source, MINIMUM_VALID_SESSION)


def test_requested_guardian_warns_for_non_ready_zscore_rows(harness, calendar):
	_populate_zscores(harness, calendar)
	date_id = harness.database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", (date(2024, 7, 3),))[0][0]
	harness.database.sql("UPDATE zscores_price_2 SET zscore = NULL, status = 5 WHERE date_id = ? AND ticker_id = 1", (date_id,))
	assert harness.database.sql("SELECT status FROM zscores_price_2 WHERE date_id = ? AND ticker_id = 1", (date_id,)) == [(5,)]
	query = SessionQuery(harness.boundary, (calendar.session(date(2024, 7, 3)),), harness.clock.now())
	repository = MySQLValidationRepository(harness.source, harness.database, guardian_windows=(2,))
	with pytest.warns(DataValidityWarning, match="price window 2"):
		inspection = repository.inspect(query)
	assert not [finding for finding in inspection.findings if finding.check is ValidationCheck.SCHEMA]
