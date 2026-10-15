import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.nn as nn
from models.sparse_linear import RandomPercentLinear
from models.multi_layer_mlp import MultiLayerLinearNet

def test_random_percent_linear():
    print("Testing RandomPercentLinear directly...")
    input_size = 100
    output_size = 50
    prob = 0.4

    layer = RandomPercentLinear(input_size, output_size, prob)

    # Check mask
    num_connections = (layer.mask > 0).sum().item()
    expected_connections = input_size * output_size * prob
    print(f"Actual connections: {num_connections}, Expected ~: {expected_connections}")

    # Tolerance for randomness (binomial distribution variance is n*p*(1-p))
    n = input_size * output_size
    variance = n * prob * (1 - prob)
    std_dev = variance ** 0.5

    if abs(num_connections - expected_connections) > 5 * std_dev:
        print(f"Warning: Connection count {num_connections} is far from expected {expected_connections}")
    else:
        print("Connection count within reasonable range.")

    # Test forward pass
    x = torch.randn(8, input_size)
    out = layer(x)
    assert out.shape == (8, output_size)
    print("Forward pass successful.")

    # Check if weights outside mask are zeroed
    masked_out = layer(x)
    # The result should be the same as manual masking
    manual_out = torch.nn.functional.linear(x, layer.weight * layer.mask, layer.bias)
    assert torch.allclose(masked_out, manual_out), "Masked output does not match manual mask application"
    print("Mask correctly applied.")

def test_mlp_integration():
    print("\nTesting MLP integration...")
    input_size = 10
    output_size = 2
    hidden_layers = [20, 20]
    layer_types = ["random-percent", "random-percent", "linear"]

    model = MultiLayerLinearNet(
        input_size=input_size,
        output_size=output_size,
        hidden_layers=hidden_layers,
        layer_types=layer_types,
        num_layers=3
    )

    print("Model structure:")
    print(model.net)

    # Layers are: 0: RP, 1: ReLU, 2: RP, 3: ReLU, 4: Linear
    assert isinstance(model.net[0], RandomPercentLinear)
    assert isinstance(model.net[2], RandomPercentLinear)
    # Wait, let's see how layers are indexed in MultiLayerLinearNet

    for i, layer in enumerate(model.net):
        print(f"Layer {i}: {type(layer)}")

    # Check if RandomPercentLinear is there
    found_rp = any(isinstance(l, RandomPercentLinear) for l in model.net)
    assert found_rp, "RandomPercentLinear not found in MLP"
    print("RandomPercentLinear successfully integrated into MLP.")

    x = torch.randn(5, input_size)
    out = model(x)
    assert out.shape == (5, output_size)
    print("MLP forward pass successful.")

if __name__ == "__main__":
    test_random_percent_linear()
    test_mlp_integration()
