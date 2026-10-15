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
	hidden_layer_size: Optional[List[int]]
	lr: float
	learning_rate_adjustment_parameter: float
	activation: str
	final_activation: str
	optimizer: str
	epochs: int
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
	layer_normalization: bool = True


	def __init__(
		self,
		*,
		timestamp_id: str,
		family_run: FamilyRun,
		day_change_centered: str,
		prior_days: int,
		mlp_layers: int,
		lr: float,
		hidden_layer_size: Optional[List[int]] = None,
		learning_rate_adjustment_parameter: float = 0.998,
		activation: str,
		final_activation: str,
		epochs: int,
		bias: str = "all",
		optimizer: str = "adam",
		include_features: Sequence[str] | None = None,
		file_structure: FileStructureSingleRunHandler | None = None,
		train_start_date_id: Optional[int] = None,
		train_end_date_id: Optional[int] = None,
		ticker_ids: List[int] | None = None,
		input_size: Optional[int] = None,
		output_size: Optional[int] = None,
		layer_normalization: bool = True,

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
		self.hidden_layer_size = hidden_layer_size
		self.lr = float(lr)
		self.learning_rate_adjustment_parameter = float(learning_rate_adjustment_parameter)
		self.activation = str(activation)
		self.final_activation = str(final_activation)
		self.epochs = int(epochs)
		self.bias = str(bias)
		self.optimizer = str(optimizer)
		self.include_features = include_features
		self.train_validation_test_sets_start_date_id = train_start_date_id
		self.train_validation_test_sets_end_date_id = train_end_date_id
		self.ticker_ids = ticker_ids if ticker_ids is not None else family_run.ticker_ids
		self.input_size = input_size
		self.output_size = output_size
		self.layer_normalization = layer_normalization
		if (self.train_validation_test_sets_start_date_id is None or self.train_validation_test_sets_end_date_id is None):
			date_resolver = DateBoundResolver(self.prior_days, dbinfo_file=family_run.dbinfo_file)
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

	@classmethod
	def from_args(cls, args: argparse.Namespace, family_run: Optional["FamilyRun"] = None, file_structure: Optional[FileStructureSingleRunHandler] = None) -> "SingleRun":
		hidden_layer_size = None
		if hasattr(args, "hidden_layers") and args.hidden_layers:
			hidden_layer_size = [int(x) for x in str(args.hidden_layers).split(",")]
		elif hasattr(args, "hidden_layer_size") and args.hidden_layer_size:
			hidden_layer_size = args.hidden_layer_size

		return cls(
			timestamp_id=str(args.timestamp_id),
			family_run=family_run, # type: ignore[arg-type]
			day_change_centered=str(args.day_change_centered),
			prior_days=int(args.prior_days),
			mlp_layers=int(args.mlp_layers),
			hidden_layer_size=hidden_layer_size,
			lr=float(args.lr),
			learning_rate_adjustment_parameter=float(getattr(args, "learning_rate_adjustment_parameter", 0.998)),
			activation=str(args.activation),
			final_activation=str(args.final_activation),
			optimizer=str(getattr(args, "optimizer", "adam")),
			include_features=tuple(args.include_features) if getattr(args, "include_features", None) else None,
			file_structure=file_structure,
			train_start_date_id=getattr(args, "train_start_date_id", None) or getattr(args, "train_validation_test_sets_start_date_id", None),
			train_end_date_id=getattr(args, "train_end_date_id", None) or getattr(args, "train_validation_test_sets_end_date_id", None),
			epochs=int(getattr(args, "epochs", 80)),
			bias=str(getattr(args, "bias", "all")),
			ticker_ids=getattr(args, "ticker_ids", None),
			input_size=getattr(args, "input_size", None),
			output_size=getattr(args, "output_size", None),
			layer_normalization=bool(str(getattr(args, "layer_normalization", "True")).lower() == "true"),
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
			"hidden_layer_size": _normalize_to_list(self.hidden_layer_size),
			"lr": float(self.lr),
			"learning_rate_adjustment_parameter": float(self.learning_rate_adjustment_parameter),
			"activation": str(self.activation),
			"final_activation": str(self.final_activation),
			"epochs": int(self.epochs),
			"bias": str(self.bias),
			"optimizer": str(self.optimizer),
			"timestamp_id": str(self.timestamp_id),
			"include_features": include_features,
			"train_validation_test_sets_start_date_id": self.train_validation_test_sets_start_date_id,
			"train_validation_test_sets_end_date_id": self.train_validation_test_sets_end_date_id,
			"ticker_ids": _normalize_to_list(self.ticker_ids),
			"input_size": self.input_size,
			"output_size": self.output_size,
			"layer_normalization": self.layer_normalization,
			"file_structure": self.file_structure.to_dict() if hasattr(self.file_structure, "to_dict") else str(self.file_structure),
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
		if hasattr(FamilyRun, "from_dict") and fr_dict:
			family_run = FamilyRun.from_dict(fr_dict)
		elif hasattr(FamilyRun, "from_args"):
			# If from_dict failed or isn't available, we try from_args.
			# However, from_args expects a full CLI-like Namespace which might be missing
			# critical file-structure fields when loading from a saved JSON.
			# We provide defaults to avoid crashes in FileStructureFamilyRunHandler.from_args.
			if "run_group_dir" not in fr_dict and "metrics_file" not in fr_dict:
				# Use parent or grandparent as the fallback root
				dir_path = os.path.dirname(os.path.abspath(path))
				# If we are in a run_single_... directory, we want the run_family_... dir's parent
				if "run_single_" in dir_path:
					fr_dict["run_group_dir"] = os.path.dirname(os.path.dirname(dir_path))
				else:
					fr_dict["run_group_dir"] = os.path.dirname(dir_path)
			family_run = FamilyRun.from_args(argparse.Namespace(**fr_dict))
		else:  # pragma: no cover - defensive fallback for older code
			if "learning_rate_adjustment_parameter_list" not in fr_dict:
				legacy_lrap = fr_dict.get("learning_rate_adjustment_parameter", 1.0)
				fr_dict["learning_rate_adjustment_parameter_list"] = [float(legacy_lrap)]
			family_run = FamilyRun(**fr_dict)  # type: ignore[call-arg]
		my_file_structure = FileStructureSingleRunHandler.from_dict(sr_dict["file_structure"])

		return cls.from_args(argparse.Namespace(**sr_dict), family_run=family_run, file_structure=my_file_structure)

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
