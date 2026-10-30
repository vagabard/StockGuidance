"""Filesystem service for atomic, inspectable saved-run directories."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Callable, Mapping
from uuid import uuid4

from stockguidance.config.paths import project_root
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.runs.contracts import (
	ArtifactReference,
	FailureRecord,
	ReportState,
	RunConfig,
	RunManifest,
	RunOperation,
	RunStatus,
	RunSummary,
	RunTimestamps,
	_validate_run_id,
	_validate_relative_path,
)


_MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".manifest.lock"
_RESERVED_SECTIONS = frozenset({"identity", "resolution", "universe", "specifications", "provenance", "results"})
_UNSET = object()


@dataclass(frozen=True)
class RunHandle:
	run_id: str
	directory: Path

	@property
	def manifest_path(self) -> Path:
		return self.directory / _MANIFEST_NAME

	@property
	def artifacts_directory(self) -> Path:
		return self.directory / "artifacts"

	@property
	def checkpoints_directory(self) -> Path:
		return self.directory / "checkpoints"

	@property
	def reports_directory(self) -> Path:
		return self.directory / "reports"


class _ManifestLock:
	def __init__(self, directory: Path) -> None:
		self.path = directory / _LOCK_NAME
		self.file: object | None = None

	def __enter__(self) -> "_ManifestLock":
		deadline = time.monotonic() + 10.0
		while True:
			try:
				self.file = self.path.open("x", encoding="utf-8")
				return self
			except FileExistsError:
				if time.monotonic() >= deadline:
					raise TimeoutError("Timed out waiting for saved-run manifest lock")
				time.sleep(0.005)

	def __exit__(self, *_: object) -> None:
		if self.file is not None:
			close = getattr(self.file, "close")
			close()
		self.path.unlink(missing_ok=True)


def _now() -> datetime:
	return datetime.now(timezone.utc)


def _reject_constant(value: str) -> object:
	raise ValueError(f"Manifest contains non-finite JSON constant {value}")


def _safe_json_load(path: Path) -> object:
	try:
		return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
	except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
		raise ValueError("Run manifest is missing, incomplete, or corrupt") from error


def _git_identity(root: Path) -> dict[str, object]:
	try:
		revision_result = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True, timeout=5)
		dirty_result = subprocess.run(("git", "status", "--porcelain"), cwd=root, check=True, capture_output=True, text=True, timeout=5)
		return {"git_revision": revision_result.stdout.strip(), "dirty_tree": bool(dirty_result.stdout.strip()), "git_available": True}
	except (OSError, subprocess.SubprocessError):
		return {"git_revision": None, "dirty_tree": None, "git_available": False}


def default_provenance(root: Path, seed: int | None = None) -> dict[str, object]:
	"""Collect identity metadata without importing optional runtime frameworks."""

	identity = _git_identity(root)
	identity.update({"software": {"python": sys.version.split()[0], "platform": platform.platform(), "stockguidance": "0.1.0"}, "seed": seed})
	return identity


def _relative_path(path: str) -> Path:
	_validate_relative_path(path)
	return Path(*path.split("/"))


class RunDirectoryService:
	"""Create and inspect run directories without database or training side effects."""

	def __init__(self, runs_root: Path | str | None = None, *, project: Path | None = None, clock: Callable[[], datetime] | None = None) -> None:
		base = (project or project_root()).resolve()
		self._project_root = base
		selected = Path("runs") if runs_root is None else Path(runs_root)
		self.runs_root = (base / selected if not selected.is_absolute() else selected).resolve()
		self._clock = _now if clock is None else clock

	def create(self, config: RunConfig, *, run_id: str | None = None, provenance: Mapping[str, object] | None = None) -> RunHandle:
		if not isinstance(config, RunConfig):
			raise ValueError("Run creation requires a RunConfig")
		if config.parent_run_id is not None:
			self.read(config.parent_run_id)
		self.runs_root.mkdir(parents=True, exist_ok=True)
		for _ in range(8):
			candidate = run_id or self._new_run_id(config.operation)
			_validate_run_id(candidate)
			path = self.runs_root / candidate
			try:
				path.mkdir()
			except FileExistsError:
				if run_id is not None:
					raise ValueError("Run ID already exists") from None
				continue
			for directory in (path / "artifacts", path / "checkpoints", path / "reports"):
				directory.mkdir()
			created = self._clock()
			if created.tzinfo is None or created.utcoffset() is None:
				raise ValueError("Run clock must return an aware datetime")
			manifest = RunManifest(
				run_id=candidate,
				operation=config.operation,
				status=RunStatus.PENDING,
				report_state=ReportState.NOT_APPLICABLE,
				timestamps=RunTimestamps(created_at=created, updated_at=created),
				original_configuration=config.settings,
				resolved_configuration=None,
				parent_run_id=config.parent_run_id,
				provenance=default_provenance(self._project_root) if provenance is None else provenance,
				results={key: None for key in ("predictions", "eligibility", "exclusions", "metrics", "reports", "trades", "equity", "benchmarks")},
			)
			self._write_manifest(path, manifest)
			return RunHandle(candidate, path)
		raise RuntimeError("Could not allocate a collision-free run ID")

	def _new_run_id(self, operation: RunOperation) -> str:
		return f"{operation.value}-{self._clock().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"

	def handle(self, run_id: str) -> RunHandle:
		path = self._run_directory(run_id)
		return RunHandle(run_id, path)

	def read(self, run_id: str) -> RunManifest:
		return self._read(run_id, set())

	def _read(self, run_id: str, ancestors: set[str]) -> RunManifest:
		path = self._run_directory(run_id)
		if run_id in ancestors:
			raise ValueError("Saved-run parent cycle detected")
		manifest = RunManifest.from_mapping(_safe_json_load(path / _MANIFEST_NAME))
		if manifest.run_id != run_id:
			raise ValueError("Manifest run ID does not match its directory")
		if manifest.parent_run_id is not None:
			self._read(manifest.parent_run_id, ancestors | {run_id})
		return manifest

	def validate(self, run_id: str) -> RunManifest:
		manifest = self.read(run_id)
		run_directory = self._run_directory(run_id)
		for reference in manifest.artifacts:
			if reference.relative_path is not None:
				path = run_directory / _relative_path(reference.relative_path)
				if not path.is_file() or _file_sha256(path) != reference.sha256:
					raise ValueError(f"Artifact reference is unavailable or changed: {reference.name}")
		return manifest

	def list_runs(self, *, include_invalid: bool = True) -> tuple[RunSummary, ...]:
		if not self.runs_root.exists():
			return ()
		items: list[RunSummary] = []
		for path in sorted((candidate for candidate in self.runs_root.iterdir() if candidate.is_dir()), key=lambda candidate: candidate.name):
			manifest_path = path / _MANIFEST_NAME
			if not manifest_path.exists():
				if include_invalid:
					items.append(RunSummary(path.name, path, False, error="Run directory has no manifest"))
				continue
			try:
				manifest = self.read(path.name)
			except ValueError as error:
				if include_invalid:
					items.append(RunSummary(path.name, path, False, error="Invalid or incomplete run manifest"))
				continue
			items.append(RunSummary(path.name, path, True, manifest))
		return tuple(items)

	def children(self, parent_run_id: str, *, include_invalid: bool = False) -> tuple[RunSummary, ...]:
		return tuple(item for item in self.list_runs(include_invalid=include_invalid) if item.manifest is not None and item.manifest.parent_run_id == parent_run_id)

	def start(self, run_id: str) -> RunManifest:
		return self._transition(run_id, RunStatus.RUNNING)

	def complete(self, run_id: str, *, report_state: ReportState = ReportState.NOT_APPLICABLE, incomplete_report: Mapping[str, object] | None = None) -> RunManifest:
		if report_state is ReportState.INCOMPLETE and incomplete_report is None:
			raise ValueError("Incomplete reports require details")
		return self._transition(run_id, RunStatus.COMPLETED, report_state=report_state, incomplete_report=incomplete_report)

	def fail(self, run_id: str, *, stage: str, code: str, message: str, retryable: bool = False) -> RunManifest:
		current = self.read(run_id)
		if current.status not in (RunStatus.PENDING, RunStatus.RUNNING):
			raise ValueError("Only pending or running runs may fail")
		now = self._clock()
		failure = FailureRecord(stage, code, message, now, retryable)
		return self._replace(run_id, replace(current, status=RunStatus.FAILED, timestamps=replace(current.timestamps, finished_at=now, updated_at=now), failure=failure), expected=current)

	def update_sections(self, run_id: str, *, resolved_configuration: Mapping[str, object] | None | object = _UNSET, sections: Mapping[str, Mapping[str, object]] | None = None) -> RunManifest:
		current = self.read(run_id)
		if current.status not in (RunStatus.PENDING, RunStatus.RUNNING):
			raise ValueError("Only pending or running runs may be updated")
		updates = {} if sections is None else dict(sections)
		if any(name not in _RESERVED_SECTIONS for name in updates):
			raise ValueError("Unknown or immutable manifest section")
		for name, value in updates.items():
			if not isinstance(value, Mapping):
				raise ValueError(f"Manifest section {name} must be a mapping")
		values = {name: dict(getattr(current, name)) for name in _RESERVED_SECTIONS}
		for name, value in updates.items():
			values[name].update(value)
		if resolved_configuration is _UNSET:
			resolved = current.resolved_configuration
		elif resolved_configuration is None:
			resolved = None
		elif isinstance(resolved_configuration, Mapping):
			resolved = dict(resolved_configuration)
		else:
			raise ValueError("Resolved configuration must be a mapping or null")
		updated = replace(current, resolved_configuration=resolved, identity=values["identity"], resolution=values["resolution"], universe=values["universe"], specifications=values["specifications"], provenance=values["provenance"], results=values["results"], timestamps=replace(current.timestamps, updated_at=self._clock()))
		return self._replace(run_id, updated, expected=current)

	def register_file(self, run_id: str, *, name: str, relative_path: str, kind: str = "artifact") -> ArtifactReference:
		current = self.read(run_id)
		if current.status not in (RunStatus.PENDING, RunStatus.RUNNING):
			raise ValueError("Artifacts may only be registered while a run is active")
		if kind not in ("artifact", "checkpoint"):
			raise ValueError("Artifact kind must be artifact or checkpoint")
		path = self._run_directory(run_id) / _relative_path(relative_path)
		if not path.is_file():
			raise ValueError("Artifact reference must point to an existing file")
		if kind == "artifact" and Path(relative_path).parts[0] != "artifacts":
			raise ValueError("Artifacts must be stored under the artifacts directory")
		if kind == "checkpoint" and Path(relative_path).parts[0] != "checkpoints":
			raise ValueError("Checkpoints must be stored under the checkpoints directory")
		if any(existing.name == name for existing in current.artifacts):
			raise ValueError("Artifact names must be unique within a run")
		reference = ArtifactReference(name, kind, _file_sha256(path), relative_path=relative_path)
		updated = replace(current, artifacts=(*current.artifacts, reference), timestamps=replace(current.timestamps, updated_at=self._clock()))
		self._replace(run_id, updated, expected=current)
		return reference

	def register_external(self, run_id: str, *, name: str, kind: str, sha256: str, external_reference: str) -> ArtifactReference:
		current = self.read(run_id)
		if current.status not in (RunStatus.PENDING, RunStatus.RUNNING):
			raise ValueError("Artifacts may only be registered while a run is active")
		if kind not in ("artifact", "checkpoint"):
			raise ValueError("Artifact kind must be artifact or checkpoint")
		if any(existing.name == name for existing in current.artifacts):
			raise ValueError("Artifact names must be unique within a run")
		reference = ArtifactReference(name, kind, sha256, external_reference=external_reference)
		self._replace(run_id, replace(current, artifacts=(*current.artifacts, reference), timestamps=replace(current.timestamps, updated_at=self._clock())), expected=current)
		return reference

	def _transition(self, run_id: str, status: RunStatus, *, report_state: ReportState | None = None, incomplete_report: Mapping[str, object] | None = None) -> RunManifest:
		current = self.read(run_id)
		allowed = (current.status is RunStatus.PENDING and status is RunStatus.RUNNING) or (current.status is RunStatus.RUNNING and status is RunStatus.COMPLETED)
		if not allowed:
			raise ValueError(f"Invalid saved-run transition {current.status.value} -> {status.value}")
		now = self._clock()
		timestamps = replace(current.timestamps, started_at=now if status is RunStatus.RUNNING else current.timestamps.started_at, finished_at=now if status is RunStatus.COMPLETED else None, updated_at=now)
		return self._replace(run_id, replace(current, status=status, report_state=current.report_state if report_state is None else report_state, incomplete_report=incomplete_report, timestamps=timestamps), expected=current)

	def _replace(self, run_id: str, manifest: RunManifest, *, expected: RunManifest | None = None) -> RunManifest:
		path = self._run_directory(run_id)
		with _ManifestLock(path):
			current = RunManifest.from_mapping(_safe_json_load(path / _MANIFEST_NAME))
			if current.run_id != manifest.run_id:
				raise ValueError("Manifest changed during update")
			if expected is not None and current.to_mapping() != expected.to_mapping():
				raise ValueError("Saved-run manifest changed during update")
			self._write_manifest(path, manifest)
		return manifest

	def _write_manifest(self, path: Path, manifest: RunManifest) -> None:
		atomic_write(path / _MANIFEST_NAME, encode_json(manifest.to_mapping()))

	def _run_directory(self, run_id: str) -> Path:
		_validate_run_id(run_id)
		if Path(run_id).name != run_id:
			raise ValueError("Run ID must identify one child directory")
		path = (self.runs_root / run_id).resolve()
		try:
			path.relative_to(self.runs_root)
		except ValueError as error:
			raise ValueError("Run path escapes the configured runs root") from error
		if not path.is_dir():
			raise ValueError("Saved run does not exist")
		return path


def _file_sha256(path: Path) -> str:
	digest = sha256()
	with path.open("rb") as source:
		for chunk in iter(lambda: source.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()

