from dataclasses import dataclass
from enum import StrEnum

from stockguidance.config.validation import require_enum, require_text


class ModelRefreshPolicy(StrEnum):
	CURRENT = "current"
	RETRAIN = "retrain"


@dataclass(frozen=True)
class ModelSelection:
	policy: ModelRefreshPolicy
	artifact_id: str | None = None

	def __post_init__(self) -> None:
		require_enum(self.policy, ModelRefreshPolicy, "policy")
		if self.policy is ModelRefreshPolicy.CURRENT:
			require_text(self.artifact_id, "current artifact_id")
		elif self.artifact_id is not None:
			raise ValueError("retrain selects its resulting artifact; do not supply an existing artifact_id")
