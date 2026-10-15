from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from stockguidance.market_data.identity import mapping

if TYPE_CHECKING:
	from torch import Tensor, nn


def validated_state(value: object, model: nn.Module) -> dict[str, Tensor]:
	import torch

	state = mapping(value)
	expected = model.state_dict()
	if set(state) != set(expected):
		raise ValueError("Weights do not match the exact architecture")
	result: dict[str, Tensor] = {}
	for name, tensor in state.items():
		if not isinstance(tensor, torch.Tensor) or tensor.shape != expected[name].shape or tensor.dtype != expected[name].dtype or not torch.isfinite(tensor).all():
			raise ValueError("Invalid weight shape, dtype, or finite values")
		result[name] = tensor
	return result


def weights_bytes(model: nn.Module) -> bytes:
	import torch

	state = {name: value.detach().cpu().clone() for name, value in validated_state(model.state_dict(), model).items()}
	output = BytesIO()
	torch.save(state, output)
	return output.getvalue()


def load_weights(content: bytes, model: nn.Module) -> None:
	import torch

	state = torch.load(BytesIO(content), map_location="cpu", weights_only=True)
	model.load_state_dict(validated_state(state, model), strict=True)
