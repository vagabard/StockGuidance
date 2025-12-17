import torch
from torch import nn
from torch.nn import Module

from .convolution_transformer import ConvolutionAndTransformerFeedForwareNet
from .multi_layer_mlp import MultiLayerLinearNet


import json
import os
_ACT_ALIASES = {
	"relu": (nn.ReLU, ()),
	"gelu": (nn.GELU, ()),
	"tanh": (nn.Tanh, ()),
	"sigmoid": (nn.Sigmoid, ()),
	"silu": (nn.SiLU, ()),
	"swish": (nn.SiLU, ()),  # alias
	"leaky_relu": (nn.LeakyReLU, (0.01,)),
	"elu": (nn.ELU, ()),
	# Tanhshrink variants
	"tanshrink": (nn.Tanhshrink, ()),
	"tanhshrink": (nn.Tanhshrink, ()),  # alias
	"softmax": (nn.Softmax, ()),
	"logsoftmax": (nn.LogSoftmax, (1,))
}


def resolve_activation_factory(name_or_none: str) -> Module | None:
	"""Return a zero-arg factory that builds the requested activation module.

	If name_or_none is None, default to ReLU. If a string is provided, it's
	case-insensitive and looked up in _ACT_ALIASES. If already a callable,
	it's returned as-is.
	"""
	# if callable(name_or_none):
	#     return name_or_none
	name = str(name_or_none or "relu").lower()
	if name in _ACT_ALIASES:
		cls, args = _ACT_ALIASES[name]
		return lambda cls=cls, args=args: cls(*args)  # fresh instance each call
	# Fallback: try to get attribute from torch_interface.nn by name
	if hasattr(nn, name.capitalize()):
		attr = getattr(nn, name.capitalize())
		if isinstance(attr, type):
			return lambda attr=attr: attr()
	# Default to ReLU if unknown
	return lambda: nn.ReLU()


def make_model(
	arch: str,
	input_size: int,
	output_size: int,
	hidden_layers: int,
	activation:str|None=None,
	final_activation:str|None =None,
	num_layers: int = 3,
):
	arch = (arch or "").lower()
	if arch in ("conv_trans", "convtransformer", "conv_tx", "convolution_transformer"):
		# `hidden_layers` unused by this architecture; kept for signature compatibility
		return ConvolutionAndTransformerFeedForwareNet(input_size, output_size, hidden_layers)
	if arch in ("mlp", "three_layer", "linear3"):
		# Resolve activation name/callable to a factory and pass through
		act_factory = resolve_activation_factory(activation)
		final_activation = resolve_activation_factory(final_activation)
		return MultiLayerLinearNet(input_size, output_size, hidden_layers, act_factory,final_activation, num_layers=num_layers)
	raise ValueError(f"Unknown architecture '{arch}'. Choices: conv_trans, mlp")


def load_model_from_manifest(manifest_path: str, map_location: str = "cpu") -> tuple[Module, dict, dict | None] :
	"""Load a trained model from a JSON manifest created by train_pytorch_network.py.

	Returns a tuple: (model, metadata_dict, optimizer_state_dict_or_None)
	The optimizer state may be None if unavailable.
	"""
	if torch is None:
		raise RuntimeError("PyTorch is not available; cannot load a saved model.")

	with open(manifest_path, "r", encoding="utf-8") as f:
		m = json.load(f)

	arch = m.get("arch")
	input_size = int(m.get("input_size"))
	output_size = int(m.get("output_size"))
	hidden_layers = int(m.get("hidden_layers", 0))
	mlp_layers = int(m.get("mlp_layers", 3))
	activation = m.get("activation", "relu")
	final_activation = m.get("final_activation", "")

	model = make_model(arch, input_size, output_size, hidden_layers, activation=activation, final_activation=final_activation, num_layers=mlp_layers)
	device = torch.device(map_location)

	base_dir = os.path.dirname(os.path.abspath(manifest_path))
	weights_rel = m.get("state_dict_path", "model_state.pt")
	weights_path = os.path.join(base_dir, weights_rel)
	state = torch.load(weights_path, map_location=device)
	model.load_state_dict(state)
	model.to(device)
	model.eval()

	opt_state = None
	opt_rel = m.get("optimizer_state_path")
	if opt_rel:
		opt_path = os.path.join(base_dir, opt_rel)
		if os.path.exists(opt_path):
			try:
				opt_state = torch.load(opt_path, map_location="cpu")
			except Exception:
				opt_state = None

	return model, m, opt_state
