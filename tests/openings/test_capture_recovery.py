from datetime import timedelta
from types import SimpleNamespace

import pytest

from stockguidance.config.source import SourceConfig
from stockguidance.market_data.opening_refresh import OpeningRefresh
from stockguidance.market_data.openings import OpeningObservation


def test_partial_full_universe_capture_recovers_without_refetching_verified_members(opening_signals, tmp_path):
	samples, store = opening_signals.samples, opening_signals.store
	timing = samples.decision()
	for ticker_id in (17, 3):
		store.path_for(samples.boundary.source, timing.entry_session.session_date.isoformat(), ticker_id).unlink()
	clock = SimpleNamespace(now=lambda: timing.decision_cutoff)
	calls = []
	failures = {3}

	def fetch(source, session, ticker_id, symbol, clock):
		calls.append(ticker_id)
		if ticker_id in failures:
			raise ValueError("fixture-provider-failure")
		return OpeningObservation(ticker_id, symbol, session, 100.0, clock.now(), f"fixture-response-{ticker_id}")

	def inspect(query):
		return samples.repository.read(query), SimpleNamespace(ticker_symbols=((17, "T17"), (3, "T3")))

	source = SourceConfig(samples.boundary.source, store.root, tmp_path / "unused-db.json")
	refresh = OpeningRefresh(source, store, SimpleNamespace(fetch=fetch), SimpleNamespace(inspect=inspect))
	with pytest.raises(ValueError):
		refresh.refresh(opening_signals.features, timing, samples.boundary, clock)
	failures.clear()
	result = refresh.refresh(opening_signals.features, timing, samples.boundary, clock)
	assert not result.issues and calls == [17, 3, 3]
	assert result.query.ticker_ids == (17, 3)
	refresh.refresh(opening_signals.features, timing, samples.boundary, clock)
	assert calls == [17, 3, 3]
	assert all(query.scope.sessions == (timing.previous_session,) for query in samples.repository.calls)


@pytest.mark.parametrize("module", ["compare_signals", "update_openings"])
def test_new_frontends_sanitize_required_failure_details(module, tmp_path, monkeypatch, capsys):
	from stockguidance.cli import compare_signals, update_openings
	frontend = compare_signals if module == "compare_signals" else update_openings
	if module == "compare_signals":
		monkeypatch.setattr(frontend, "load_configuration", lambda path: SimpleNamespace(retraining=True))
		monkeypatch.setattr(frontend, "assemble_signal_comparison", lambda *args: (_ for unused in ()).throw(RuntimeError("fixture-secret")))
		arguments = ["--config", str(tmp_path / "job.json"), "--artifacts-directory", str(tmp_path / "models")]
	else:
		monkeypatch.setattr(frontend, "load_job", lambda path: (_ for unused in ()).throw(RuntimeError("fixture-secret")))
		arguments = ["--config", str(tmp_path / "job.json"), "--session", "2024-07-05"]
	assert frontend.main(arguments) == 1
	assert "fixture-secret" not in capsys.readouterr().out
