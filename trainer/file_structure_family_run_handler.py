from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict

from .metric_file_handler import MetricFileHandler

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	pass

RUN_FAMILY_PREFIX = "run_family_"
class FileStructureFamilyRunHandler:
	"""Encapsulate per-repeat run directory and core file paths.

	One instance represents a single configuration repeat:
	- resolves ``single_rep_dir`` and ``suffix`` based on repeat index/count
	- ensures directories exist
	- exposes paths for metrics CSV and checkpoint/state files
	- owns a ``MetricFileHandler`` for the per-epoch training metrics CSV
	"""

	def __init__(self) -> None:
		"""Lightweight initialiser; prefer :meth:`from_args` for normal use.

		The actual path resolution is performed by :meth:`from_args` (family-level
		paths) and :meth:`create` (per-repeat paths).
		"""

		# Family-level paths
		self.family_state_path = None
		self.family_dir: str | None = None
		self.family_root_parent: str | None = None

		# Misc helpers
		self.metric_handler: MetricFileHandler | None = None

	@classmethod
	def from_args(cls, args) -> FileStructureFamilyRunHandler:
		"""Construct a basic file-structure helper from CLI ``args``.

		Only *family-level* information is derived here. In particular we
		resolve the directory that will host all runs in the family
		(``family_root_parent``) from the configured ``metrics_file`` (or an
		explicit ``run_group_dir`` attribute if present). The concrete
		``family_dir`` is created later by :meth:`set_from_initial_id` once an
		``initial_id`` is known.

		This mirrors the role of :meth:`FileStructureSingleRunHandler.from_args`
		but keeps the surface area minimal for the family controller.
		"""

		inst = cls()

		# Prefer an explicit run_group_dir (used by some tests / harnesses); fall
		# back to inferring it from ``metrics_file`` when available.
		metrics_dir = getattr(args, "run_group_dir", None)
		metrics_file = getattr(args, "metrics_file", None)
		if not metrics_dir:
			if not metrics_file:
				raise ValueError("from_args expects args.metrics_file or args.run_group_dir to be set")
			metrics_dir = os.path.dirname(os.path.abspath(str(metrics_file))) or os.path.join("data")
		else:
			metrics_dir = os.path.abspath(str(metrics_dir))

		inst.family_root_parent = metrics_dir
		inst.metric_handler = MetricFileHandler(metrics_file)
		return inst

	def set_from_initial_id(self, initial_id: str) -> None:
		self.family_dir = os.path.join(self.family_root_parent, f"{RUN_FAMILY_PREFIX}{initial_id}")
		self.family_state_path = os.path.join(self.family_dir, "family_state.json")
		os.makedirs(self.family_dir, exist_ok=True)

	def to_dict(self) -> Dict[str, Any]:
		"""Return a JSON-serialisable snapshot of the file structure.

		Only simple, path-like attributes are included so that callers can persist
		and later reconstruct the structure if needed.
		"""

		return {
			"family_dir": self.family_dir,
			"family_root_parent": self.family_root_parent,
			"family_state_path": self.family_state_path,
		}

	@classmethod
	def from_dict(cls, data: Dict[str, Any]) -> FileStructureFamilyRunHandler:
		"""Reconstruct an instance from :meth:`to_dict` output.

		This performs the inverse mapping of :meth:`to_dict` for the simple
		path-like attributes. No filesystem side-effects are triggered and
		no ``MetricFileHandler`` is instantiated; callers that require an
		active metric handler should construct it separately.
		"""

		inst = cls()
		inst.family_dir = data.get("family_dir")
		inst.family_root_parent = data.get("family_root_parent")
		inst.family_state_path = data.get("family_state_path")
		# ``metric_handler`` remains ``None``; instantiating it would require
		# I/O and is better left to callers that need it.
		return inst

	@classmethod
	def load(cls, resume_dir: str) -> FileStructureFamilyRunHandler:
		"""Factory that locates an existing ``family_state.json``.

		This mirrors the expectations of :meth:`FamilyRun.load`, returning a
		``FileStructureFamilyRunHandler`` with ``family_dir`` and ``family_state_path`` set.
		"""

		family_dir = os.path.abspath(resume_dir)
		state_path = os.path.join(family_dir, "family_state.json")
		if not os.path.exists(state_path):
			raise RuntimeError(f"No family_state.json found in resume-directory: {family_dir}")

		inst = cls()
		inst.family_dir = family_dir
		inst.family_state_path = state_path
		# Infer surrounding layout as best we can from the resume directory
		inst.family_root_parent = os.path.dirname(family_dir)
		return inst







__all__ = ["FileStructureFamilyRunHandler"]
