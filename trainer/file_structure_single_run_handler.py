from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, Tuple

from .file_structure_family_run_handler import FileStructureFamilyRunHandler
from .metric_file_handler import MetricFileHandler

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun

SUBDIRECTORY_OF_SOURCE = "data"
class FileStructureSingleRunHandler:
	"""Encapsulate per-repeat run directory and core file paths.

	One instance represents a single configuration repeat:
	- resolves ``single_rep_dir`` and ``suffix`` based on repeat index/count
	- ensures directories exist
	- exposes paths for metrics CSV and checkpoint/state files
	- owns a ``MetricFileHandler`` for the per-epoch training metrics CSV
	"""

	def __init__(
		self,
		*,
		run_group_dir: str | None = None,
		suffix: str | None = None,
		parent_file_structure: FileStructureFamilyRunHandler | None = None,
	) -> None:
		"""Initialise a run file-structure helper.

		All parameters are optional so that existing call sites that construct the
		class with no arguments continue to work. Callers that already know the
		key paths (for example when reconstructing from saved state) may provide
		those values directly here instead of relying on :meth:`create`.
		"""

		# Metrics configuration (may be refined in ``create``)
		self.run_group_dir: str | None = run_group_dir

		self.suffix: str | None = suffix

		# Misc helpers
		self.parent_file_structure: FileStructureFamilyRunHandler | None = parent_file_structure

		# Prefer the family directory when available; otherwise fall back to
		# the module root directory. This keeps behaviour consistent with
		# :meth:`from_args` while avoiding ``None`` dereferences during
		# construction.
		family_root = self.parent_file_structure.family_dir  # type: ignore[assignment]
		self.wrapper_path = self._create_wrapper_path(family_root)
		if self.run_group_dir :
			self.metrics_file = os.path.join(self.run_group_dir, f"{self.suffix}_training_metrics.csv")



	@classmethod
	def from_args(cls, args) -> FileStructureSingleRunHandler:
		"""Factory that initialises family-level paths from ``args``.

		Parameters
		----------
		args:
			Typically an :class:`argparse.Namespace` with at least a
			``metrics_file`` attribute.
		"""

		inst = cls()
		metrics_file = getattr(args, "metrics_file", None)
		if not metrics_file:
			raise ValueError("from_args expects args.metrics_file to be set")

		inst.metrics_file = str(metrics_file)
		# Derive the directory that will contain the metrics file. This is
		# needed even before per-repeat directories are created so that other
		# components (e.g. family controllers) know where the run lives.
		inst.run_directory = cls._calculate_run_directory(metrics_file)
		inst.run_group_dir = os.path.dirname(inst.metrics_file) or os.path.join("data")
		# Keep existing behaviour for wrapper_path; avoid changing caller
		inst.root_dir = os.path.dirname(os.path.abspath(__file__))
		inst.parent_file_structure = args.family_file_structure
		inst.wrapper_path = inst._create_wrapper_path(inst.parent_file_structure.family_dir)
		return inst

	@staticmethod
	def _create_wrapper_path(root_dir:str) -> str:
		# Prefer to derive the wrapper path from the provided ``root_dir`` when
		# possible, but fall back to this module's location when ``root_dir`` does
		# not contain the expected subdirectory (e.g. when tests pass in a tmp
		# directory).
		resolved_root = os.path.abspath(root_dir) if root_dir else ""
		lower_root = resolved_root.lower()
		try:
			subdirectory_index = lower_root.index(SUBDIRECTORY_OF_SOURCE)
		except ValueError:
			trainer_dir = os.path.dirname(os.path.abspath(__file__))
			return os.path.join(trainer_dir, "train_single_run.py")
		else:
			wrapper_path = resolved_root
			if subdirectory_index > 0:
				wrapper_path = resolved_root[:subdirectory_index]
			return os.path.join(wrapper_path, "trainer" + os.path.sep + "train_single_run.py")

	@staticmethod
	def _calculate_run_directory(metrics_file: str) -> str:
		"""Return the absolute directory that will contain ``metrics_file``.

		This helper is intentionally a ``@staticmethod`` so it can be used from
		both the classmethod :meth:`from_args` and instance methods such as
		:meth:`create` without requiring an instance.
		"""
		return os.path.dirname(os.path.abspath(metrics_file))

	def create(self, args, rep_index: int, family_run: FamilyRun) -> None:
		self.run_group_dir = os.path.dirname(args.metrics_file) or os.path.join("data")
		self.suffix = f"_rep{rep_index}"
		self.metrics_file = args.metrics_file
		self.base_run_dir: str
		self.run_family_prefix: str
		self.wrapper_path = self._create_wrapper_path(self.run_group_dir)
		self.parent_file_structure = family_run.file_structure

		# Ensure the repeat directory exists.
		os.makedirs(self.run_group_dir, exist_ok=True)

	def set_from_timestamp(self, timestamp: str) -> None:
		self.run_group_dir = os.path.join(self.parent_file_structure.family_dir if self.parent_file_structure.family_dir else "", timestamp)
		metrics_file_header = self.suffix
		if not metrics_file_header:
			metrics_file_header = ""
		self.metrics_file = os.path.join(self.run_group_dir, f"{metrics_file_header}_training_metrics.csv")

		os.makedirs(self.run_group_dir, exist_ok=True)

	def to_dict(self) -> Dict[str, Any]:
		"""Return a JSON-serialisable snapshot of the file structure.

		Only simple, path-like attributes are included so that callers can persist
		and later reconstruct the structure if needed.
		"""

		parent_dict: Dict[str, Any] | None
		if isinstance(self.parent_file_structure, FileStructureFamilyRunHandler):
			# Delegate to the family-level handler so that we only store
			# simple, path-like attributes instead of a live helper instance.
			parent_dict = self.parent_file_structure.to_dict()
		else:
			# For ``None`` or already-serialisable values, keep as-is. This also
			# preserves backward compatibility for any older payloads that may
			# already contain a plain dictionary.
			parent_dict = self.parent_file_structure  # type: ignore[assignment]

		return {
			"run_group_dir": self.run_group_dir,
			"suffix": self.suffix,
			"parent_file_structure": parent_dict,
			"wrapper_path": self.wrapper_path,
		}

	@classmethod
	def from_dict(cls, data: Dict[str, Any]) -> FileStructureSingleRunHandler:
		"""Reconstruct an instance from :meth:`to_dict` output.

		This performs the inverse mapping of :meth:`to_dict` for the simple
		path-like attributes. No filesystem side-effects are triggered and
		no ``MetricFileHandler`` is instantiated; callers that require an
		active metric handler should construct it separately.
		"""

		# ``parent_file_structure`` was previously stored as a live
		# ``FileStructureFamilyRunHandler`` instance, which is not
		# JSON-serialisable. New payloads store the handler as a plain
		# dictionary produced by ``FileStructureFamilyRunHandler.to_dict``.
		# Accept both forms for backwards compatibility.
		parent_raw = data.get("parent_file_structure")
		if isinstance(parent_raw, dict):
			parent_file_structure = FileStructureFamilyRunHandler.from_dict(parent_raw)
		else:
			parent_file_structure = parent_raw
		inst = cls(parent_file_structure=parent_file_structure)
		inst.metrics_file = data.get("metrics_file")
		inst.suffix = data.get("suffix")
		inst.ckpt_model = data.get("ckpt_model")
		inst.ckpt_optim = data.get("ckpt_optim")
		inst.wrapper_path = data.get("wrapper_path")
		inst.run_directory = data.get("run_directory")
		# ``metric_handler`` remains ``None``; instantiating it would require
		# I/O and is better left to callers that need it.
		return inst

	def get_directory_for_rep(self, repeat_index: int) -> str:
		return os.path.join(self.run_group_dir, f"rep{repeat_index}")

	def get_metrics_file_for_rep(self, repeat_index: int) -> str:
		rep_metrics_file = os.path.join(self.get_directory_for_rep(repeat_index), f"{self.suffix}_training_metrics.csv")
		return rep_metrics_file

	def get_metrics_file_handler_for_rep(self, repeat_index: int) -> MetricFileHandler:
		rep_metrics_file = os.path.join(self.get_directory_for_rep(repeat_index), f"{self.suffix}_training_metrics.csv")
		metric_handler = MetricFileHandler(rep_metrics_file)
		return metric_handler

	def get_checkpoint_files_for_rep(self, repeat_index: int) -> Tuple[str, str]:
		model_checkpoint = os.path.join(self.get_directory_for_rep(repeat_index), f"{self.suffix}_model_state.pt")
		optimizer_checkpoint = os.path.join(self.get_directory_for_rep(repeat_index), f"{self.suffix}_optimizer_state.pt")
		return model_checkpoint,optimizer_checkpoint


__all__ = [FileStructureSingleRunHandler]
