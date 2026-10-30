from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from stockguidance.artifacts.weights import validated_state
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, mapping
from stockguidance.training.configuration import TrainingConfig

if TYPE_CHECKING:
	import torch
	from torch import nn
	from torch.amp import GradScaler
	from torch.optim import Optimizer
	from torch.optim.lr_scheduler import ExponentialLR


def checkpoint_identity(config: TrainingConfig, revision: str, *, device: str, precision: str, framework_version: str, preprocessing_identity: str | None = None) -> str:
	settings = asdict(config)
	del settings["epochs"]
	payload: dict[str, object] = {"schema": "training-checkpoint-1" if preprocessing_identity is None else "training-checkpoint-2", "configuration": settings, "data_revision": revision, "effective_device": device, "effective_precision": precision, "framework_version": framework_version}
	if preprocessing_identity is not None:
		payload["preprocessing_identity"] = preprocessing_identity
	return content_id(payload)


def save_checkpoint(path: Path, identity: str, epoch: int, model: nn.Module, optimizer: Optimizer, scheduler: ExponentialLR, scaler: GradScaler, generator: torch.Generator, *, schema: str = "stockguidance-checkpoint-1") -> None:
	import torch

	if schema not in ("stockguidance-checkpoint-1", "stockguidance-checkpoint-2"):
		raise ValueError("Unsupported checkpoint schema")
	state = {"schema": schema, "identity": identity, "epoch": epoch, "model": validated_state(model.state_dict(), model), "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(), "scaler": scaler.state_dict(), "rng": torch.get_rng_state(), "loader_rng": generator.get_state(), "cuda_rng": torch.cuda.get_rng_state_all() if next(model.parameters()).device.type == "cuda" else []}
	output = BytesIO()
	torch.save(state, output)
	atomic_write(path, output.getvalue())


def resume_checkpoint(path: Path, identity: str, model: nn.Module, optimizer: Optimizer, scheduler: ExponentialLR, scaler: GradScaler, generator: torch.Generator, *, schema: str = "stockguidance-checkpoint-1") -> int:
	import torch

	state = mapping(torch.load(path, map_location="cpu", weights_only=True))
	if set(state) != {"schema", "identity", "epoch", "model", "optimizer", "scheduler", "scaler", "rng", "loader_rng", "cuda_rng"} or state["schema"] != schema or state["identity"] != identity or type(state["epoch"]) is not int or state["epoch"] <= 0:
		raise ValueError("Checkpoint configuration or dataset is incompatible")
	model.load_state_dict(validated_state(state["model"], model), strict=True)
	optimizer.load_state_dict(mapping(state["optimizer"]))
	scheduler.load_state_dict(mapping(state["scheduler"]))
	scaler.load_state_dict(mapping(state["scaler"]))
	if not isinstance(state["rng"], torch.Tensor) or not isinstance(state["loader_rng"], torch.Tensor) or not isinstance(state["cuda_rng"], list):
		raise ValueError("Checkpoint random state is incomplete")
	torch.set_rng_state(state["rng"])
	generator.set_state(state["loader_rng"])
	if state["cuda_rng"]:
		if not all(isinstance(value, torch.Tensor) for value in state["cuda_rng"]):
			raise ValueError("Invalid CUDA random state")
		torch.cuda.set_rng_state_all(state["cuda_rng"])
	return state["epoch"]
