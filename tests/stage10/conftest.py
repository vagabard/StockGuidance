from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from stage5.conftest import calendar, chronological_storage, harness, samples
from stage6.conftest import cpu_threads, training
from stage7.conftest import job_json, recommender
from stage8.conftest import simulation
from stage8.test_simulation_cli import simulation_json
from stage6.test_cli_and_workflow import job
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.features.specification import CloseFeature, SignalMode
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.openings import OpeningObservation


@pytest.fixture
def opening_signals(samples, tmp_path):
	store = OpeningStore(tmp_path / "openings")
	for observation in samples.repository.observations.values():
		session = observation.timing.session
		previous = samples.repository.observations.get((samples.calendar.shift(session, -1), observation.ticker_id))
		price = (previous or observation).close * (1.5 if observation.ticker_id == 17 else 0.5)
		store.save(samples.boundary.source, OpeningObservation(observation.ticker_id, f"T{observation.ticker_id}", session, price, session.opens_at + timedelta(minutes=2), content_id({"fixture": session.session_date.isoformat(), "ticker": observation.ticker_id})))
	features = replace(samples.specification, signal_mode=SignalMode.ENTRY_OPEN, feature_order=(CloseFeature.RELATIVE_CLOSE, CloseFeature.ENTRY_OPEN_RETURN))
	return SimpleNamespace(samples=samples, store=store, features=features, repository=SignalRepository(samples.repository, store))
