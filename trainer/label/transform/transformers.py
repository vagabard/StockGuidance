from typing import List, Tuple, Dict

import numpy
import numpy as np

def identity(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Identity transformation (next_day_change_in_price)."""
    return stock_ids_and_features_and_label_by_date

def zero_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """SET zero_centered_next_day_price_change = next_day_change_in_price - 1"""
    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        transformed[date_id] = [(ticker_id, features, label - 1.0 if label is not None and not np.isnan(label) else label)
                                for ticker_id, features, label in rows]
    return transformed

def half_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Similar to zero_centered but maybe with different shift?
    The DB code didn't show half_centered but load_database_rows_into_tensors.py had it.
    Looking at convert_target_label_to_database_column:
    - "half" -> "half_centered_next_day_price_change"
    If I don't see it in the DB population script, maybe it's next_day_change_in_price - 0.5?
    Wait, let me check if there are other files in database/
    """
    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        transformed[date_id] = [(ticker_id, features, label - 0.5 if label is not None and not np.isnan(label) else label)
                                for ticker_id, features, label in rows]
    return transformed

def gapped01(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """
    CASE
        WHEN next_day_change_in_price >= 1 THEN 0.5 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
        ELSE 0.0 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
    END
    """
    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        new_rows = []
        for ticker_id, features, label in rows:
            if label is None or np.isnan(label):
                new_label = label
            else:
                if label >= 1:
                    new_label = 0.5 + label / (2 * (label + 1))
                else:
                    new_label = 0.0 + label / (2 * (label + 1))
            new_rows.append((ticker_id, features, new_label))
        transformed[date_id] = new_rows
    return transformed

def binary_labels(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """CASE WHEN next_day_change_in_price >= 1 THEN 1 ELSE 0 END"""
    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        transformed[date_id] = [(ticker_id, features, 1.0 if label >= 1.0 else 0.0)
                                if label is not None and not np.isnan(label) else (ticker_id, features, label)
                                for ticker_id, features, label in rows]
    return transformed

def top_100_binary_labels(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """IF(r.rn <= 100, 1, 0) where rn is rank by next_day_change_in_price DESC"""
    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        # Sort rows by label descending
        valid_rows = [r for r in rows if r[2] is not None and not np.isnan(r[2])]
        valid_rows.sort(key=lambda x: x[2], reverse=True)
        top_100_tickers = {r[0] for r in valid_rows[:100]}

        new_rows = []
        for ticker_id, features, label in rows:
            if label is None or np.isnan(label):
                new_label = label
            else:
                new_label = 1.0 if ticker_id in top_100_tickers else 0.0
            new_rows.append((ticker_id, features, new_label))
        transformed[date_id] = new_rows
    return transformed

def gappednegpos(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """next_day_price_change_to_extremes_0_1 * 2 - 1"""
    transformed_01 = gapped01(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in transformed_01.items():
        transformed[date_id] = [(ticker_id, features, label * 2 - 1 if label is not None and not np.isnan(label) else label)
                                for ticker_id, features, label in rows]
    return transformed

def binary_negpos(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """next_day_raise1_lower0 * 2 - 1"""
    transformed_01 = binary_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in transformed_01.items():
        transformed[date_id] = [(ticker_id, features, label * 2 - 1 if label is not None and not np.isnan(label) else label)
                                for ticker_id, features, label in rows]
    return transformed

def top_100_binary_negpos(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """next_day_top100_raise1_lower0 * 2 - 1"""
    transformed_01 = top_100_binary_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in transformed_01.items():
        transformed[date_id] = [(ticker_id, features, label * 2 - 1 if label is not None and not np.isnan(label) else label)
                                for ticker_id, features, label in rows]
    return transformed

def normalized_labels(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """
    Normalize all stock labels such that 1.0 maps to 0.5,
    max_value_found maps to 1.0, and min_value_found maps to 0.0.
    """
    transformed = {}
    pos_range = max_value_found - 1.0
    neg_range = 1.0 - min_value_found

    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        new_rows = []
        for ticker_id, features, label in rows:
            if label is None or np.isnan(label):
                new_label = label
            elif label >= 1.0:
                new_label = 0.5 + 0.5 * (label - 1.0) / pos_range if pos_range > 0 else 0.5
            else:
                new_label = 0.5 - 0.5 * (1.0 - label) / neg_range if neg_range > 0 else 0.5
            new_rows.append((ticker_id, features, new_label))
        transformed[date_id] = new_rows
    return transformed

def proportional_normalized_labels(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """
    Find whether the absolute value of the difference between min and 1 or max and 1 is greater.
    Then use that value for proportional normalization.
    That means if min is .4 and max is 1.1, then the normalization will put 1 at .5 and .4 goes to 0.
    1.4 would go to 1, but since 1.1 is the highest then the largest value would not be 1, it would be less than 1.
    """
    max_diff = max(abs(min_value_found - 1.0), abs(max_value_found - 1.0))
    if max_diff == 0:
        # Should only happen if all labels are 1.0
        return stock_ids_and_features_and_label_by_date

    transformed = {}
    for date_id, rows in stock_ids_and_features_and_label_by_date.items():
        transformed[date_id] = [
            (ticker_id, features, 0.5 + (label - 1.0) / (2 * max_diff) if label is not None and not np.isnan(label) else label)
            for ticker_id, features, label in rows
        ]
    return transformed

def normalized_zero_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Calls normalize_labels, then transforms to [-1, 1]."""
    norm_data = normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in norm_data.items():
        transformed[date_id] = [(tid, feat, label * 2.0 - 1.0 if label is not None and not np.isnan(label) else label)
                                for tid, feat, label in rows]
    return transformed

def proportional_normalized_zero_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Calls proportional_normalize_labels, then transforms to [-1, 1]."""
    norm_data = proportional_normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in norm_data.items():
        transformed[date_id] = [(tid, feat, label * 2.0 - 1.0 if label is not None and not np.isnan(label) else label)
                                for tid, feat, label in rows]
    return transformed

def proportional_normalized_zero_centered_gapped(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Calls proportional_normalize_labels, then transforms to [-1, 1]."""
    prop_norm_zero = proportional_normalized_zero_centered(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)
    transformed = {}
    for date_id, rows in prop_norm_zero.items():
        transformed[date_id] = [(tid, feat, label *0.5 + 0.5*numpy.sign(label) if label is not None and not np.isnan(label) else label)
                                for tid, feat, label in rows]
    return transformed

def normalized_half_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Just calls normalize_labels."""
    return normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)

def proportional_normalized_half_centered(stock_ids_and_features_and_label_by_date: Dict[int, List[Tuple[int, List[float], float]]], min_value_found: float = 0.0, max_value_found: float = 0.0) -> Dict[int, List[Tuple[int, List[float], float]]]:
    """Just calls proportional_normalize_labels."""
    return proportional_normalized_labels(stock_ids_and_features_and_label_by_date, min_value_found, max_value_found)

