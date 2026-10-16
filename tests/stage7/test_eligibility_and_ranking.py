from dataclasses import replace
from datetime import timedelta

import pytest

from stockguidance.market_data.availability import ObservationTiming
from stockguidance.recommendations.eligibility import EligibilityPolicy, EligibilityState, LiquidityObservation, assess_eligibility, eligibility_revision
from stockguidance.recommendations.ranking import rank_scores
from stockguidance.recommendations.result import RankedTicker


def liquidity(samples, ticker_id=17, count=200):
	session = samples.decision().previous_session
	return LiquidityObservation(ticker_id, count, ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15)), "fixture-liquidity-revision")


def test_sparse_reversed_tickers_preserve_exact_scores_and_stable_ties(samples):
	observations = (liquidity(samples, 3), liquidity(samples, 17))
	assessment = assess_eligibility((17, 3), EligibilityPolicy(200), samples.decision(), observations)
	assert rank_scores((RankedTicker(17, 1.0), RankedTicker(3, 2.0)), assessment, 5) == (RankedTicker(3, 2.0), RankedTicker(17, 1.0))
	assert rank_scores((RankedTicker(17, 1.0), RankedTicker(3, 1.0)), assessment, 1) == (RankedTicker(3, 1.0),)
	assert eligibility_revision(assessment) != eligibility_revision(assess_eligibility((17, 3), EligibilityPolicy(200), samples.decision(), (replace(observations[0], transactions=201), observations[1])))


@pytest.mark.parametrize("count,expected", [(0, "ineligible"), (199, "ineligible"), (200, "eligible"), (201, "eligible")])
def test_liquidity_threshold_uses_available_previous_session_counts(samples, count, expected):
	assessment = assess_eligibility((17,), EligibilityPolicy(), samples.decision(), (liquidity(samples, count=count),))
	assert assessment[0].state.value == expected
	ranking = rank_scores((RankedTicker(17, -1.0),), assessment, 5)
	assert len(ranking) == (1 if expected == "eligible" else 0)


@pytest.mark.parametrize("problem", ["missing", "late", "same_session", "stale"])
def test_unavailable_observation_never_bypasses_required_rule(samples, problem):
	observation = liquidity(samples)
	if problem == "late":
		observation = replace(observation, timing=replace(observation.timing, available_at=samples.decision().decision_cutoff + timedelta(seconds=1)))
	elif problem in {"same_session", "stale"}:
		session = samples.decision().entry_session if problem == "same_session" else samples.calendar.shift(samples.decision().previous_session, -1)
		observation = replace(observation, timing=ObservationTiming(session, session.closes_at, session.closes_at))
	assessment = assess_eligibility((17,), EligibilityPolicy(), samples.decision(), () if problem == "missing" else (observation,))
	assert assessment[0].state is EligibilityState.UNAVAILABLE
	with pytest.raises(ValueError, match="unavailable"):
		rank_scores((RankedTicker(17, 1.0),), assessment, 1)


@pytest.mark.parametrize("value", [-1, True, 1.5, "200"])
def test_invalid_transaction_requirements_are_rejected(value):
	with pytest.raises(ValueError):
		EligibilityPolicy(value)


@pytest.mark.parametrize("problem", ["duplicate_observation", "outside_universe", "duplicate_score", "wrong_score_order", "invalid_k"])
def test_external_layouts_are_validated(samples, problem):
	observations = (liquidity(samples), liquidity(samples, 3))
	if problem in {"duplicate_observation", "outside_universe"}:
		observations = (observations[0], observations[0]) if problem == "duplicate_observation" else (replace(observations[0], ticker_id=99),)
		with pytest.raises(ValueError):
			assess_eligibility((17, 3), EligibilityPolicy(), samples.decision(), observations)
	else:
		assessment = assess_eligibility((17, 3), EligibilityPolicy(), samples.decision(), observations)
		scores = (RankedTicker(17, 1.0), RankedTicker(3, 2.0))
		if problem == "duplicate_score":
			scores = (scores[0], scores[0])
		elif problem == "wrong_score_order":
			scores = tuple(reversed(scores))
		with pytest.raises(ValueError):
			rank_scores(scores, assessment, 0 if problem == "invalid_k" else 5)
