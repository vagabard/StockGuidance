from typing import Dict, List, Tuple
from trainer.label.transform.transformers import (
	identity,
	zero_centered,
	half_centered,
	gapped01,
	binary_labels,
	top_100_binary_labels,
	gappednegpos,
	binary_negpos,
	top_100_binary_negpos,
	normalized_labels,
	proportional_normalized_labels,
	normalized_zero_centered,
	proportional_normalized_zero_centered,
	normalized_half_centered,
	proportional_normalized_half_centered, proportional_normalized_zero_centered_gapped
)

def transform_labels(day_change_centered: str, stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
	if day_change_centered == "half":
		return half_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "zero":
		return zero_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "gapped01":
		return gapped01(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "binary_labels":
		return binary_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "top_100_binary_labels":
		return top_100_binary_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "gappednegpos":
		return gappednegpos(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "binary_negpos":
		return binary_negpos(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "top_100_binary_negpos":
		return top_100_binary_negpos(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "normalize":
		return normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "proportional":
		return proportional_normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "normalized_zero_centered":
		return normalized_zero_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "proportional_normalized_zero_centered":
		return proportional_normalized_zero_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "proportional_normalized_zero_centered_gapped":
		return proportional_normalized_zero_centered_gapped(stock_ids_and_features_and_label_by_date, min_value_found,max_value_found)
	elif day_change_centered == "normalized_half_centered":
		return normalized_half_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	elif day_change_centered == "proportional_normalized_half_centered":
		return proportional_normalized_half_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
	else:
		return identity(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
