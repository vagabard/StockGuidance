import torch
from torch import nn
from torch.nn import Module
from typing import List, Optional

from .convolution_transformer import ConvolutionAndTransformerFeedForwareNet
from .multi_layer_mlp import MultiLayerLinearNet


class SigmoidRange(nn.Module):
	"""
	Activation function based on Sigmoid that maps inputs to the range [-1, 1].
	Formula: 2 * sigmoid(x) - 1
	"""
	def __init__(self):
		super().__init__()

	def forward(self, x: torch.Tensor) -> torch.Tensor:
		return 2.0 * torch.sigmoid(x) - 1.0


def xavier_init_weights(m: nn.Module):
	"""Apply Xavier uniform initialization to linear and convolutional layers."""
	if isinstance(m, (nn.Linear, nn.Conv1d)):
		nn.init.xavier_uniform_(m.weight)
		if m.bias is not None:
			nn.init.constant_(m.bias, 0)


import json
import os
_ACT_ALIASES = {
	"relu": (nn.ReLU, ()),
	"gelu": (nn.GELU, ()),
	"tanh": (nn.Tanh, ()),
	"sigmoid": (nn.Sigmoid, ()),
	"sigmoid_range": (SigmoidRange, ()),
	"sigmoid_ranged": (SigmoidRange, ()),
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
	name = str(name_or_none or "none").lower()
	if name in ("none", "identity", ""):
		return None
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
	hidden_layers: List[int] | int,
	activation:str|None=None,
	final_activation:str|None =None,
	num_layers: int = 3,
	layer_types: Optional[List[str]] = None,
	sequence_length: int = 1,
	bias_mode: str = "all",
	layer_normalization: bool = True,
):
	if isinstance(hidden_layers, int):
		hidden_layers = [hidden_layers]
	arch = (arch or "").lower()
	if arch in ("conv_trans", "convtransformer", "conv_tx", "convolution_transformer"):
		# `hidden_layers` unused by this architecture; kept for signature compatibility
		return ConvolutionAndTransformerFeedForwareNet(input_size, output_size, hidden_layers)
	if arch in ("mlp", "three_layer", "linear3"):
		# Resolve activation name/callable to a factory and pass through
		act_factory = resolve_activation_factory(activation)
		final_activation = resolve_activation_factory(final_activation)
		return MultiLayerLinearNet(input_size, output_size, hidden_layers, act_factory, final_activation, num_layers=num_layers, layer_types=layer_types, sequence_length=sequence_length, bias_mode=bias_mode, layer_normalization=layer_normalization)
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
	hidden_layers_raw = m.get("hidden_layers", 0)
	if isinstance(hidden_layers_raw, list):
		hidden_layers = [int(x) for x in hidden_layers_raw]
	else:
		hidden_layers = [int(hidden_layers_raw)]
	mlp_layers = int(m.get("mlp_layers", 3))
	activation = m.get("activation", "relu")
	final_activation = m.get("final_activation", "")
	layer_types = m.get("layer_types")
	if layer_types is None:
		# Fallback: some older manifests might not have layer_types but we can infer it
		# if it was all linear. However, if the state_dict has masks, it was likely RandomPercentLinear.
		# For now, we trust the manifest or default to None which make_model handles.
		pass
	prior_days = int(m.get("prior_days", 0))
	sequence_length = m.get("sequence_length", prior_days + 1)
	bias_mode = m.get("bias_mode", "all")
	layer_normalization = m.get("layer_normalization")

	# If layer_normalization is missing from manifest, we try to infer it from the state_dict
	# BEFORE creating the model, because it affects the Sequential architecture.
	base_dir = os.path.dirname(os.path.abspath(manifest_path))
	weights_rel = m.get("state_dict_path", "model_state.pt")
	weights_path = os.path.join(base_dir, weights_rel)
	state = torch.load(weights_path, map_location=torch.device(map_location))

	if layer_normalization is None:
		# Heuristic: check if any LayerNorm-like keys exist in the state_dict
		# LayerNorm parameters are usually 'weight' and 'bias' (unlike Linear which has 'weight' and 'bias')
		# but they appear at specific indices.
		# A better way is to see if any key looks like 'net.N.weight' where net[N] would be LayerNorm.
		# If input_size > 5000, MultiLayerLinearNet adds LayerNorm at net.1.
		# If net.1.weight exists in state_dict and its shape is [widths[0]], it's likely LayerNorm.
		found_ln = False
		for key in state.keys():
			# LayerNorm keys in Sequential: net.1.weight, net.1.bias etc.
			# Linear keys: net.0.weight, net.0.bias etc.
			if ".weight" in key or ".bias" in key:
				# If it was trained with LayerNorm, we expect some keys that are NOT Linear
				# but LayerNorm is also (weight, bias).
				# However, we can check for common LayerNorm key patterns or just assume
				# if it's missing it's FALSE for backward compatibility with old massive runs.
				pass

		# If it's an old model (> 5000 input) it almost certainly didn't have LayerNorm
		# because that's a newer addition.
		layer_normalization = False
		print(f"Warning: 'layer_normalization' missing in manifest for massive model (input_size={input_size}). Defaulting to False for compatibility.")

	model = make_model(arch, input_size, output_size, hidden_layers, activation=activation, final_activation=final_activation, num_layers=mlp_layers, layer_types=layer_types, sequence_length=sequence_length, bias_mode=bias_mode, layer_normalization=layer_normalization)
	device = torch.device(map_location)

	# Handle RandomPercentLinear masks if missing in model but present in state_dict
	if layer_types is None:
		inferred_layer_types = []
		found_mask = False
		# Check which layers have masks
		for key in state.keys():
			if key.endswith(".mask"):
				found_mask = True
				break

		if found_mask:
			# If we found any mask, we need to rebuild the model with proper layer types.
			# We'll try to infer which layers are RandomPercentLinear.
			# Linear layers in Sequential are usually at indices 0, 2, 4... (if there are activations/norm)
			# But MultiLayerLinearNet construction is more complex.
			# Let's try to see which 'net.N' has a '.mask'
			mask_indices = set()
			for key in state.keys():
				if key.endswith(".mask"):
					# key is like 'net.0.mask'
					parts = key.split('.')
					if len(parts) >= 2 and parts[1].isdigit():
						mask_indices.add(int(parts[1]))

			if mask_indices:
				# We know which indices in self.net are RandomPercentLinear.
				# However, make_model/MultiLayerLinearNet takes a list of types for LINEAR layers only.
				# We need to map these back to linear layer indices.

				# Instead of complex mapping, a safe heuristic if layer_types is missing but masks exist:
				# assume all linear layers were 'random-percent' if we can't be sure,
				# OR better: if any has a mask, assume they all might be.
				# Actually, the user's default was 'linear'.

				# Let's try to be more precise.
				# We can instantiate a dummy model with 'linear' and see which index corresponds to which layer_index.
				dummy_model = make_model(arch, input_size, output_size, hidden_layers, activation=activation, final_activation=final_activation, num_layers=mlp_layers, layer_types=None, sequence_length=sequence_length, bias_mode=bias_mode)

				linear_layer_to_net_idx = []
				for i, layer in enumerate(dummy_model.net):
					if hasattr(nn, 'Linear') and isinstance(layer, nn.Linear):
						linear_layer_to_net_idx.append(i)
					elif hasattr(layer, 'weight') and hasattr(layer, 'bias'):
						# Fallback for when we don't want to import everything
						linear_layer_to_net_idx.append(i)

				inferred_layer_types = []
				for net_idx in linear_layer_to_net_idx:
					if net_idx in mask_indices:
						inferred_layer_types.append("random-percent")
					else:
						inferred_layer_types.append("linear")

				if inferred_layer_types:
					print(f"Inferred layer_types from state_dict: {inferred_layer_types}")
					layer_types = inferred_layer_types
					# Re-instantiate the model with correct layer_types
					model = make_model(arch, input_size, output_size, hidden_layers, activation=activation, final_activation=final_activation, num_layers=mlp_layers, layer_types=layer_types, sequence_length=sequence_length, bias_mode=bias_mode)
	elif isinstance(layer_types, str):
		# If layer_types is a string (e.g. from an old manifest), convert it to a list if it was intended to be one
		# or just keep it as is if make_model handles it (it expects Optional[List[str]])
		pass

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
