from __future__ import annotations

import math
import os
from typing import Optional, List, TYPE_CHECKING

from trainer.util.date_bounds import DateBoundResolver

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun


def _derive_dates_tail(
	percent: float,
	min_date_id: int,
	max_date_id: int,
) -> Optional[List[int]]:
	"""Legacy helper: pick the *last* ``percent`` of the global date range.

	This preserves the previous behaviour that based ``simulate_percent`` on the
	whole available [min_date_id, max_date_id] window and selected a *tail*
	segment. It is now only used as a fallback when we do not have sufficient
	information about the training window.
	"""
	try:
		total_days = max_date_id - min_date_id + 1
		if total_days <= 0:
			return None
		count = max(1, int(math.ceil(total_days * (percent / 100.0))))
		start_id = max_date_id - count + 1
		if start_id < min_date_id:
			start_id = min_date_id
		return list(range(int(start_id), int(max_date_id) + 1))
	except Exception as exc:  # pragma: no cover - defensive
		print(
			"Warning: failed to derive dates_to_handle from simulate_percent; "
			f"running on all dates instead: {exc}"
		)
		return None


class SimulationDateRangeBuilder:

	def build_dates_to_handle_for_simulation(self, family_run: "FamilyRun") -> Optional[List[int]]:
		"""Convert ``args.simulate_percent`` into a concrete list of ``date_id`` values.

		This mirrors the original helper inside ``train_pytorch_network`` but is now
		available as a reusable utility.
		"""

		percent = family_run.simulate_percent

		# Percent <= 0 or missing means "use all dates" -> let simulate_trading decide.
		if percent is None or percent <= 0.0:
			return None

		# Clamp excessively large values to 100%.
		if percent > 100.0:
			percent = 100.0

		load_date_id_range = self._import_load_date_id_range()
		if load_date_id_range is None:
			return None

		try:
			min_date_id, max_date_id = load_date_id_range(
				host=os.getenv("MYSQL_HOST", "localhost"),
				database=os.getenv("MYSQL_DB", "stockforecasting"),
				user=os.getenv("MYSQL_USER", "Redbeard"),
				password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
				port=int(os.getenv("MYSQL_PORT", "3306")),
				driver="auto",
			)
		except Exception as exc:  # pragma: no cover - defensive
			print(
				"Warning: failed to query date_id range for simulate_percent; "
				f"running on all dates instead: {exc}"
			)
			return None

		# Build a contiguous list of date_ids for resolving relative bounds such as
		# "-1100" in FamilyRun.train_start_date_id.
		all_dates = list(range(int(min_date_id), int(max_date_id) + 1))
		if not all_dates:
			return None
		resolver = DateBoundResolver(all_dates)

		# Determine the effective window [train_start_id, train_end_id] to which
		# the training percent applies. If train_* ids are missing we fall back to
		# the whole available range.
		raw_train_start = getattr(family_run, "train_start_date_id", None)
		raw_train_end = getattr(family_run, "train_end_date_id", None)
		try:
			if raw_train_start is not None and raw_train_end is not None:
				train_start_id = int(resolver.resolve_bound(raw_train_start, "start"))
				train_end_id = int(resolver.resolve_bound(raw_train_end, "end"))
			else:
				# No explicit training window – use the full DB range.
				train_start_id = int(min_date_id)
				train_end_id = int(max_date_id)
		except Exception as exc:  # pragma: no cover - defensive
			print(
				"Warning: failed to resolve training date window for simulate_percent; "
				f"falling back to legacy behaviour: {exc}"
			)
			return _derive_dates_tail(percent, int(min_date_id), int(max_date_id))

		if train_start_id > train_end_id:
			train_start_id, train_end_id = train_end_id, train_start_id

		# Compute how much of this window is used for training based on
		# family_run.train_percent, then take the remaining dates as candidates for
		# simulation.
		train_percent = getattr(family_run, "train_percent", None)
		if train_percent is None:
			# Without a training percent we cannot know how many dates remain.
			return _derive_dates_tail(percent, int(min_date_id), int(max_date_id))

		# ``train_percent`` is historically a fraction (0.9) rather than 90.
		if train_percent > 1.0:
			train_fraction = float(train_percent) / 100.0
		else:
			train_fraction = float(train_percent)
		if train_fraction < 0.0:
			train_fraction = 0.0
		elif train_fraction > 1.0:
			train_fraction = 1.0

		total_days = int(train_end_id) - int(train_start_id) + 1
		if total_days <= 0:
			return None

		training_days = int(math.floor(total_days * train_fraction))
		if training_days < 0:
			training_days = 0
		if training_days > total_days:
			training_days = total_days

		# Remaining dates after the training window – these are the candidates for
		# simulation. Training is assumed to use the earliest dates.
		remaining_days = total_days - training_days
		if remaining_days <= 0:
			# Nothing left to simulate on.
			return None

		remaining_start_id = int(train_start_id) + training_days
		remaining_end_id = int(train_end_id)
		if remaining_start_id > remaining_end_id:
			remaining_start_id, remaining_end_id = remaining_end_id, remaining_start_id

		candidate_dates: List[int] = list(range(remaining_start_id, remaining_end_id + 1))
		if not candidate_dates:
			return None

		# ``percent`` now specifies how much of the **remaining** dates to use for
		# simulation. 100% means all remaining instances, 50% means the first half,
		# and so on.
		fraction = float(percent) / 100.0
		if fraction <= 0.0:
			return None
		if fraction > 1.0:
			fraction = 1.0

		count = max(1, int(math.ceil(len(candidate_dates) * fraction)))
		return candidate_dates[:count]

	# ----- Internal helpers -------------------------------------------------

	@staticmethod
	def _import_load_date_id_range():
		try:
			from load_date_id_range import load_date_id_range as _load_date_id_range
		except Exception as exc:  # pragma: no cover - defensive
			print(
				"Warning: could not import load_date_id_range; running simulation on "
				f"all dates instead: {exc}"
			)
			return None
		return _load_date_id_range


def build_dates_to_handle_for_simulation(family_run: "FamilyRun") -> Optional[List[int]]:
	"""Convenience wrapper used by callers that previously relied on the
	nested ``_build_dates_to_handle_for_simulation`` helper.
	"""

	builder = SimulationDateRangeBuilder()
	return builder.build_dates_to_handle_for_simulation(family_run)


__all__ = [
	"SimulationDateRangeBuilder",
	"build_dates_to_handle_for_simulation",
]
