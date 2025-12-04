from __future__ import annotations

import argparse
from typing import List, Optional


def general_arg_parser_understands_ranges(val: List[str]) -> List[str]:

	"""Expand simple inclusive integer ranges like "a-b" within a list of strings.

	Values that do not contain a dash are returned unchanged. For range strings,
	`a-b` is expanded to all integer values between a and b (inclusive). The
	order of a and b does not matter and negative values are clamped to 0.

	This mirrors the behaviour previously implemented inside
	`train_pytorch_network.py`.
	"""

	parsed_list: List[str] = []
	for one_val in val:
		if isinstance(one_val, str) and "-" in one_val :
			parts = one_val.split("-", 1)
			if len(parts) != 2:
				parsed_list.append(one_val)
				continue
			# right now we are only supporting integer ranges
			try:
				a = int(parts[0].strip())
				b = int(parts[1].strip())
			except Exception :
				parsed_list.append(one_val)
				continue
				#eat this, because it could be a string value for an argument like some-value-i-made-up
				#raise argparse.ArgumentTypeError(f"Invalid range value: {val} ")
			if b < a:
				a, b = b, a
			# constrain to non-negative
			a = max(0, a)
			b = max(0, b)
			parsed_list.extend(list(range(a, b + 1)))
		else:
			parsed_list.append(one_val)
	return parsed_list


def general_arg_parser_understands_lists(
	val: str,
	keyword: str,
	valid_values: Optional[List[str]] = None,
) -> List[str]:
	"""Parse comma-separated values and simple ranges into a list of strings.

	- Splits the raw string on commas
	- Trims whitespace
	- Expands any `a-b` ranges via :func:`general_arg_parser_understands_ranges`
	- Optionally validates that all entries are contained in ``valid_values``

	This helper is intended to be general-purpose and reused by scripts that
	want flexible CLI parsing of list / range style arguments.
	"""

	# First check for commas: ranges might be separated by commas, or single
	# values.
	comma_processed_string = {val.strip().lower()}
	if "," in val.strip().lower():
		parts = [p.strip() for p in val.split(",") if p.strip()]
		comma_processed_string = []  # type: ignore[assignment]
		for p in parts:
			comma_processed_string.append(p)  # type: ignore[union-attr]

	comma_and_dash_processed_string = general_arg_parser_understands_ranges(
		list(comma_processed_string)
	)
	if valid_values is None:
		return comma_and_dash_processed_string
	else:
		for one_entry in comma_and_dash_processed_string:
			if one_entry not in valid_values:
				raise argparse.ArgumentTypeError(
					f"Invalid '{keyword}' entry: '{one_entry}'. Supported: {sorted(valid_values)}"
				)
	return comma_and_dash_processed_string


def parse_float_list(val, keyword: str) -> List[float]:
	"""Parse a CLI argument that may represent one or more float values.

	The behaviour matches the original helpers from ``train_pytorch_network``:

	- If ``val`` can be parsed directly as a single float (including scientific
	  notation), return a single-element list ``[float(val)]``.
	- Otherwise, interpret ``val`` as a comma-separated list and/or ranges and
	  validate that every expanded entry can be parsed as a float.
	"""

	try:
		# in case of scientific notation or a simple single value
		float(val)
		return [float(val)]
	except Exception:
		pass

	parsed_list = general_arg_parser_understands_lists(str(val), keyword)
	for one_entry in parsed_list:
		try:
			float(one_entry)
		except Exception:
			raise argparse.ArgumentTypeError(
				f"Invalid {keyword} value: '{one_entry}'"
			)
	return [float(p) for p in parsed_list]


def parse_int_list(val, keyword: str) -> List[int]:
	"""Parse a CLI argument that may represent one or more integer values.

	Accepts the same comma and range syntax as
	:func:`general_arg_parser_understands_lists` and validates that all
	resulting entries are integers.
	"""

	parsed_list = general_arg_parser_understands_lists(str(val), keyword)
	for one_entry in parsed_list:
		try:
			int(one_entry)
		except Exception:
			raise argparse.ArgumentTypeError(
				f"Invalid {keyword} value: '{one_entry}'"
			)
	return [int(p) for p in parsed_list]


__all__ = [
	"general_arg_parser_understands_ranges",
	"general_arg_parser_understands_lists",
	"parse_float_list",
	"parse_int_list",
]
