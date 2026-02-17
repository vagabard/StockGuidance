import torch
import pytest
from trainer.loss_functions.losses import mse_restricted

def test_mse_restricted_top_logic():
    # Setup test data: B=1, O=10, k=3
    # We want to ensure:
    # 1. Length is 10.
    # 2. Index 3 is in both top-3 predicted and top-3 labeled.

    # Predictions: highest at indices 1, 3, 5
    y_pred = torch.tensor([[0.1, 0.9, 0.2, 0.8, 0.3, 0.7, 0.1, 0.1, 0.1, 0.1]], dtype=torch.float32)
    # Labels:      highest at indices 0, 3, 2
    y_true = torch.tensor([[0.8, 0.2, 0.7, 0.6, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]], dtype=torch.float32)
    k = 3

    # Logic for mode="top", k=3:
    # 1. Top 3 predictions: 0.9 (idx 1), 0.8 (idx 3), 0.7 (idx 5)
    #    Preds at these indices: [0.9, 0.8, 0.7]
    #    Labels at these indices: [0.2, 0.6, 0.1]
    #    MSE_pred = ((0.9-0.2)^2 + (0.8-0.6)^2 + (0.7-0.1)^2) / 3
    #             = (0.49 + 0.04 + 0.36) / 3 = 0.89 / 3 = 0.296666...

    # 2. Top 3 labels: 0.8 (idx 0), 0.7 (idx 2), 0.6 (idx 3)
    #    Labels at these indices: [0.8, 0.7, 0.6]
    #    Preds at these indices: [0.1, 0.2, 0.8]
    #    MSE_true = ((0.1-0.8)^2 + (0.2-0.7)^2 + (0.8-0.6)^2) / 3
    #             = (0.49 + 0.25 + 0.04) / 3 = 0.78 / 3 = 0.26

    # 3. Final MSE = (0.296666... + 0.26) / 2 = 0.556666... / 2 = 0.278333...

    result = mse_restricted(y_pred, y_true, mode="top", k=k)
    expected = (0.89/3.0 + 0.26) / 2.0

    assert torch.allclose(result, torch.tensor(expected, dtype=torch.float32)), f"Expected {expected}, got {result.item()}"

def test_mse_restricted_bottom_logic():
    # Setup test data: B=1, O=10, k=2
    y_pred = torch.tensor([[0.1, 0.9, 0.4, 0.6, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]], dtype=torch.float32)
    y_true = torch.tensor([[0.8, 0.2, 0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]], dtype=torch.float32)
    k = 2

    # Logic for mode="bottom", k=2:
    # 1. Bottom 2 predictions: 0.1 (idx 0) and 0.4 (idx 2)
    #    Preds at these indices: [0.1, 0.4]
    #    Labels at these indices: [0.8, 0.5]
    #    MSE_pred = ((0.1-0.8)^2 + (0.4-0.5)^2) / 2 = (0.49 + 0.01) / 2 = 0.25

    # 2. Bottom 2 labels: 0.2 (idx 1) and 0.3 (idx 3)
    #    Labels at these indices: [0.2, 0.3]
    #    Preds at these indices: [0.9, 0.6]
    #    MSE_true = ((0.9-0.2)^2 + (0.6-0.3)^2) / 2 = (0.49 + 0.09) / 2 = 0.29

    # 3. Final MSE = (0.25 + 0.29) / 2 = 0.27

    result = mse_restricted(y_pred, y_true, mode="bottom", k=k)
    expected = 0.27

    assert torch.allclose(result, torch.tensor(expected)), f"Expected {expected}, got {result.item()}"

def test_mse_restricted_k_larger_than_o():
    y_pred = torch.tensor([[0.1, 0.9]], dtype=torch.float32)
    y_true = torch.tensor([[0.8, 0.2]], dtype=torch.float32)
    k = 5 # Larger than O=2

    # Should cap k at 2
    # Both parts will use both elements
    # MSE_pred = MSE_true = ((0.1-0.8)^2 + (0.9-0.2)^2) / 2 = (0.49 + 0.49) / 2 = 0.49
    # Final = 0.49

    result = mse_restricted(y_pred, y_true, mode="top", k=k)
    expected = 0.49
    assert torch.allclose(result, torch.tensor(expected))

def test_mse_restricted_batch_averaging():
    y_pred = torch.tensor([[1.0, 0.0], [0.0, 1.0]], dtype=torch.float32)
    y_true = torch.tensor([[0.0, 0.0], [1.0, 1.0]], dtype=torch.float32)
    k = 1

    # Row 0:
    # Pred top: 1.0 (idx 0), True=0.0. MSE_pred = (1-0)^2 = 1.0
    # True top: 0.0 (idx 0 or 1), say idx 0. True=0.0, Pred=1.0. MSE_true = (1-0)^2 = 1.0
    # Row 0 MSE = 1.0

    # Row 1:
    # Pred top: 1.0 (idx 1), True=1.0. MSE_pred = (1-1)^2 = 0.0
    # True top: 1.0 (idx 0 or 1), say idx 0. True=1.0, Pred=0.0. MSE_true = (0-1)^2 = 1.0
    # Row 1 MSE = (0.0 + 1.0) / 2 = 0.5

    # The function returns the MEAN of MSEs across all gathered elements if torch.mean is used on the combined results?
    # Wait, let's look at the implementation:
    # mse_pred_based = torch.mean((gather_pred_1 - gather_true_1) ** 2)
    # mse_true_based = torch.mean((gather_pred_2 - gather_true_2) ** 2)
    # return (mse_pred_based + mse_true_based) / 2.0

    # gather_pred_1 for batch: [[1.0], [1.0]]
    # gather_true_1 for batch: [[0.0], [1.0]]
    # mse_pred_based = ((1-0)^2 + (1-1)^2) / 2 = (1+0)/2 = 0.5

    # gather_true_2 for batch: [[0.0], [1.0]] (picking idx 0 for both for simplicity)
    # gather_pred_2 for batch: [[1.0], [0.0]]
    # mse_true_based = ((1-0)^2 + (0-1)^2) / 2 = (1+1)/2 = 1.0

    # result = (0.5 + 1.0) / 2 = 0.75

    result = mse_restricted(y_pred, y_true, mode="top", k=k)
    expected = 0.75
    assert torch.allclose(result, torch.tensor(expected))

if __name__ == "__main__":
    pytest.main([__file__])
