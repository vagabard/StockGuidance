
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import time
import os
from trainer.datasets import IndexDataset
from models.multi_layer_mlp import MultiLayerLinearNet
from training_instance_provider import TrainingInstanceProvider
import argparse

class MockProvider:
    def __init__(self, input_size, output_size, num_instances):
        self.input_size = input_size
        self.output_size = output_size
        self.num_instances = num_instances

    def get_feature_and_label_pair(self, index):
        # IndexDataset expects 1-based or 0-based depending on usage,
        # but here we just return random data.
        # TrainingInstanceProvider usually returns (x, y, date)
        # Real provider returns Double (float64) tensors
        x = torch.randn(self.input_size, dtype=torch.float64)
        y = torch.randn(self.output_size, dtype=torch.float64)
        return x, y, "2023-01-01"

def benchmark():
    parser = argparse.ArgumentParser(description="Benchmark IndexDataset")
    parser.add_argument("--real", action="store_true", help="Use real training instance provider")
    parser.add_argument("--start-date", type=int, default=15347, help="Start date ID for real provider")
    parser.add_argument("--end-date", type=int, default=16801, help="End date ID for real provider")
    parser.add_argument("--prior-days", type=int, default=7, help="Number of prior days for real provider")
    args = parser.parse_args()

    # Parameters from issue description
    input_size = 40000
    hidden_size = 12000
    output_size = 5000
    num_layers = 4
    epochs = 10
    batch_size = 128 # Reasonable default for large models
    num_instances = 2048 # Small dataset for fast benchmarking

    # Force CUDA 5070 (device 0)
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    if torch.cuda.is_available():
        device = torch.device("cuda")
        device_name = torch.cuda.get_device_name(0)
        print(f"Using GPU: {device_name}")
        if "5070" not in device_name:
             raise RuntimeError(f"Expected a 5070 GPU, but found: {device_name}")
    else:
        print("Diagnostic: torch.cuda.is_available() is False")
        print(f"Diagnostic: torch.version.cuda = {torch.version.cuda}")
        print(f"Diagnostic: CUDA_VISIBLE_DEVICES = {os.environ.get('CUDA_VISIBLE_DEVICES')}")
        raise RuntimeError("GPU requested but not available. Failing as requested.")

    # 1. Prepare Data
    if args.real:
        print(f"Using real TrainingInstanceProvider for dates ({args.start_date}, {args.end_date}) with {args.prior_days} prior days...")
        provider = TrainingInstanceProvider(
            start_date_id=args.start_date,
            end_date_id=args.end_date
        )
        provider.set_how_many_days_in_past_to_use(args.prior_days)

        num_instances = provider.get_maximum_number_of_test_train_instances()
        if num_instances <= 0:
            raise RuntimeError("No training instances available for the given date range.")

        # Determine actual input/output sizes from real data
        # We need to get one pair to find the sizes
        x_sample, y_sample, _ = provider.get_feature_and_label_pair(1)
        input_size = x_sample.shape[0] if hasattr(x_sample, 'shape') else len(x_sample)
        output_size = y_sample.shape[0] if hasattr(y_sample, 'shape') else len(y_sample)

        print(f"Real data loaded: {num_instances} instances, input_size={input_size}, output_size={output_size}")
    else:
        provider = MockProvider(input_size, output_size, num_instances)

    indices = list(range(1, num_instances + 1)) if args.real else list(range(num_instances))
    dataset = IndexDataset(provider, indices)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    # 2. Prepare Model
    # MultiLayerLinearNet(input_size, output_size, hidden_layers, activation_factory, final_activation, num_layers)
    # hidden_layers is a list of widths for hidden layers.
    # For num_layers=4, we need 3 hidden layers.
    model = MultiLayerLinearNet(
        input_size=input_size,
        output_size=output_size,
        hidden_layers=[hidden_size], # It will pad to required number of layers
        num_layers=num_layers
    ).to(device)

    optimizer = optim.Adam(model.parameters(), lr=0.001)
    criterion = nn.MSELoss()

    print(f"Starting benchmark for {epochs} epochs...")
    start_time = time.time()

    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats()

    for epoch in range(epochs):
        epoch_start = time.time()
        running_loss = 0.0
        for inputs, targets in dataloader:
            inputs, targets = inputs.to(device).float(), targets.to(device).float()

            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()

            running_loss += loss.item()

        epoch_end = time.time()
        print(f"Epoch {epoch+1}/{epochs}, Loss: {running_loss/len(dataloader):.4f}, Time: {epoch_end - epoch_start:.2f}s")

    end_time = time.time()
    total_time = end_time - start_time

    print(f"\nBenchmark Complete!")
    print(f"Total Time: {total_time:.2f}s")
    print(f"Average Epoch Time: {total_time/epochs:.2f}s")

    if device.type == "cuda":
        max_mem = torch.cuda.max_memory_allocated() / (1024 ** 2)
        print(f"Peak GPU Memory Used: {max_mem:.2f} MB")
    else:
        print("GPU not available, memory tracking skipped.")

if __name__ == "__main__":
    benchmark()
