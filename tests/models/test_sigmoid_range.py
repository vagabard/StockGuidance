import torch
from stockguidance.models.activations import sigmoid_range_type

SigmoidRange = sigmoid_range_type()

def test_sigmoid_range():
    act = SigmoidRange()

    # Test values
    inputs = torch.tensor([-100.0, -1.0, 0.0, 1.0, 100.0])
    outputs = act(inputs)

    print("Testing SigmoidRange activation:")
    for i, o in zip(inputs, outputs):
        print(f"  Input: {i.item():>6.1f} -> Output: {o.item():>8.4f}")

    # Check boundaries
    assert torch.allclose(act(torch.tensor([-1e9])), torch.tensor([-1.0])), "Lower bound failed"
    assert torch.allclose(act(torch.tensor([1e9])), torch.tensor([1.0])), "Upper bound failed"
    assert torch.allclose(act(torch.tensor([0.0])), torch.tensor([0.0])), "Zero point failed"

    print("\nVerification successful: Range is indeed [-1, 1] and 0 maps to 0.")

if __name__ == "__main__":
    test_sigmoid_range()
