from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, TYPE_CHECKING

from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler

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
	family_run: "FamilyRun"

	# Resolved per-run hyperparameters (single configuration, not repeats)
	day_change_centered: str
	prior_days: int
	mlp_layers: int
	lr: float
	activation: str
	final_activation: str
	timestamp_id: str
	last_save_location:str

	# Per-run file structure helper. This is typically derived from the
	# parent ``FamilyRun`` but can be overridden when loading from disk.
	file_structure: FileStructureSingleRunHandler

	# Optional per-run feature selection override
	include_features: Sequence[str] | None = None

	def __init__(
		self,
		*,
		timestamp_id: str,
		family_run: FamilyRun,
		day_change_centered: str,
		prior_days: int,
		mlp_layers: int,
		lr: float,
		activation: str,
		final_activation: str,
		include_features: Sequence[str] | None = None,
		file_structure: FileStructureSingleRunHandler | None = None,

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
		self.activation = str(activation)
		self.final_activation = str(final_activation)
		self.include_features = include_features
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
			activation=str(args.activation),
			final_activation=str(args.final_activation),
			include_features=getattr(args, "include_features", None),
			file_structure=self.file_structure,
		)

	def to_dict(self) -> Dict[str, Any]:
		"""Return a JSON-serialisable representation of this SingleRun.

		The associated FamilyRun is deliberately *not* included here to avoid
		circular references. Call :meth:`save` if you need a combined
		``{family_run, single_run}`` payload.
		"""

		include_features: list[str] | None
		if self.include_features is None:
			include_features = None
		else:
			include_features = [str(f) for f in self.include_features]

		return {
			"day_change_centered": str(self.day_change_centered),
			"prior_days": int(self.prior_days),
			"mlp_layers": int(self.mlp_layers),
			"lr": float(self.lr),
			"activation": str(self.activation),
			"final_activation": str(self.final_activation),
			"timestamp_id": str(self.timestamp_id),
			"include_features": include_features,
			"file_structure": self.file_structure.to_dict()

			#family_run: "FamilyRun"




		}

	def save(self, path: str) -> Dict[str, Any]:
		"""Persist this run (and its parent FamilyRun) to ``path`` as JSON.

		The JSON structure is::

		    {"family_run": {...}, "single_run": {...}}

		where ``family_run`` comes from ``FamilyRun.to_dict()`` and
		``single_run`` from :meth:`to_dict`.
		"""

		from trainer.state.family_run import FamilyRun  # local import to avoid cycles

		if not isinstance(self.family_run, FamilyRun):  # type: ignore[arg-type]
			raise TypeError("SingleRun.family_run must be a FamilyRun instance to save")

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
		both a ``family_run`` and a ``single_run`` section.
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
			activation=str(sr_dict["activation"]),
			final_activation=str(sr_dict["final_activation"]),
			include_features=tuple(sr_dict.get("include_features") or []),
			file_structure=my_file_structure
		)

	def to_commandline(self) -> List[str]:
		"""Convert this state into a concrete command-line argv list.
		"""

		def _flag(name: str) -> str:
			return "--" + name.replace("_", "-")
		wrapper_path = self.file_structure.wrapper_path
		argv: List[str] = [sys.executable, wrapper_path]

		argv += [_flag("single_run_file"), str(self.last_save_location)]
		#   OLD WAY, we now just pass in a singlerun serialized json file
		# Core run-identifying parameters
		#argv += [_flag("day_change_centered"), str(self.day_change_centered)]
		# argv += [_flag("prior_days"), str(int(self.prior_days))]
		# argv += [_flag("mlp_layers"), str(int(self.mlp_layers))]
		# argv += [_flag("lr"), str(float(self.lr))]
		# argv += [_flag("activation"), str(self.activation)]
		# argv += [_flag("final_activation"), str(self.final_activation)]
		#
		# # Common training hyperparameters pulled from args when present.
		# if hasattr(self.family_run, "epochs"):
		# 	argv += [_flag("epochs"), str(int(self.family_run.total_epochs_to_train))]
		# if hasattr(self.family_run, "batch_size"):
		# 	argv += [_flag("batch_size"), str(int(self.family_run.batch_size))]
		# if hasattr(self.family_run, "hidden_layers"):
		# 	argv += [_flag("hidden_layers"), str(int(self.family_run.hidden_layers))]
		# if hasattr(self.family_run, "device"):
		# 	argv += [_flag("device"), str(self.family_run.device)]
		# if hasattr(self.family_run, "limit") and self.family_run.limit is not None:
		# 	argv += [_flag("limit"), str(int(self.family_run.limit))]
		# if hasattr(self.family_run, "log_interval"):
		# 	argv += [_flag("log_interval"), str(int(self.family_run.log_interval))]
		# if hasattr(self.family_run, "rank_lower"):
		# 	argv += [_flag("rank_lower"), str(int(self.family_run.rank_lower))]
		# if hasattr(self.family_run, "rank_upper"):
		# 	argv += [_flag("rank_upper"), str(int(self.family_run.rank_upper))]
		# if bool(getattr(self.family_run, "use_ranking_error_loss", False)):
		# 	argv += [_flag("use_ranking_error_loss")]
		# 	argv += [_flag("rank_temp"), str(float(getattr(self.family_run, "rank_temp", 1.0)))]
		# if bool(getattr(self.family_run, "enable_rank_err", False)):
		# 	argv += [_flag("enable_rank_err")]
		# if hasattr(self.family_run, "split_mode"):
		# 	argv += [_flag("split_mode"), str(self.family_run.split_mode)]
		# if getattr(self.family_run, "seed", None) is not None:
		# 	argv += [_flag("seed"), str(int(self.family_run.random_seed_splits))]
		# if hasattr(self.family_run, "precision"):
		# 	argv += [_flag("precision"), str(self.family_run.precision)]
		# if hasattr(self.family_run, "arch"):
		# 	argv += [_flag("arch"), str(self.family_run.arch)]
		# if bool(getattr(self.family_run, "dont_use_dataloader", False)):
		# 	argv += [_flag("dont_use_dataloader")]
		# if bool(getattr(self.family_run, "shuffle", False)):
		# 	argv += [_flag("shuffle")]
		# if int(getattr(self.family_run, "num_workers", 0) or 0) > 0:
		# 	argv += [_flag("num_workers"), str(int(self.family_run.number_of_GPU_workers))]
		# if bool(getattr(self.family_run, "pin_memory", False)):
		# 	argv += [_flag("pin_memory")]
		#
		# # Simulation-related options (if enabled)
		# if bool(getattr(self.family_run, "simulate_after", False)):
		# 	argv += [_flag("simulate_after")]
		# if hasattr(self.family_run, "simulate_percent"):
		# 	argv += [_flag("simulate_percent"), str(float(getattr(self.family_run, "simulate_percent", 0.0)))]
		# if int(getattr(self.family_run, "trading_interval", 0) or 0) > 0:
		# 	argv += [_flag("trading_interval"), str(int(getattr(self.family_run, "trading_interval", 0) or 0))]
		# 	argv += [_flag("simulate_top_k"), str(getattr(self.family_run, "simulate_top_k", "10"))]
		# 	if bool(getattr(self.family_run, "simulate_prior_only", False)):
		# 		argv += [_flag("simulate_prior_only")]
		# if int(getattr(self.family_run, "ensemble_interval", 0) or 0) > 0:
		# 	argv += [
		# 		_flag("ensemble_interval"),
		# 		str(int(getattr(self.family_run, "ensemble_interval", 0) or 0)),
		# 	]
		#
		# # Optional feature selection override for this specific run.
		# if self.include_features:
		# 	include_str = ",".join(sorted(str(f) for f in self.include_features))
		# 	argv += [_flag("include_features"), include_str]
		#
		# argv += [
		# 	_flag("run-repeats"),
		# 	str(self.family_run.run_repeats),
		# ]
		# if self.family_run.train_start_date_id is not None:
		# 	argv += [
		# 		_flag("train-start-date-id"),
		# 		self.family_run.train_start_date_id,
		# 	]
		# if self.family_run.train_end_date_id is not None:
		# 	argv += [
		# 		_flag("train-end-date-id"),
		# 		self.family_run.train_end_date_id,
		# 	]
		# metrics_override = os.path.join(self.file_structure.run_group_dir, "training_metrics.csv")
		# argv += [_flag("metrics-file"), metrics_override]
		# argv += [_flag("initial_run"), self.family_run.initial_run_id]

		return argv


__all__ = ["SingleRun"]
