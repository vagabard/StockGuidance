from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from stockguidance.artifacts.archive import archive_bytes
from stockguidance.artifacts.contracts import ArtifactMetadata, ArtifactReference
from stockguidance.artifacts.decoding import metadata_payload
from stockguidance.artifacts.loading import load_model_bytes
from stockguidance.artifacts.weights import weights_bytes
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id

if TYPE_CHECKING:
	from torch import nn


def export_artifact(path: Path, model: nn.Module, metadata: ArtifactMetadata) -> ArtifactReference:
	import torch

	if not isinstance(path, Path) or not isinstance(metadata, ArtifactMetadata):
		raise ValueError("Export requires an explicit path and complete metadata")
	weights = weights_bytes(model)
	payload = {"metadata": metadata_payload(metadata), "weights_sha256": sha256(weights).hexdigest()}
	identity = content_id(payload)
	content = archive_bytes({"schema": "stockguidance-inference-2" if metadata.versioned else "stockguidance-inference-1", "artifact_id": identity, "payload": payload}, {"weights.pt": weights})
	loaded = load_model_bytes(content)
	parameter = next(model.parameters())
	probe = torch.linspace(-0.1, 0.1, metadata.semantics.features.input_size, dtype=torch.float32).reshape(1, -1)
	was_training = model.training
	try:
		model.eval()
		with torch.inference_mode():
			before = model(probe.to(device=parameter.device, dtype=parameter.dtype)).float().cpu()
			after = loaded.model(probe)
		if not isinstance(before, torch.Tensor) or not isinstance(after, torch.Tensor) or before.shape != (1, len(metadata.semantics.features.ticker_ids)) or not torch.isfinite(before).all() or not torch.allclose(before, after, rtol=1e-4, atol=1e-6):
			raise ValueError("Inference export did not preserve valid predictions")
	finally:
		model.train(was_training)
	atomic_write(path, content)
	return ArtifactReference(path, identity)
