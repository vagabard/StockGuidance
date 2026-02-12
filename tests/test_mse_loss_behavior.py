import torch
import torch.nn as nn

def test_mse_behavior():
    criterion = nn.L1Loss ()

    # Cases with 0.5
    val_p05 = torch.tensor([0.5])
    target_pos1 = torch.tensor([1.0])
    target_neg1 = torch.tensor([-1.0])

    loss_p05_pos1 = criterion(val_p05, target_pos1)
    loss_p05_neg1 = criterion(val_p05, target_neg1)

    print("MSELoss comparisons for 0.5:")
    print(f"  MSELoss(0.5, +1.0) = {loss_p05_pos1.item():.4f} (expected (0.5 - 1.0)^2 = 0.25)")
    print(f"  MSELoss(0.5, -1.0) = {loss_p05_neg1.item():.4f} (expected (0.5 - (-1.0))^2 = 2.25)")

    # Cases with -0.5
    val_n05 = torch.tensor([-0.5])

    loss_n05_pos1 = criterion(val_n05, target_pos1)
    loss_n05_neg1 = criterion(val_n05, target_neg1)

    print("\nMSELoss comparisons for -0.5:")
    print(f"  MSELoss(-0.5, +1.0) = {loss_n05_pos1.item():.4f} (expected (-0.5 - 1.0)^2 = 2.25)")
    print(f"  MSELoss(-0.5, -1.0) = {loss_n05_neg1.item():.4f} (expected (-0.5 - (-1.0))^2 = 0.25)")

if __name__ == "__main__":
    test_mse_behavior()
