"""Temporary legacy imports of the shared model implementation."""

from stockguidance.models.mlp import mlp_type
from stockguidance.models.transformer import transformer_layer_type

MultiLayerLinearNet = mlp_type()
TransformerLayer = transformer_layer_type()
