from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from stockguidance.artifacts.archive import read_archive
from stockguidance.artifacts.contracts import ArtifactMetadata, ArtifactReference
from stockguidance.artifacts.decoding import decode_metadata, exact_mapping
from stockguidance.artifacts.weights import load_weights
from stockguidance.market_data.identity import content_id
from stockguidance.models.factory import build_model

if TYPE_CHECKING:
	from torch import nn


@dataclass(frozen=True)
class LoadedModel:
	metadata: ArtifactMetadata
	artifact_id: str
	model: nn.Module


def load_model_bytes(content: bytes) -> LoadedModel:
	manifest, files = read_archive(content)
	if manifest["schema"] not in ("stockguidance-inference-1", "stockguidance-inference-2"):
		raise ValueError("Unsupported inference artifact; old open-to-close models require retraining")
	payload = exact_mapping(manifest["payload"], ("metadata", "weights_sha256"))
	if set(files) != {"weights.pt"} or sha256(files["weights.pt"]).hexdigest() != payload["weights_sha256"] or content_id(payload) != manifest["artifact_id"]:
		raise ValueError("Incomplete or corrupt inference artifact")
	metadata = decode_metadata(payload["metadata"])
	model = build_model(metadata.model, metadata.semantics.features)
	load_weights(files["weights.pt"], model)
	model.eval()
	return LoadedModel(metadata, content_id(payload), model)


def load_artifact(reference: ArtifactReference | Path) -> LoadedModel:
	path = reference.path if isinstance(reference, ArtifactReference) else reference
	if not isinstance(path, Path):
		raise ValueError("Loading requires an explicit artifact path/reference")
	loaded = load_model_bytes(path.read_bytes())
	if isinstance(reference, ArtifactReference) and loaded.artifact_id != reference.artifact_id:
		raise ValueError("Artifact reference identity changed")
	return loaded
