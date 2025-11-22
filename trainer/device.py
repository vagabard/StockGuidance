from __future__ import annotations

import os
from typing import Optional, Tuple

from .compat import TORCH_AVAILABLE, torch


def setup_device_and_precision(args) -> Tuple["torch.device", str, bool, Optional["torch.dtype"]]:
    """Resolve torch device and AMP precision settings.

    Returns (device, eff_precision:str, use_amp:bool, amp_dtype:Optional[torch.dtype]).
    """
    if not TORCH_AVAILABLE:
        raise RuntimeError("PyTorch is required for training. Please install a CUDA-enabled PyTorch build.")

    # Ensure CUDA allocator uses expandable segments before any CUDA initialization
    try:
        conf_key = "PYTORCH_ALLOC_CONF"
        new_part = "expandable_segments:True"
        prev_conf = os.environ.get(conf_key)
        if not prev_conf:
            os.environ[conf_key] = new_part
        elif "expandable_segments" not in prev_conf:
            os.environ[conf_key] = f"{prev_conf},{new_part}"
        print(f"CUDA alloc conf: {conf_key}={os.environ.get(conf_key)}")
    except Exception:
        pass

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # type: ignore[attr-defined]
    else:
        device = torch.device("cuda" if args.device == "cuda" else "cpu")  # type: ignore[attr-defined]

    requested_precision = (args.precision or "auto").lower()
    raised_prec = requested_precision if requested_precision in ("auto", "fp32", "fp16", "bf16") else "auto"
    if raised_prec == "auto":
        eff_precision = "fp16" if device.type == "cuda" else "fp32"  # type: ignore[union-attr]
    else:
        eff_precision = raised_prec
    use_amp = (device.type == "cuda") and (eff_precision in ("fp16", "bf16"))  # type: ignore[union-attr]
    amp_dtype = None
    if use_amp:
        amp_dtype = torch.float16 if eff_precision == "fp16" else torch.bfloat16

    device_name = torch.cuda.get_device_name(device) if device.type == "cuda" else "CPU"  # type: ignore[union-attr]
    print(f"Selected device: {device.type.upper()} ({device_name})")  # type: ignore[union-attr]
    print(f"Selected precision: {eff_precision.upper()} | autocast={'ON' if use_amp else 'OFF'}")

    return device, eff_precision, use_amp, amp_dtype


def enforce_cuda_or_fail(device) -> None:
    """Raise a helpful error if the selected device is not CUDA for training."""
    if not TORCH_AVAILABLE:
        raise RuntimeError("PyTorch is required for training. Please install a CUDA-enabled PyTorch build.")
    if device.type != "cuda":  # type: ignore[union-attr]
        if not torch.cuda.is_available():  # type: ignore[union-attr]
            raise RuntimeError(
                "CUDA GPU is required for training, but torch.cuda.is_available() is False. "
                "Install a CUDA-enabled PyTorch build and ensure a compatible GPU/driver is present."
            )
        else:
            raise RuntimeError(
                "CUDA GPU is required for training, but a non-CUDA device was selected. "
                "Please run with --device cuda."
            )


__all__ = [
    "setup_device_and_precision",
    "enforce_cuda_or_fail",
]
