from __future__ import annotations

from typing import TYPE_CHECKING

from stockguidance.features.specification import FeatureSpec
from stockguidance.models.activations import activation_factory
from stockguidance.models.convolution import convolution_type
from stockguidance.models.mlp import mlp_type
from stockguidance.models.specification import Architecture, ModelSpec

if TYPE_CHECKING:
	from torch import nn


def build_model(specification: ModelSpec, features: FeatureSpec) -> nn.Module:
	if not isinstance(specification, ModelSpec) or not isinstance(features, FeatureSpec):
		raise ValueError("Model construction requires explicit model and feature specifications")
	if specification.architecture is Architecture.CONV_TRANSFORMER:
		return convolution_type()(features.input_size, len(features.ticker_ids), list(specification.hidden_widths)).float()
	return mlp_type()(features.input_size, len(features.ticker_ids), list(specification.hidden_widths), activation_factory(specification.activation), num_layers=specification.num_layers, layer_types=list(specification.layer_types), sequence_length=features.lookback_sessions, bias_mode=specification.bias, layer_normalization=specification.layer_normalization).float()
