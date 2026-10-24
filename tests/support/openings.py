from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from support.samples import calendar, chronological_storage, harness, samples
from support.training import cpu_threads, job, training
from support.recommendations import job_json, recommender
from support.simulation import simulation, simulation_json
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
