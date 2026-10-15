from dataclasses import dataclass
from enum import StrEnum

from stockguidance.config.validation import require_enum, require_text


class WorkflowStage(StrEnum):
	REQUEST = "request"
	REFRESH = "refresh"
	VALIDATION = "validation"
	ARTIFACT = "artifact"
	TRAINING = "training"
	PREDICTION = "prediction"
	PLANNING = "planning"


class FailureCode(StrEnum):
	INVALID_REQUEST = "invalid_request"
	UNSUPPORTED = "unsupported"
	PROVIDER = "provider"
	STORAGE = "storage"
	NOT_READY = "not_ready"
	INCOMPATIBLE_ARTIFACT = "incompatible_artifact"
	TRAINING_FAILED = "training_failed"
	PREDICTION_FAILED = "prediction_failed"


@dataclass(frozen=True)
class StageFailure:
	"""Messages are caller-sanitized descriptions, never credentials or raw payloads."""

	stage: WorkflowStage
	code: FailureCode
	message: str
	retryable: bool = False

	def __post_init__(self) -> None:
		require_enum(self.stage, WorkflowStage, "stage")
		require_enum(self.code, FailureCode, "code")
		require_text(self.message, "message")
		if type(self.retryable) is not bool:
			raise ValueError("retryable must be a boolean")


class StageExecutionError(RuntimeError):
	def __init__(self, failure: StageFailure) -> None:
		if not isinstance(failure, StageFailure):
			raise ValueError("failure must be a StageFailure")
		self.failure = failure
		super().__init__(f"{failure.stage.value}: {failure.code.value}: {failure.message}")
