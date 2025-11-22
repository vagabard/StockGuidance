"""
Isolated single-run entry point.

Usage:
    python run_one_training_setup.py --prior-days 3 --mlp-layers 4 --lr 0.001 ...

This wrapper ensures that each training configuration runs in a fresh Python
process so PyTorch/CUDA/DataLoader state is fully reset between runs.
"""
from __future__ import annotations

import sys


class RunOneTrainingSetup:
    """Thin wrapper class exposing a `main` method to run a single training config.

    We simply delegate to `train_pytorch_network.main`, which performs the
    actual training logic. The parent process is responsible for invoking this
    script with single-valued arguments (no ranges) per run.
    """

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        # Defer imports so that only this process initializes torch and related modules
        from train_pytorch_network import main as train_main

        # Pass through argv so the same CLI is supported here.
        return train_main(argv)


if __name__ == "__main__":
    raise SystemExit(RunOneTrainingSetup.main(sys.argv[1:]))
