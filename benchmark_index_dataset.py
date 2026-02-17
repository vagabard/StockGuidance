
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import time
import os
from trainer.datasets import IndexDataset
from models.multi_layer_mlp import MultiLayerLinearNet
from training_instance_provider import TrainingInstanceProvider
from trainer.util.utils import tensorize_and_sanitize
import argparse
import json

class MockProvider:
    def __init__(self, input_size, output_size, num_instances, delay=0.01):
        self.input_size = input_size
        self.output_size = output_size
        self.num_instances = num_instances
        self.delay = delay

    def get_feature_and_label_pair(self, index):
        # Simulate I/O or processing delay
        if self.delay > 0:
            time.sleep(self.delay)
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
    parser.add_argument("--epochs", type=int, default=3, help="Number of epochs per configuration")
    parser.add_argument("--batch-size", type=int, default=128, help="Batch size")
    parser.add_argument("--num-instances", type=int, default=1024, help="Number of instances for mock provider")
    parser.add_argument("--delay", type=float, default=0.02, help="Artificial delay per instance in mock provider (seconds)")
    args = parser.parse_args()

    # Parameters from issue description
    input_size = 20000
    hidden_size = 10000
    output_size = 2000
    num_layers = 3
    epochs = args.epochs
    batch_size = args.batch_size
    num_instances = args.num_instances

    # Force CUDA 5070 (device 0)
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"

    if torch.cuda.is_available():
        device = torch.device("cuda")
        device_name = torch.cuda.get_device_name(0)
        print(f"Using GPU: {device_name}")
        if "5070" not in device_name:
             print(f"WARNING: Expected a 5070 GPU, but found: {device_name}")
    else:
        print("Diagnostic: torch.cuda.is_available() is False")
        raise RuntimeError("GPU requested but not available. Failing as requested.")

    # 1. Prepare Data Provider
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
        x_sample, y_sample, _ = provider.get_feature_and_label_pair(1)
        input_size = x_sample.shape[0] if hasattr(x_sample, 'shape') else len(x_sample)
        output_size = y_sample.shape[0] if hasattr(y_sample, 'shape') else len(y_sample)
        print(f"Real data loaded: {num_instances} instances, input_size={input_size}, output_size={output_size}")
    else:
        provider = MockProvider(input_size, output_size, num_instances, delay=args.delay)

    indices = list(range(1, num_instances + 1)) if args.real else list(range(num_instances))
    dataset = IndexDataset(provider, indices)

    # Configurations to test
    # (num_workers, pin_memory, persistent_workers, non_blocking)
    configs = [
        (0, True, False, True),
        (2, True, True, True),
        (4, True, True, True),
        (8, True, True, True),
        (12, True, True, True),
        (16, True, True, True),
    ]

    results = []

    for num_workers, pin_memory, persistent_workers, non_blocking in configs:
        print(f"\nTesting Config: workers={num_workers}, pin={pin_memory}, persistent={persistent_workers}, non_blocking={non_blocking}")

        # Fresh model and optimizer for each config to avoid state carryover
        model = MultiLayerLinearNet(
            input_size=input_size,
            output_size=output_size,
            hidden_layers=[hidden_size],
            num_layers=num_layers
        ).to(device)
        optimizer = optim.Adam(model.parameters(), lr=0.001)
        criterion = nn.MSELoss()

        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=True,
            num_workers=num_workers,
            pin_memory=pin_memory,
            persistent_workers=persistent_workers
        )

        # Warmup epoch
        print("  Warmup epoch...")
        for inputs, targets in dataloader:
            inputs, targets = tensorize_and_sanitize(inputs, targets, device=device, non_blocking=non_blocking)
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
            break # Just one batch for warmup

        start_time = time.time()
        epoch_times = []

        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()

        for epoch in range(epochs):
            epoch_start = time.time()
            for inputs, targets in dataloader:
                inputs, targets = tensorize_and_sanitize(inputs, targets, device=device, non_blocking=non_blocking)
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()

            epoch_end = time.time()
            epoch_duration = epoch_end - epoch_start
            epoch_times.append(epoch_duration)
            print(f"  Epoch {epoch+1}/{epochs}: {epoch_duration:.2f}s")

        total_time = time.time() - start_time
        avg_epoch_time = sum(epoch_times) / len(epoch_times)
        max_mem = torch.cuda.max_memory_allocated() / (1024 ** 2) if device.type == "cuda" else 0

        res = {
            "num_workers": num_workers,
            "pin_memory": pin_memory,
            "persistent_workers": persistent_workers,
            "non_blocking": non_blocking,
            "avg_epoch_time": avg_epoch_time,
            "max_mem_mb": max_mem
        }
        results.append(res)
        print(f"  Avg Epoch Time: {avg_epoch_time:.2f}s, Max GPU Mem: {max_mem:.2f} MB")

    print("\n" + "="*50)
    print(f"{'Workers':<8} {'Pin':<6} {'Persist':<8} {'NonBlk':<8} {'Avg Time':<10} {'Max Mem'}")
    print("-" * 50)
    for r in results:
        print(f"{r['num_workers']:<8} {str(r['pin_memory']):<6} {str(r['persistent_workers']):<8} {str(r['non_blocking']):<8} {r['avg_epoch_time']:<10.2f} {r['max_mem_mb']:.2f} MB")
    print("="*50)

if __name__ == "__main__":
    benchmark()
