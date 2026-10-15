from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from torch import Tensor, nn


@cache
def sigmoid_range_type() -> type[nn.Module]:
	import torch
	from torch import nn

	class SigmoidRange(nn.Module):
		def forward(self, inputs: Tensor) -> Tensor:
			return 2.0 * torch.sigmoid(inputs) - 1.0

	return SigmoidRange


def activation_factory(name: str) -> Callable[[], nn.Module] | None:
	from torch import nn

	aliases: dict[str, Callable[[], nn.Module]] = {
		"relu": nn.ReLU, "gelu": nn.GELU, "tanh": nn.Tanh,
		"sigmoid": nn.Sigmoid, "sigmoid_range": sigmoid_range_type(),
		"sigmoid_ranged": sigmoid_range_type(), "silu": nn.SiLU,
		"swish": nn.SiLU, "leaky_relu": nn.LeakyReLU, "elu": nn.ELU,
		"tanshrink": nn.Tanhshrink, "tanhshrink": nn.Tanhshrink,
		"softmax": lambda: nn.Softmax(dim=1), "logsoftmax": lambda: nn.LogSoftmax(dim=1),
	}
	if name in ("none", "identity", ""):
		return None
	if name not in aliases:
		raise ValueError("Unsupported activation")
	return aliases[name]
