from dataclasses import FrozenInstanceError, replace

import pytest

from stockguidance.features.specification import FeatureSpec, SignalMode
from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.request import RecommendationRequest
from stockguidance.targets.specification import TargetSpec


@pytest.mark.parametrize("raw", ["current", "retrain"])
def test_model_policy_external_values(raw: str) -> None:
	policy = ModelRefreshPolicy(raw)
	selection = ModelSelection(policy, "artifact" if policy is ModelRefreshPolicy.CURRENT else None)
	assert selection.policy.value == raw


@pytest.mark.parametrize("raw", ["existing", "train", "", "CURRENT", None, 1])
def test_invalid_model_policy_external_values(raw: object) -> None:
	with pytest.raises(ValueError):
		ModelRefreshPolicy(raw)


@pytest.mark.parametrize("changes", [{"policy": "current"}, {"artifact_id": ""}, {"artifact_id": None}, {"policy": ModelRefreshPolicy.RETRAIN}])
def test_invalid_model_selections(changes: dict[str, object]) -> None:
	with pytest.raises(ValueError):
		replace(ModelSelection(ModelRefreshPolicy.CURRENT, "artifact"), **changes)


@pytest.mark.parametrize("policy", list(AllocationPolicy))
def test_allocation_policies_are_independent_of_model_semantics(recommendation_request: RecommendationRequest, policy: AllocationPolicy) -> None:
	allocation = AllocationSpec(policy, recommendation_request.allocation.anchor_session)
	updated = replace(recommendation_request, allocation=allocation, model=ModelSelection(ModelRefreshPolicy.RETRAIN))
	assert updated.features is recommendation_request.features
	assert updated.target is recommendation_request.target
	assert allocation.buy_cadence_sessions == 2
	assert allocation.allocation_count == (2 if policy is AllocationPolicy.STAGGERED_HALVES else 1)


@pytest.mark.parametrize("changes", [{"policy": "daily"}, {"policy": "staggered_halves"}, {"cash_rest": "immediate"}, {"cash_rest": False}])
def test_unsupported_allocation_and_cash_rest(recommendation_request: RecommendationRequest, changes: dict[str, object]) -> None:
	with pytest.raises(ValueError):
		replace(recommendation_request.allocation, **changes)


def test_entry_open_requires_its_explicit_opening_transformation() -> None:
	assert SignalMode("entry_open") is SignalMode.ENTRY_OPEN
	with pytest.raises(ValueError, match="entry_open requires"):
		FeatureSpec((7, 3), 2, signal_mode=SignalMode.ENTRY_OPEN)


@pytest.mark.parametrize("changes", [
	{"ticker_ids": ()}, {"ticker_ids": (7, 7)}, {"ticker_ids": (True,)},
	{"ticker_ids": [7, 3]}, {"ticker_ids": (0,)}, {"ticker_ids": ("7",)},
	{"lookback_sessions": 0}, {"lookback_sessions": True}, {"signal_mode": "previous_close"},
	{"missing_history": "zero_fill"}, {"interval": ObservationInterval.HOURLY},
])
def test_feature_runtime_validation(changes: dict[str, object]) -> None:
	with pytest.raises(ValueError):
		replace(FeatureSpec((7, 3), 2), **changes)


@pytest.mark.parametrize("changes", [{"entry_price": "open"}, {"exit_price": "open"}, {"horizon": 1}, {"transform": "rank"}])
def test_target_convention_cannot_be_silently_changed(changes: dict[str, object]) -> None:
	with pytest.raises(ValueError):
		replace(TargetSpec(), **changes)


def test_immutable_configuration_and_explicit_target(recommendation_request: RecommendationRequest) -> None:
	assert recommendation_request.target.raw_formula == "close(T+1) / close(T) - 1"
	assert recommendation_request.features.ticker_ids == (7, 3)
	assert "provider-key-reference" not in repr(recommendation_request)
	with pytest.raises(FrozenInstanceError):
		setattr(recommendation_request.features, "lookback_sessions", 99)
