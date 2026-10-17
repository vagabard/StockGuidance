"""Canonical JSON bytes for the versioned Python/Java interchange contract."""

from decimal import Decimal, InvalidOperation
import json
from math import isfinite
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
	return ("".join(_parts(value)) + "\n").encode("utf-8")


def canonical_sha256(value: Any) -> str:
	from hashlib import sha256

	return sha256(canonical_json_bytes(value)).hexdigest()


def _parts(value: Any) -> list[str]:
	if value is None:
		return ["null"]
	if value is True:
		return ["true"]
	if value is False:
		return ["false"]
	if isinstance(value, str):
		return [json.dumps(value, ensure_ascii=True, separators=(",", ":"))]
	if isinstance(value, int) and not isinstance(value, bool):
		return [str(value)]
	if isinstance(value, (float, Decimal)):
		try:
			number = Decimal(str(value))
		except InvalidOperation as error:
			raise ValueError("Canonical numbers must be finite") from error
		if not number.is_finite() or (isinstance(value, float) and not isfinite(value)):
			raise ValueError("Canonical numbers must be finite")
		plain = format(number, "f")
		if "." in plain:
			plain = plain.rstrip("0").rstrip(".")
		return ["0" if number.is_zero() else plain or "0"]
	if isinstance(value, list | tuple):
		return ["[", ",".join("".join(_parts(item)) for item in value), "]"]
	if isinstance(value, dict):
		if any(not isinstance(key, str) for key in value):
			raise ValueError("Canonical object keys must be text")
		items = []
		for key in sorted(value):
			items.append("".join(_parts(key)) + ":" + "".join(_parts(value[key])))
		return ["{", ",".join(items), "}"]
	raise TypeError(f"Unsupported canonical value: {type(value).__name__}")
