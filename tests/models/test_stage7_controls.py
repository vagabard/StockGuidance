from dataclasses import asdict

import pytest
import torch
from torch import nn

from stockguidance.artifacts.decoding import decode_model
from stockguidance.features.specification import FeatureSpec
from stockguidance.models.factory import build_model
from stockguidance.models.specification import Activation, Architecture, ModelSpec


def test_per_layer_and_shared_activation_controls_round_trip_and_derive_widths() -> None:
	features = FeatureSpec((17, 3), 2)
	specification = ModelSpec(
		hidden_widths=(7, 5), hidden_activations=(Activation.RELU, Activation.TANH),
		final_activation=Activation.SIGMOID, implementation_version="stockguidance-models-v2",
	)
	decoded = decode_model(specification.to_mapping())
	assert decoded == specification
	model = build_model(specification, features)
	assert model(torch.zeros(4, features.input_size)).shape == (4, 2)
	assert [type(layer) for layer in model.net if isinstance(layer, (nn.ReLU, nn.Tanh, nn.Sigmoid))] == [nn.ReLU, nn.Tanh, nn.Sigmoid]
	assert torch.all((model(torch.zeros(4, features.input_size)) >= 0) & (model(torch.zeros(4, features.input_size)) <= 1))

	shared = ModelSpec(
		hidden_widths=(4, 4), shared_hidden_activation=Activation.GELU,
		implementation_version="stockguidance-models-v2",
	)
	shared_model = build_model(shared, features)
	assert [type(layer) for layer in shared_model.net if isinstance(layer, nn.GELU)] == [nn.GELU, nn.GELU]


def test_v1_rejects_changed_controls_and_conv_transformer_rejects_incompatible_v2_controls() -> None:
	with pytest.raises(ValueError, match="models-v2"):
		ModelSpec(final_activation=Activation.TANH)
	with pytest.raises(ValueError, match="conv_trans"):
		ModelSpec(architecture=Architecture.CONV_TRANSFORMER, final_activation=Activation.TANH, implementation_version="stockguidance-models-v2")


def test_v1_mapping_keeps_legacy_strict_shape() -> None:
	mapping = ModelSpec().to_mapping()
	assert set(mapping) == {"architecture", "hidden_widths", "activation", "layer_types", "bias", "layer_normalization", "final_activation", "implementation_version"}
	assert decode_model(mapping) == ModelSpec()
