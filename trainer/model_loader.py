"""Legacy checkpoint loading. New inference uses stockguidance.artifacts.loading."""

from __future__ import annotations
import torch
from torch import nn
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from trainer.state.family_run import FamilyRun
    from trainer.state.single_run import SingleRun

from models import make_model

def load_model_from_checkpoint(
    checkpoint_path: str,
    family_run: FamilyRun,
    single_run: SingleRun,
    input_size: int,
    output_size: int,
    mlp_layers: int,
    activation: str,
    prior_days: int,
    device: torch.device = torch.device("cpu"),
    map_location: str = "cpu",
    final_activation: Optional[str] = None
) -> tuple[nn.Module, Optional[dict], Optional[dict]]:
    """
    Unified loader for models from checkpoints.
    Returns (model, optimizer_state_dict, scaler_state_dict).
    """
    if make_model is None:
        raise RuntimeError("models.make_model is unavailable")

    checkpoint = torch.load(checkpoint_path, map_location=map_location, weights_only=False)

    # Check if it's a bundle checkpoint or a direct state dict
    if isinstance(checkpoint, dict) and 'model_state_dict' in checkpoint:
        model_state = checkpoint['model_state_dict']
        optimizer_state = checkpoint.get('optimizer_state_dict')
        scaler_state = checkpoint.get('scaler_state_dict')
    else:
        model_state = checkpoint
        optimizer_state = None
        scaler_state = None

    if final_activation is None:
        final_activation = str(getattr(family_run, "final_activation_type", None) or getattr(family_run, "final_activation", None))

    model = make_model(
        arch=str(family_run.neural_network_type),
        input_size=int(input_size),
        output_size=int(output_size),
        hidden_layers=single_run.hidden_layer_size or [int(input_size)],
        num_layers=int(mlp_layers),
        activation=str(activation),
        final_activation=str(final_activation),
        layer_types=family_run.layer_types,
        sequence_length=int(prior_days) + 1,
        bias_mode=str(single_run.bias),
        layer_normalization=single_run.layer_normalization,
    )

    model.load_state_dict(model_state)
    model.to(device)
    model.eval()

    return model, optimizer_state, scaler_state
