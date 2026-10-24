from dataclasses import dataclass

import pytest

from support.ingestion import calendar, harness
from support.samples import samples
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


@pytest.fixture
def validator(harness, calendar):
	@dataclass
	class Validator:
		def run(self, settings=None):
			selected = settings or ValidationSettings(harness.settings.start, harness.settings.end, harness.settings.publication)
			return validate_database(harness.source, selected, harness.boundary, calendar, MySQLValidationRepository(harness.source, harness.database), harness.clock)

	return Validator()
