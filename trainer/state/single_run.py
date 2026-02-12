from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, TYPE_CHECKING, Optional

from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler
from trainer.util.date_bounds import DateBoundResolver, DateBoundType

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun


@dataclass(init=False)
class SingleRun:

	"""State for a single logical training run (excluding repeats).

	This is intended to capture the same information that is currently
	scattered across ``args`` and the various resolved hyperparameters in
	``train_pytorch_network`` / ``trainer.family_controller`` when spawning
	child subprocesses.

	The instance can turn itself into a command-line argv list that can be
	used with ``subprocess`` or when constructing run-family commands.
	"""


	# Back-reference to the creating FamilyRun controller
	family_run: FamilyRun

	# Resolved per-run hyperparameters (single configuration, not repeats)
	day_change_centered: str
	prior_days: int
	mlp_layers: int
	lr: float
	learning_rate_adjustment_parameter: float
	activation: str
	final_activation: str
	optimizer: str
	timestamp_id: str
	last_save_location:str
	# Per-run file structure helper. This is typically derived from the
	# parent ``FamilyRun`` but can be overridden when loading from disk.
	file_structure: FileStructureSingleRunHandler

	# Optional per-run feature selection override
	include_features: Sequence[str] | None = None
	train_validation_test_sets_start_date_id : Optional[int] = None # these are calculated from raw_train_start_with_special_characters and raw_train_end_with_special_characters
	train_validation_test_sets_end_date_id : Optional[int] = None
	ticker_ids: List[int] | None = None
	input_size: Optional[int] = None
	output_size: Optional[int] = None


	def __init__(
		self,
		*,
		timestamp_id: str,
		family_run: FamilyRun,
		day_change_centered: str,
		prior_days: int,
		mlp_layers: int,
		lr: float,
		learning_rate_adjustment_parameter: float = 0.998,
		activation: str,
		final_activation: str,
		optimizer: str = "adam",
		include_features: Sequence[str] | None = None,
		file_structure: FileStructureSingleRunHandler | None = None,
		train_start_date_id: Optional[int] = None,
		train_end_date_id: Optional[int] = None,
		ticker_ids: List[int] | None = None,
		input_size: Optional[int] = None,
		output_size: Optional[int] = None,

	) -> None:
		"""Explicit initialiser for :class:`SingleRun`.

		Parameters mirror the dataclass fields but ``file_structure`` is
		optional. When omitted, a fresh ``FileStructureSingleRunHandler``
		instance is created so that the object is always in a usable state.
		Callers that have a concrete file-structure instance (for example when
		loading from disk) can pass it explicitly.
		"""
		self.timestamp_id = timestamp_id
		self.family_run = family_run
		self.day_change_centered = str(day_change_centered)
		self.prior_days = int(prior_days)
		self.mlp_layers = int(mlp_layers)
		self.lr = float(lr)
		self.learning_rate_adjustment_parameter = float(learning_rate_adjustment_parameter)
		self.activation = str(activation)
		self.final_activation = str(final_activation)
		self.optimizer = str(optimizer)
		self.include_features = include_features
		self.train_validation_test_sets_start_date_id = train_start_date_id
		self.train_validation_test_sets_end_date_id = train_end_date_id
		self.ticker_ids = ticker_ids
		self.input_size = input_size
		self.output_size = output_size
		if (self.train_validation_test_sets_start_date_id is None or self.train_validation_test_sets_end_date_id is None):
			date_resolver = DateBoundResolver(self.prior_days)
			if self.train_validation_test_sets_start_date_id is None:
				self.train_validation_test_sets_start_date_id = date_resolver.resolve_bound(family_run.raw_train_start_with_special_characters, DateBoundType.START)
			if self.train_validation_test_sets_end_date_id is None:
				self.train_validation_test_sets_end_date_id = date_resolver.resolve_bound(family_run.raw_train_end_with_special_characters, DateBoundType.END)
		if self.train_validation_test_sets_start_date_id > self.train_validation_test_sets_end_date_id:
			self.train_validation_test_sets_start_date_id, self.train_validation_test_sets_end_date_id = self.train_validation_test_sets_end_date_id, self.train_validation_test_sets_start_date_id



		# Lazily construct a per-run file-structure helper when one is not
		# supplied (for example when creating runs from a FamilyRun sweep).
		#
		# ``file_structure`` is kept when explicitly provided (e.g. by
		# :meth:`load`), so any paths restored from JSON are preserved.
		self.file_structure = file_structure if file_structure is not None else FileStructureSingleRunHandler( parent_file_structure=family_run.file_structure)
		self.file_structure.set_from_timestamp(self.timestamp_id)
		# Wire the per-run helper back to the family's file structure when
		# available so that timestamp-based directories can be derived.
		self.last_save_location = ""




	# ------------------------------------------------------------------
	# Serialization helpers
	# ------------------------------------------------------------------

	def from_args(self,args:argparse.Namespace) -> SingleRun:
		return SingleRun(
			timestamp_id=args.timestamp_id,
			family_run=self.family_run,
			day_change_centered=str(args.day_change_centered),
			prior_days=int(args.prior_days),
			mlp_layers=int(args.mlp_layers),
			lr=float(args.lr),
			learning_rate_adjustment_parameter=float(getattr(args, "learning_rate_adjustment_parameter", 0.998)),
			activation=str(args.activation),
			final_activation=str(args.final_activation),
			optimizer=str(getattr(args, "optimizer", "adam")),
			include_features=getattr(args, "include_features", None),
			file_structure=self.file_structure,
			train_start_date_id=getattr(args, "train_validation_test_sets_start_date_id", None),
			train_end_date_id=getattr(args, "train_validation_test_sets_end_date_id", None),
		)

	def to_dict(self) -> Dict[str, Any]:
		"""Return a JSON-serialisable representation of this SingleRun.

		The associated FamilyRun is deliberately *not* included here to avoid
		circular references. Call :meth:`save` if you need a combined
		``{single_run, single_run}`` payload.
		"""

		include_features: list[str] | None
		if self.include_features is None:
			include_features = None
		else:
			include_features = [str(f) for f in self.include_features]

		def _normalize_to_list(val: Any) -> Any:
			if hasattr(val, "tolist"):  # Handle numpy arrays
				return val.tolist()
			if isinstance(val, (list, tuple, set)):
				return [_normalize_to_list(v) for v in val]
			return val

		return {
			"day_change_centered": str(self.day_change_centered),
			"prior_days": int(self.prior_days),
			"mlp_layers": int(self.mlp_layers),
			"lr": float(self.lr),
			"learning_rate_adjustment_parameter": float(self.learning_rate_adjustment_parameter),
			"activation": str(self.activation),
			"final_activation": str(self.final_activation),
			"optimizer": str(self.optimizer),
			"timestamp_id": str(self.timestamp_id),
			"include_features": include_features,
			"train_validation_test_sets_start_date_id": self.train_validation_test_sets_start_date_id,
			"train_validation_test_sets_end_date_id": self.train_validation_test_sets_end_date_id,
			"ticker_ids": _normalize_to_list(self.ticker_ids),
			"input_size": self.input_size,
			"output_size": self.output_size,
			"file_structure": self.file_structure.to_dict()
		}

	def save(self, path: str) -> Dict[str, Any]:
		"""Persist this run (and its parent FamilyRun) to ``path`` as JSON.

		The JSON structure is::

			{"single_run": {...}, "single_run": {...}}

		where ``single_run`` comes from ``FamilyRun.to_dict()`` and
		``single_run`` from :meth:`to_dict`.
		"""

		from trainer.state.family_run import FamilyRun  # local import to avoid cycles

		if not isinstance(self.family_run, FamilyRun):  # type: ignore[arg-type]
			raise TypeError("SingleRun.single_run must be a FamilyRun instance to save")

		family_payload = self.family_run.to_dict()
		single_payload = self.to_dict()
		payload: Dict[str, Any] = {
			"family_run": family_payload,
			"single_run": single_payload,
		}
		os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
		with open(path, "w", encoding="utf-8") as f:
			json.dump(payload, f, indent=2, sort_keys=True)
		self.last_save_location=os.path.abspath(path)
		return payload

	@classmethod
	def load(cls, path: str) -> "SingleRun":
		"""Load a ``SingleRun`` (and embedded ``FamilyRun``) from JSON.

		The file must have been produced by :meth:`save` and therefore contain
		both a ``single_run`` and a ``single_run`` section.
		"""

		from trainer.state.family_run import FamilyRun  # local import to avoid cycles

		with open(path, "r", encoding="utf-8") as f:
			data: Dict[str, Any] = json.load(f)

		fr_dict = data.get("family_run") or {}
		sr_dict = data.get("single_run") or {}

		# Use the dedicated helper so that ``file_structure`` and set/list
		# fields are reconstructed correctly from JSON.
		if hasattr(FamilyRun, "from_dict"):
			family_run = FamilyRun.from_dict(fr_dict)  # type: ignore[assignment]
		else:  # pragma: no cover - defensive fallback for older code
			family_run = FamilyRun(**fr_dict)  # type: ignore[call-arg]
		my_file_structure = FileStructureSingleRunHandler.from_dict(sr_dict["file_structure"])
		return cls(
			timestamp_id=str(sr_dict["timestamp_id"]),
			family_run=family_run,
			day_change_centered=str(sr_dict["day_change_centered"]),
			prior_days=int(sr_dict["prior_days"]),
			mlp_layers=int(sr_dict["mlp_layers"]),
			lr=float(sr_dict["lr"]),
			learning_rate_adjustment_parameter=float(sr_dict.get("learning_rate_adjustment_parameter", 0.998)),
			activation=str(sr_dict["activation"]),
			final_activation=str(sr_dict["final_activation"]),
			optimizer=str(sr_dict.get("optimizer", "adam")),
			include_features=tuple(sr_dict.get("include_features") or []),
			file_structure=my_file_structure,
			train_start_date_id=sr_dict.get("train_validation_test_sets_start_date_id"),
			train_end_date_id=sr_dict.get("train_validation_test_sets_end_date_id"),
			ticker_ids=sr_dict.get("ticker_ids"),
			input_size=sr_dict.get("input_size"),
			output_size=sr_dict.get("output_size"),
		)

	def to_commandline(self) -> List[str]:
		"""Convert this state into a concrete command-line argv list.
		"""

		def _flag(name: str) -> str:
			return "--" + name.replace("_", "-")
		wrapper_path = self.file_structure.wrapper_path
		argv: List[str] = [sys.executable, wrapper_path]

		argv += [_flag("single_run_file"), str(self.last_save_location)]
		return argv


__all__ = ["SingleRun"]
