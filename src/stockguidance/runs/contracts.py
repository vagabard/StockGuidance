"""Validated JSON contracts for saved runs.

This module intentionally has no database, network, model, or training imports.
The manifest is a durable identity record; later stages may populate its
reserved sections without changing the run-directory protocol.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from stockguidance.config.validation import require_aware_datetime, require_text


MANIFEST_SCHEMA = "stockguidance-run-1"
ARTIFACT_SHA256_LENGTH = 64
REDACTED_VALUE = "[REDACTED]"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_PATTERN = re.compile(
	r"(?:^|[_-])(api[ _-]?key|access[ _-]?key|secret|password|passwd|token|credential|private[ _-]?key)(?:$|[_-])",
	re.IGNORECASE,
)
_SECRET_TEXT_PATTERN = re.compile(
	r"(?P<prefix>\b(?:api[ _-]?key|access[ _-]?key|secret|password|passwd|token|credential)\s*[=:]\s*)(?P<value>[^\s,;]+)",
	re.IGNORECASE,
)
_RESERVED_RESULT_KEYS = (
	"predictions",
	"eligibility",
	"exclusions",
	"metrics",
	"reports",
	"trades",
	"equity",
	"benchmarks",
)


class RunOperation(StrEnum):
	TRAINING = "training"
	RECOMMENDATION = "recommendation"
	SIMULATION = "simulation"
	REPLAY = "replay"
	RERUN = "rerun"
	RESCORE = "rescore"


class RunStatus(StrEnum):
	PENDING = "pending"
	RUNNING = "running"
	COMPLETED = "completed"
	FAILED = "failed"


class ReportState(StrEnum):
	NOT_APPLICABLE = "not_applicable"
	COMPLETE = "complete"
	INCOMPLETE = "incomplete"


def _safe_text(value: str) -> str:
	return _SECRET_TEXT_PATTERN.sub(lambda match: f"{match.group('prefix')}{REDACTED_VALUE}", value)


def _secret_key(key: str) -> bool:
	return bool(_SECRET_KEY_PATTERN.search(key))


def _sanitize(value: object, *, key: str | None = None) -> object:
	if key is not None and _secret_key(key):
		return REDACTED_VALUE
	if value is None or isinstance(value, (bool, str)):
		return _safe_text(value) if isinstance(value, str) else value
	if type(value) is int:
		return value
	if type(value) is float:
		if not math.isfinite(value):
			raise ValueError("Run configuration cannot contain non-finite numbers")
		return value
	if isinstance(value, Enum):
		return _sanitize(value.value, key=key)
	if isinstance(value, datetime):
		require_aware_datetime(value, "run datetime")
		return {"__type__": "datetime", "value": value.isoformat()}
	if isinstance(value, date):
		return {"__type__": "date", "value": value.isoformat()}
	if isinstance(value, Decimal):
		return {"__type__": "decimal", "value": str(value)}
	if isinstance(value, Path):
		return {"__type__": "path", "value": value.as_posix()}
	if is_dataclass(value) and not isinstance(value, type):
		return _sanitize({field.name: getattr(value, field.name) for field in fields(value)}, key=key)
	if isinstance(value, Mapping):
		result: dict[str, object] = {}
		for raw_key, raw_value in value.items():
			if not isinstance(raw_key, str) or not raw_key.strip():
				raise ValueError("Run configuration mapping keys must be nonempty strings")
			result[raw_key] = _sanitize(raw_value, key=raw_key)
		return result
	if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
		return [_sanitize(item) for item in value]
	raise ValueError(f"Run configuration contains unsupported value type {type(value).__name__}")


def sanitize_settings(value: Mapping[str, object]) -> dict[str, object]:
	"""Return a JSON-safe copy with credential-shaped fields redacted."""

	result = _sanitize(value)
	if not isinstance(result, dict):
		raise ValueError("Run settings must be a mapping")
	return result


def _mapping(value: object, name: str) -> dict[str, object]:
	if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
		raise ValueError(f"{name} must be a string-keyed mapping")
	return dict(value)


def _required_text(payload: Mapping[str, object], name: str) -> str:
	value = payload.get(name)
	if not isinstance(value, str):
		raise ValueError(f"{name} must be text")
	return value


def _optional_mapping(value: object, name: str) -> dict[str, object] | None:
	if value is None:
		return None
	return _mapping(value, name)


def _validate_run_id(value: str, name: str = "run ID") -> str:
	if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
		raise ValueError(f"{name} must be a simple path-safe identifier")
	return value


def _validate_relative_path(value: str) -> str:
	if not isinstance(value, str) or not value or "\\" in value:
		raise ValueError("Manifest paths must be nonempty POSIX relative paths")
	path = PurePosixPath(value)
	if path.is_absolute() or ":" in path.parts or any(part in ("", ".", "..") for part in path.parts):
		raise ValueError("Manifest paths must stay inside the run directory")
	return path.as_posix()


@dataclass(frozen=True)
class RunConfig:
	"""Caller-owned settings used to create one immutable run identity."""

	operation: RunOperation
	settings: Mapping[str, object]
	parent_run_id: str | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.operation, RunOperation):
			raise ValueError("Run operation must be typed")
		sanitized = sanitize_settings(self.settings)
		object.__setattr__(self, "settings", MappingProxyType(sanitized))
		if self.parent_run_id is not None:
			_validate_run_id(self.parent_run_id, "parent run ID")


@dataclass(frozen=True)
class RunTimestamps:
	created_at: datetime
	started_at: datetime | None = None
	finished_at: datetime | None = None
	updated_at: datetime | None = None

	def __post_init__(self) -> None:
		for name, value in (("created_at", self.created_at), ("started_at", self.started_at), ("finished_at", self.finished_at), ("updated_at", self.updated_at)):
			if value is not None:
				require_aware_datetime(value, name)
		if self.started_at is not None and self.started_at < self.created_at:
			raise ValueError("Run start cannot precede creation")
		if self.finished_at is not None and self.started_at is not None and self.finished_at < self.started_at:
			raise ValueError("Run finish cannot precede start")
		if self.updated_at is not None and self.updated_at < self.created_at:
			raise ValueError("Run update cannot precede creation")


@dataclass(frozen=True)
class FailureRecord:
	stage: str
	code: str
	message: str
	failed_at: datetime
	retryable: bool = False

	def __post_init__(self) -> None:
		for value, name in ((self.stage, "failure stage"), (self.code, "failure code")):
			require_text(value, name)
		require_text(self.message, "failure message")
		require_aware_datetime(self.failed_at, "failure time")
		if type(self.retryable) is not bool:
			raise ValueError("Failure retryable flag must be boolean")
		object.__setattr__(self, "message", _safe_text(self.message))

	def to_mapping(self) -> dict[str, object]:
		return {"stage": self.stage, "code": self.code, "message": self.message, "failed_at": self.failed_at.isoformat(), "retryable": self.retryable}

	@classmethod
	def from_mapping(cls, value: object) -> "FailureRecord":
		payload = _mapping(value, "failure")
		retryable = payload.get("retryable", False)
		if type(retryable) is not bool:
			raise ValueError("Failure retryable flag must be boolean")
		return cls(
			_required_text(payload, "stage"),
			_required_text(payload, "code"),
			_required_text(payload, "message"),
			datetime.fromisoformat(_required_text(payload, "failed_at")),
			retryable,
		)


@dataclass(frozen=True)
class ArtifactReference:
	"""A hash-bound reference, never the artifact/checkpoint payload itself."""

	name: str
	kind: str
	sha256: str
	relative_path: str | None = None
	external_reference: str | None = None

	def __post_init__(self) -> None:
		require_text(self.name, "artifact name")
		require_text(self.kind, "artifact kind")
		if not _SHA256_PATTERN.fullmatch(self.sha256):
			raise ValueError("Artifact references require a lowercase SHA256 digest")
		if (self.relative_path is None) == (self.external_reference is None):
			raise ValueError("Artifact references require exactly one local or external reference")
		if self.relative_path is not None:
			_validate_relative_path(self.relative_path)
		if self.external_reference is not None:
			require_text(self.external_reference, "external artifact reference")

	def to_mapping(self) -> dict[str, object]:
		return {"name": self.name, "kind": self.kind, "sha256": self.sha256, "relative_path": self.relative_path, "external_reference": self.external_reference}

	@classmethod
	def from_mapping(cls, value: object) -> "ArtifactReference":
		payload = _mapping(value, "artifact reference")
		relative_path = payload.get("relative_path")
		external_reference = payload.get("external_reference")
		if relative_path is not None and not isinstance(relative_path, str):
			raise ValueError("Artifact relative path must be text or null")
		if external_reference is not None and not isinstance(external_reference, str):
			raise ValueError("Artifact external reference must be text or null")
		return cls(_required_text(payload, "name"), _required_text(payload, "kind"), _required_text(payload, "sha256"), relative_path, external_reference)


def _decode_datetime(value: object, name: str) -> datetime:
	if not isinstance(value, str):
		raise ValueError(f"{name} must be an ISO timestamp")
	try:
		decoded = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError(f"{name} must be an ISO timestamp") from error
	require_aware_datetime(decoded, name)
	return decoded


@dataclass(frozen=True)
class RunManifest:
	"""The complete version-1 saved-run manifest and reserved future sections."""

	run_id: str
	operation: RunOperation
	status: RunStatus
	report_state: ReportState
	timestamps: RunTimestamps
	original_configuration: Mapping[str, object]
	resolved_configuration: Mapping[str, object] | None
	parent_run_id: str | None = None
	identity: Mapping[str, object] = MappingProxyType({})
	resolution: Mapping[str, object] = MappingProxyType({})
	universe: Mapping[str, object] = MappingProxyType({})
	specifications: Mapping[str, object] = MappingProxyType({})
	provenance: Mapping[str, object] = MappingProxyType({})
	artifacts: tuple[ArtifactReference, ...] = ()
	results: Mapping[str, object] = MappingProxyType({})
	failure: FailureRecord | None = None
	incomplete_report: Mapping[str, object] | None = None

	def __post_init__(self) -> None:
		_validate_run_id(self.run_id)
		if not isinstance(self.operation, RunOperation) or not isinstance(self.status, RunStatus) or not isinstance(self.report_state, ReportState):
			raise ValueError("Manifest enum fields must be typed")
		if not isinstance(self.timestamps, RunTimestamps):
			raise ValueError("Manifest timestamps must be typed")
		if self.status is RunStatus.PENDING and (self.timestamps.started_at is not None or self.timestamps.finished_at is not None):
			raise ValueError("Pending manifests cannot have start or finish timestamps")
		if self.status is RunStatus.RUNNING and (self.timestamps.started_at is None or self.timestamps.finished_at is not None):
			raise ValueError("Running manifests require a start and no finish timestamp")
		if self.status is RunStatus.COMPLETED and (self.timestamps.started_at is None or self.timestamps.finished_at is None):
			raise ValueError("Completed manifests require start and finish timestamps")
		if self.status is RunStatus.FAILED and self.timestamps.finished_at is None:
			raise ValueError("Failed manifests require a finish timestamp")
		if self.parent_run_id is not None:
			_validate_run_id(self.parent_run_id, "parent run ID")
		for name, value in (("original configuration", self.original_configuration), ("identity", self.identity), ("resolution", self.resolution), ("universe", self.universe), ("specifications", self.specifications), ("provenance", self.provenance), ("results", self.results)):
			if not isinstance(value, Mapping):
				raise ValueError(f"Manifest {name} must be a mapping")
			_sanitize(value)
		if self.resolved_configuration is not None:
			sanitize_settings(self.resolved_configuration)
		if not isinstance(self.artifacts, tuple) or any(not isinstance(item, ArtifactReference) for item in self.artifacts):
			raise ValueError("Manifest artifact references must be typed")
		if self.status is RunStatus.FAILED and self.failure is None:
			raise ValueError("Failed manifests require failure details")
		if self.status is not RunStatus.FAILED and self.failure is not None:
			raise ValueError("Only failed manifests may contain failure details")
		if self.report_state is ReportState.INCOMPLETE:
			if self.operation is not RunOperation.SIMULATION or self.status is not RunStatus.COMPLETED or self.incomplete_report is None:
				raise ValueError("Only simulations may have incomplete reports")
		elif self.incomplete_report is not None:
			raise ValueError("Incomplete report details require incomplete report state")
		object.__setattr__(self, "original_configuration", MappingProxyType(sanitize_settings(self.original_configuration)))
		if self.resolved_configuration is not None:
			object.__setattr__(self, "resolved_configuration", MappingProxyType(sanitize_settings(self.resolved_configuration)))
		for name in ("identity", "resolution", "universe", "specifications", "provenance", "results"):
			sanitized_section = _sanitize(getattr(self, name))
			if not isinstance(sanitized_section, dict):
				raise ValueError(f"Manifest {name} must be a mapping")
			object.__setattr__(self, name, MappingProxyType(sanitized_section))
		if self.incomplete_report is not None:
			sanitized_report = _sanitize(self.incomplete_report)
			if not isinstance(sanitized_report, dict):
				raise ValueError("Incomplete report must be a mapping")
			object.__setattr__(self, "incomplete_report", MappingProxyType(sanitized_report))

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": MANIFEST_SCHEMA,
			"run_id": self.run_id,
			"parent_run_id": self.parent_run_id,
			"operation": self.operation.value,
			"status": self.status.value,
			"report_state": self.report_state.value,
			"timestamps": {
				"created_at": self.timestamps.created_at.isoformat(),
				"started_at": None if self.timestamps.started_at is None else self.timestamps.started_at.isoformat(),
				"finished_at": None if self.timestamps.finished_at is None else self.timestamps.finished_at.isoformat(),
				"updated_at": None if self.timestamps.updated_at is None else self.timestamps.updated_at.isoformat(),
			},
			"configuration": {"original": dict(self.original_configuration), "resolved": None if self.resolved_configuration is None else dict(self.resolved_configuration)},
			"identity": dict(self.identity),
			"resolution": dict(self.resolution),
			"universe": dict(self.universe),
			"specifications": dict(self.specifications),
			"provenance": dict(self.provenance),
			"artifacts": {"references": [reference.to_mapping() for reference in self.artifacts]},
			"results": {key: self.results.get(key) for key in _RESERVED_RESULT_KEYS} | {key: value for key, value in self.results.items() if key not in _RESERVED_RESULT_KEYS},
			"failure": None if self.failure is None else self.failure.to_mapping(),
			"incomplete_report": None if self.incomplete_report is None else dict(self.incomplete_report),
		}

	@classmethod
	def from_mapping(cls, value: object) -> "RunManifest":
		payload = _mapping(value, "manifest")
		if payload.get("schema") != MANIFEST_SCHEMA:
			raise ValueError("Unsupported or incomplete run manifest schema")
		timestamps = _mapping(payload.get("timestamps"), "manifest timestamps")
		configuration = _mapping(payload.get("configuration"), "manifest configuration")
		artifacts = _mapping(payload.get("artifacts"), "manifest artifacts")
		references = artifacts.get("references")
		if not isinstance(references, list):
			raise ValueError("Manifest artifact references must be a list")
		result = _mapping(payload.get("results"), "manifest results")
		created = _decode_datetime(timestamps.get("created_at"), "created_at")
		started = None if timestamps.get("started_at") is None else _decode_datetime(timestamps.get("started_at"), "started_at")
		finished = None if timestamps.get("finished_at") is None else _decode_datetime(timestamps.get("finished_at"), "finished_at")
		updated = None if timestamps.get("updated_at") is None else _decode_datetime(timestamps.get("updated_at"), "updated_at")
		failure_value = payload.get("failure")
		run_id = _required_text(payload, "run_id")
		operation = _required_text(payload, "operation")
		status = _required_text(payload, "status")
		report_state = _required_text(payload, "report_state")
		parent_run_id = payload.get("parent_run_id")
		if parent_run_id is not None and not isinstance(parent_run_id, str):
			raise ValueError("parent_run_id must be text or null")
		return cls(
			run_id,
			RunOperation(operation),
			RunStatus(status),
			ReportState(report_state),
			RunTimestamps(created, started, finished, updated),
			sanitize_settings(_mapping(configuration.get("original"), "original configuration")),
			None if configuration.get("resolved") is None else sanitize_settings(_mapping(configuration.get("resolved"), "resolved configuration")),
			parent_run_id,
			_mapping(payload.get("identity"), "manifest identity"),
			_mapping(payload.get("resolution"), "manifest resolution"),
			_mapping(payload.get("universe"), "manifest universe"),
			_mapping(payload.get("specifications"), "manifest specifications"),
			_mapping(payload.get("provenance"), "manifest provenance"),
			tuple(ArtifactReference.from_mapping(item) for item in references),
			result,
			None if failure_value is None else FailureRecord.from_mapping(failure_value),
			_optional_mapping(payload.get("incomplete_report"), "incomplete report"),
		)


@dataclass(frozen=True)
class RunSummary:
	run_id: str
	path: Path
	valid: bool
	manifest: RunManifest | None = None
	error: str | None = None

