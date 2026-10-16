"""Training argument parsing; application callers use training.service.train_model."""

from stockguidance.cli.train_model import build_parser, parse_args

__all__ = ["build_parser", "parse_args"]
