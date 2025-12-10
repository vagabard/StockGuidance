from __future__ import annotations

from typing import Iterable

from trainer.loss_functions.ranking import ranking_score, ranking_max_score


def ranking_error(
	predicted_order: Iterable,
	true_order: Iterable,
) -> float:
	"""Return a normalized error in [0, 1] where 0 is perfect ranking and 1 is worst.

	Error = 1 - (score / max_score).
	If ``max_score`` is 0 (degenerate N=0), returns 0.0.
	"""
	s = ranking_score(predicted_order, true_order)
	m = ranking_max_score(len(list(true_order)))
	return 0.0 if m == 0.0 else max(0.0, min(1.0, 1.0 - (s / m)))


__all__ = ["ranking_error"]
