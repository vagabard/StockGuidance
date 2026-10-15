from dataclasses import asdict, dataclass
from math import isclose, isfinite
from pathlib import Path

from stockguidance.artifacts.archive import archive_bytes, read_archive
from stockguidance.artifacts.contracts import ArtifactReference, ModelSemantics
from stockguidance.artifacts.decoding import decode_semantics, exact_mapping
from stockguidance.artifacts.loading import LoadedModel, load_model_bytes
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id


@dataclass(frozen=True)
class LoadedEnsemble:
	artifact_id: str
	semantics: ModelSemantics
	members: tuple[LoadedModel, ...]
	weights: tuple[float, ...]


def validate_members(members: tuple[LoadedModel, ...], weights: tuple[float, ...]) -> ModelSemantics:
	if not members or len(members) != len(weights) or len({member.artifact_id for member in members}) != len(members):
		raise ValueError("Ensembles require explicit distinct members and one weight per member")
	semantics = members[0].metadata.semantics
	if any(member.metadata.semantics != semantics for member in members):
		raise ValueError("Ensemble feature/target/ticker/source/calendar semantics differ")
	if any(isinstance(weight, bool) or not isinstance(weight, (float, int)) or not isfinite(weight) or weight <= 0 for weight in weights) or not isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-12):
		raise ValueError("Ensemble weights must be finite, positive and sum to one")
	return semantics


def load_ensemble_bytes(content: bytes) -> LoadedEnsemble:
	manifest, files = read_archive(content)
	if manifest["schema"] != "stockguidance-ensemble-1":
		raise ValueError("Unsupported ensemble artifact")
	payload = exact_mapping(manifest["payload"], ("semantics", "members"))
	entries = payload["members"]
	if not isinstance(entries, list) or not entries or set(files) != {f"members/{index}.sgmodel" for index in range(len(entries))} or content_id(payload) != manifest["artifact_id"]:
		raise ValueError("Incomplete or corrupt ensemble artifact")
	members: list[LoadedModel] = []
	weights: list[float] = []
	for index, entry in enumerate(entries):
		fields = exact_mapping(entry, ("artifact_id", "weight", "file"))
		weight = fields["weight"]
		if isinstance(weight, bool) or not isinstance(weight, (float, int)) or fields["file"] != f"members/{index}.sgmodel":
			raise ValueError("Invalid ensemble member specification")
		member = load_model_bytes(files[f"members/{index}.sgmodel"])
		if fields["artifact_id"] != member.artifact_id:
			raise ValueError("Ensemble member identity changed")
		members.append(member)
		weights.append(float(weight))
	semantics = validate_members(tuple(members), tuple(weights))
	if semantics != decode_semantics(payload["semantics"]):
		raise ValueError("Ensemble declared semantics disagree with its members")
	return LoadedEnsemble(content_id(payload), semantics, tuple(members), tuple(weights))


def export_ensemble(path: Path, references: tuple[ArtifactReference, ...], weights: tuple[float, ...] | None = None) -> ArtifactReference:
	if not isinstance(path, Path) or not isinstance(references, tuple) or not references or any(not isinstance(reference, ArtifactReference) for reference in references):
		raise ValueError("Ensemble export requires explicit member artifact references")
	weights = tuple(1.0 / len(references) for member in references) if weights is None else weights
	if not isinstance(weights, tuple):
		raise ValueError("Ensemble weights must be immutable")
	contents = tuple(reference.path.read_bytes() for reference in references)
	members = tuple(load_model_bytes(content) for content in contents)
	if any(member.artifact_id != reference.artifact_id for member, reference in zip(members, references)):
		raise ValueError("Ensemble member reference changed")
	semantics = validate_members(members, weights)
	payload = {"semantics": asdict(semantics), "members": [{"artifact_id": member.artifact_id, "weight": weight, "file": f"members/{index}.sgmodel"} for index, (member, weight) in enumerate(zip(members, weights))]}
	identity = content_id(payload)
	content = archive_bytes({"schema": "stockguidance-ensemble-1", "artifact_id": identity, "payload": payload}, {f"members/{index}.sgmodel": member for index, member in enumerate(contents)})
	load_ensemble_bytes(content)
	atomic_write(path, content)
	return ArtifactReference(path, identity)


def load_ensemble(reference: ArtifactReference | Path) -> LoadedEnsemble:
	path = reference.path if isinstance(reference, ArtifactReference) else reference
	if not isinstance(path, Path):
		raise ValueError("Ensemble loading requires an explicit path/reference")
	loaded = load_ensemble_bytes(path.read_bytes())
	if isinstance(reference, ArtifactReference) and loaded.artifact_id != reference.artifact_id:
		raise ValueError("Ensemble reference identity changed")
	return loaded
