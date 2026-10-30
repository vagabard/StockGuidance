import numpy as np
import pytest

from stockguidance.features.specification import FeatureSpec
from stockguidance.preprocessing import FittedPreprocessing, NormalizationMethod, PreprocessingSpec, fit_preprocessing
from stockguidance.targets.specification import TargetSpecV2


def test_standard_input_and_shared_output_fit_only_training_values() -> None:
	features = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
	targets = np.asarray([[10.0, 20.0], [30.0, 40.0]], dtype=np.float64)
	feature_spec = FeatureSpec((17, 3), 1)
	state = fit_preprocessing(PreprocessingSpec(NormalizationMethod.STANDARD, NormalizationMethod.STANDARD), features, targets, feature_specification=feature_spec, target_specification=TargetSpecV2(), data_revision="fixture")
	assert state.transform_inputs(features).mean(axis=0) == pytest.approx((0.0, 0.0))
	assert state.transform_outputs(targets).mean() == pytest.approx(0.0)
	assert np.allclose(state.inverse_outputs(state.transform_outputs(np.asarray([[50.0, -10.0]]))), [[50.0, -10.0]])
	assert FittedPreprocessing.from_mapping(state.to_mapping()) == state


def test_min_max_constant_and_out_of_range_values_are_not_clipped() -> None:
	state = fit_preprocessing(PreprocessingSpec("min_max", "min_max"), [[2.0, 5.0], [4.0, 5.0]], [[1.0, 1.0], [3.0, 1.0]])
	assert np.allclose(state.transform_inputs([[6.0, 9.0]]), [[2.0, 0.0]])
	assert np.allclose(state.transform_outputs([[5.0, 1.0]]), [[2.0, 0.0]])
	assert state.input.constant == (False, True)


def test_fitted_state_rejects_reordered_universe() -> None:
	state = fit_preprocessing(PreprocessingSpec(), [[1.0, 2.0]], [[.1, .2]], feature_specification=FeatureSpec((17, 3), 1), target_specification=TargetSpecV2(), data_revision="fixture")
	with pytest.raises(ValueError, match="ticker order"):
		state.validate_for(FeatureSpec((3, 17), 1), TargetSpecV2())
