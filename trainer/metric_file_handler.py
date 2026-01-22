from __future__ import annotations

import csv
import os
from typing import Iterable, List, Sequence, Any, Optional


class MetricFileHandler:
    """Small helper responsible for writing training metrics to a single CSV file.

    Responsibilities (one instance per metrics file):
    - Know the canonical CSV header for training metrics.
    - Ensure the parent directory exists.
    - Lazily create the file on first write and emit the header row.
    - Append subsequent metric rows in the correct order.
    """

    # Default header for per-epoch training metrics CSV files.
    DEFAULT_TRAINING_HEADER: List[str] = [
        "epoch",
        "train_mse",
        "val_mse",
        "train_top10_mse",
        "val_top10_mse",
        "current_learning_rate",
    ]

    def __init__(self, path: str, header: Optional[Sequence[str]] = None):
        self.path = path
        # If no header is provided, fall back to the standard training header.
        effective_header = list(header) if header is not None else self.DEFAULT_TRAINING_HEADER
        self.header: List[str] = [str(h) for h in effective_header]
        # Create parent directory eagerly so that first write can succeed.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def write_row(self, values: Iterable[Any]) -> None:
        """Append one metrics row to the CSV file, writing the header if needed."""

        metrics_exists = os.path.exists(self.path)
        try:
            with open(self.path, "a" if metrics_exists else "w", newline="", encoding="utf-8") as mf:
                writer = csv.writer(mf)
                if not metrics_exists:
                    writer.writerow(self.header)
                writer.writerow(list(values))
        except Exception as exc:  # pragma: no cover - defensive logging only
            print(f"Warning: failed to write metrics to {self.path}: {exc}")


__all__ = ["MetricFileHandler"]
