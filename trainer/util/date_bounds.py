from __future__ import annotations

from typing import Any, Sequence, List


class DateBoundResolver:
    """Resolve user-specified train/validation window bounds to concrete date_ids.

    This is extracted from the `_resolve_bound` helper that previously lived as a
    nested function inside `train_pytorch_network._run_one`.
    """

    def __init__(self, usable_dates: Sequence[int]):
        if not usable_dates:
            raise RuntimeError("No usable dates available for training window computation")
        self._usable_dates: List[int] = [int(d) for d in usable_dates]

    def resolve_bound(self, spec: Any, which: str) -> int:
        """Resolve a user-specified bound to an absolute ``date_id``.

        ``spec`` may be ``None`` (use first/last), an absolute integer date_id,
        or a relative index in string form:

        * "+N" -> date at index N from the *start* (0-based) of ``usable_dates``
        * "-N" -> date at index ``len(usable_dates) - N`` from the *start*, i.e.
          N steps back from the last available date.
        """

        usable_dates = self._usable_dates
        print(spec, "   is type ", type(spec))

        if spec is None:
            return int(usable_dates[0] if which == "start" else usable_dates[-1])

        if isinstance(spec, int):
            return int(spec)

        # Normalise to string for parsing
        text = str(spec).strip()
        if not text:
            return int(usable_dates[0] if which == "start" else usable_dates[-1])
        # Relative from start or end
        if text[0] in "+-" and len(text) > 1:
            try:
                offset = int(text)
            except ValueError:
                raise ValueError(
                    f"Invalid {which} date specifier '{spec}': expected integer, '+N' or '-N'"
                )
            length = len(usable_dates)
            if offset >= 0:
                # '+N' (or 'N' with leading '+'): index from start, 0-based
                idx = offset
            else:
                # '-N': N steps back from the end
                idx = length + offset
            if idx < 0 or idx >= length:
                raise ValueError(
                    f"Resolved {which} index {idx} from spec '{spec}' is outside the "
                    f"available date_id range [0, {length - 1}]"
                )
            return int(usable_dates[idx])

        # Fallback: treat as absolute date_id
        try:
            return int(text)
        except ValueError as ve:
            raise ValueError(
                f"Invalid {which} date specifier '{spec}': expected integer, '+N' or '-N'"
            ) from ve


__all__ = ["DateBoundResolver"]
