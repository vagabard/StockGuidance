import json
import os
import numpy as np
import pytest
from feature_vector_builder import FeatureVectorBuilder

def _torch_or_skip():
    try:
        import torch
    except Exception:
        pytest.skip("PyTorch not available in test environment", allow_module_level=True)
        return None
    return torch

def test_feature_vector_builder_from_db(tmp_path, monkeypatch):
    """
    Verify that FeatureVectorBuilder.from_db correctly calls fetch_grouped_by_date
    and initializes the builder with correct parameters.
    """
    torch = _torch_or_skip()
    from StockRecommenderFromModel import StockRecommenderFromModel
    import feature_vector_builder as fvb_mod

    # 1. Setup Mock Recommender
    class MockRec:
        def __init__(self):
            self.ticker_ids = [1, 2, 3]
            self.input_size = 6
        def get_required_previous_days(self): return 1
        def get_day_change_centered(self): return "zero"
        def get_current_day_change_used(self): return True
        def get_volume_ratio_used(self): return False
        def get_vwap_ratio_used(self): return False
        def get_tranaction_ratio_used(self): return False
        def get_num_features_per_ticker(self): return 1

    rec = MockRec()

    # 2. Mock fetch_grouped_by_date
    def mock_fetch_grouped_by_date(**kwargs):
        # Return dummy data
        dates = np.array([100, 101])
        # 2 dates, 3 tickers, 1 feature -> (2, 3)
        X_flat = np.array([
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0]
        ])
        _Y = None
        db_ticker_ids = [1, 2, 3]
        return dates, X_flat, _Y, db_ticker_ids

    monkeypatch.setattr(fvb_mod, "fetch_grouped_by_date", mock_fetch_grouped_by_date)

    # 3. Call from_db
    builder = FeatureVectorBuilder.from_db(rec, dbinfo_file="dummy.id")

    # 4. Assertions
    assert isinstance(builder, FeatureVectorBuilder)
    assert builder.num_days == 2
    assert builder.num_features == 1
    assert list(builder.model_ticker_ids) == [1, 2, 3]
    np.testing.assert_array_equal(builder.dates, [100, 101])

    # Test build_for_date
    feat_vec, date_id = builder.build_for_date()
    assert date_id == 101
    # expected: [current(101), prior(100)] -> [4.0, 5.0, 6.0, 1.0, 2.0, 3.0]
    np.testing.assert_array_equal(feat_vec, [4.0, 5.0, 6.0, 1.0, 2.0, 3.0])

if __name__ == "__main__":
    pytest.main([__file__])
