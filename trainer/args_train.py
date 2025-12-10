from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from trainer.util.argparse_utils import (
	general_arg_parser_understands_lists,
	parse_float_list,
	parse_int_list,
)


FEATURE_KEY_TO_ATTR: Dict[str, str] = {
	"price-change-today": "change_in_price_today",
	"volume-ratio": "volume_ratio",
	"vwap-ratio": "vwap_ratio",
	"transaction-ratio": "transaction_ratio",
}


def set_feature_flags(args: Any, selected: Set[str]) -> None:
	"""Populate feature-related boolean flags on ``args``.

	This mirrors the logic that previously lived inside
	``train_pytorch_network._main_impl`` and is kept here so that
	argument/feature handling can be reused in other entry points.
	"""
	for key, attr_name in FEATURE_KEY_TO_ATTR.items():
		value = key in selected
		setattr(args, attr_name, value)
	# Maintain the historical misspelled attribute name expected by some code.
	setattr(args, "tranaction_ratio", getattr(args, "transaction_ratio", False))


def parse_feature_selections(args: Any) -> Tuple[Set[str], List[str]]:
	"""Parse ``--include-features`` and ``--include-one-feature`` from ``args``.

	Returns ``(base_optional_features, include_one_feature_list)`` where:

	* ``base_optional_features`` is the set derived from ``include_features``.
	* ``include_one_feature_list`` is the list derived from
	  ``include_one_feature`` (may be empty).
	"""
	valid_keys: List[str] = list(FEATURE_KEY_TO_ATTR.keys())

	raw_include = str(getattr(args, "include_features", "") or "")
	if raw_include.strip():
		base_optional_features: Set[str] = set(
			general_arg_parser_understands_lists(
				raw_include,
				"include_features",
				valid_values=valid_keys,
			)
		)
	else:
		base_optional_features = set()

	raw_include_one = str(getattr(args, "include_one_feature", "") or "")
	if raw_include_one.strip():
		include_one_feature_list: List[str] = general_arg_parser_understands_lists(
			raw_include_one,
			"include-one-feature",
			valid_values=valid_keys,
		)
	else:
		include_one_feature_list = []

	return base_optional_features, include_one_feature_list


def parse_sweep_lists(
	args: Any,
	valid_activations: Optional[Iterable[str]] = None,
	valid_day_centers: Optional[Iterable[str]] = None,
) -> Tuple[List[int], List[int], List[float], List[str], List[str], List[str]]:
	"""Parse and normalise the main sweepable option lists for training.

	This consolidates the list / range parsing previously in
	``train_pytorch_network._main_impl`` so that other modules can reuse the
	behaviour while keeping it in a single place.

	Returns a tuple:
	``(prior_days_list, mlp_layers_list, lr_list, activation_list,
	final_activation_list, day_change_centered_list)``.
	"""

	prior_days_list = parse_int_list(getattr(args, "prior_days"), "prior_days")
	mlp_layers_list = parse_int_list(getattr(args, "mlp_layers"), "mlp_layers")
	lr_list = parse_float_list(getattr(args, "lr"), "lr")

	activation_list = general_arg_parser_understands_lists(
		getattr(args, "activation", "relu"),
		"activation",
		valid_values=list(valid_activations) if valid_activations is not None else None,
	)
	final_activation_list = general_arg_parser_understands_lists(
		getattr(args, "final_activation", "None"),
		"final_activation",
		valid_values=list(valid_activations) if valid_activations is not None else None,
	)
	day_change_centered_list = general_arg_parser_understands_lists(
		getattr(args, "day_change_centered", "one"),
		"day_change_centered",
		valid_values=list(valid_day_centers) if valid_day_centers is not None else None,
	)

	return (
		prior_days_list,
		mlp_layers_list,
		lr_list,
		activation_list,
		final_activation_list,
		day_change_centered_list,
	)


__all__ = [
	"FEATURE_KEY_TO_ATTR",
	"set_feature_flags",
	"parse_feature_selections",
	"parse_sweep_lists",
]
