from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING

from stockguidance.training.configuration import DeviceChoice, OptimizerChoice, Precision, TrainingConfig

if TYPE_CHECKING:
	import torch
	from torch import nn
	from torch.optim import Optimizer


@dataclass(frozen=True)
class Resources:
	device: torch.device
	precision: Precision
	amp_dtype: torch.dtype | None


def resolve_resources(config: TrainingConfig) -> Resources:
	import torch

	allocator_key = "PYTORCH_ALLOC_CONF"
	allocator = os.environ.get(allocator_key, "")
	if "expandable_segments" not in allocator:
		os.environ[allocator_key] = ",".join(filter(None, (allocator, "expandable_segments:True")))
	device = torch.device("cuda" if config.device is DeviceChoice.AUTO and torch.cuda.is_available() else "cpu" if config.device is DeviceChoice.AUTO else config.device.value)
	if device.type == "cuda" and not torch.cuda.is_available():
		raise ValueError("CUDA was requested but is unavailable")
	if device.type == "cuda" and "4070" in torch.cuda.get_device_name(device):
		raise ValueError("The existing training device policy excludes GPUs named 4070")
	precision = Precision.FP16 if config.precision is Precision.AUTO and device.type == "cuda" else Precision.FP32 if config.precision is Precision.AUTO else config.precision
	if device.type == "cpu" and precision is not Precision.FP32:
		raise ValueError("CPU training requires fp32 precision")
	amp_dtype = torch.float16 if precision is Precision.FP16 else torch.bfloat16 if precision is Precision.BF16 else None
	return Resources(device, precision, amp_dtype)


def create_optimizer(model: nn.Module, config: TrainingConfig, resources: Resources) -> Optimizer:
	from torch import optim

	fused = resources.device.type == "cuda"
	if config.optimizer is OptimizerChoice.ADAM:
		return optim.Adam(model.parameters(), lr=config.learning_rate, fused=fused)
	if config.optimizer is OptimizerChoice.ADAMW:
		return optim.AdamW(model.parameters(), lr=config.learning_rate, fused=fused)
	return optim.SGD(model.parameters(), lr=config.learning_rate, fused=fused)
