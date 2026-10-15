import math

import pytest
import torch
import torch.nn as nn

from models import make_model


def _xavier_uniform_bound(weight: torch.Tensor) -> float:
    fan_in, fan_out = nn.init._calculate_fan_in_and_fan_out(weight)
    return math.sqrt(6.0 / float(fan_in + fan_out))


def _kaiming_uniform_bound(weight: torch.Tensor, nonlinearity: str, a: float) -> float:
    fan_in, _ = nn.init._calculate_fan_in_and_fan_out(weight)
    gain = nn.init.calculate_gain(nonlinearity, a)
    return gain * math.sqrt(3.0 / float(fan_in))


@pytest.mark.parametrize(
    ("activation", "expected_init"),
    [
        ("relu", "kaiming_relu"),
        ("gelu", "kaiming_relu"),
        ("silu", "kaiming_relu"),
        ("leaky_relu", "kaiming_leaky_relu"),
        ("elu", "kaiming_relu"),
        ("tanh", "xavier"),
        ("tanhshrink", "xavier"),
        ("sigmoid", "xavier"),
        ("sigmoid_range", "xavier"),
    ],
)
def test_dense_mlp_weight_init_ranges_match_activation(activation: str, expected_init: str) -> None:
    torch.manual_seed(1234)
    model = make_model(
        arch="mlp",
        input_size=8,
        output_size=3,
        hidden_layers=[6, 5],
        activation=activation,
        final_activation="",
        num_layers=3,
        layer_types=["linear", "linear", "linear"],
    )

    linear_layers = [m for m in model.net if isinstance(m, nn.Linear)]
    assert linear_layers, "Expected at least one dense Linear layer."

    for layer in linear_layers:
        if expected_init == "xavier":
            expected_bound = _xavier_uniform_bound(layer.weight)
        elif expected_init == "kaiming_leaky_relu":
            expected_bound = _kaiming_uniform_bound(layer.weight, nonlinearity="leaky_relu", a=0.01)
        else:
            expected_bound = _kaiming_uniform_bound(layer.weight, nonlinearity="relu", a=0.0)

        max_abs = layer.weight.detach().abs().max().item()
        assert max_abs <= expected_bound + 1e-6

        if layer.bias is not None:
            assert torch.count_nonzero(layer.bias).item() == 0
