import numpy as np
import pytest
from feature_vector_builder import FeatureVectorBuilder

def test_feature_vector_builder_basic():
    # Setup dummy data
    # 3 dates, 2 tickers, 2 features
    X_flat = np.array([
        [1.0, 1.1, 2.0, 2.1], # date 100
        [3.0, 3.1, 4.0, 4.1], # date 101
        [5.0, 5.1, 6.0, 6.1]  # date 102
    ])
    dates = np.array([100, 101, 102])
    db_ticker_ids = [1, 2]
    model_ticker_ids = [1, 2]
    num_features = 2
    num_days = 1 # Just current day

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    # Test build for latest date
    feat_vec, actual_date_id = builder.build_for_date()
    assert actual_date_id == 102
    np.testing.assert_array_equal(feat_vec, [5.0, 5.1, 6.0, 6.1])

    # Test build for specific date
    feat_vec, actual_date_id = builder.build_for_date(date_id=101)
    assert actual_date_id == 101
    np.testing.assert_array_equal(feat_vec, [3.0, 3.1, 4.0, 4.1])

def test_feature_vector_builder_with_priors():
    # 3 dates, 2 tickers, 1 feature
    X_flat = np.array([
        [1.0, 2.0], # date 100
        [3.0, 4.0], # date 101
        [5.0, 6.0]  # date 102
    ])
    dates = np.array([100, 101, 102])
    db_ticker_ids = [1, 2]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 2 # Current + 1 prior

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    # Test build for latest date (102). Should include 102 and 101.
    feat_vec, actual_date_id = builder.build_for_date()
    assert actual_date_id == 102
    # Layout: current then priors -> [X(102), X(101)] -> [5.0, 6.0, 3.0, 4.0]
    np.testing.assert_array_equal(feat_vec, [5.0, 6.0, 3.0, 4.0])

def test_feature_vector_builder_realign():
    # DB has tickers [2, 1], model expects [1, 2]
    X_flat = np.array([
        [2.0, 1.0], # date 100: Ticker 2 feature=2.0, Ticker 1 feature=1.0
        [4.0, 3.0]  # date 101: Ticker 2 feature=4.0, Ticker 1 feature=3.0
    ])
    dates = np.array([100, 101])
    db_ticker_ids = [2, 1]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 1

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    # Aligned X_flat should have Ticker 1 then Ticker 2
    feat_vec, actual_date_id = builder.build_for_date(date_id=101)
    # Expected: [3.0, 4.0]
    np.testing.assert_array_equal(feat_vec, [3.0, 4.0])

def test_feature_vector_builder_missing_ticker():
    # DB has only Ticker 2, model expects [1, 2]
    X_flat = np.array([
        [2.0],
        [4.0]
    ])
    dates = np.array([100, 101])
    db_ticker_ids = [2]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 1

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    feat_vec, actual_date_id = builder.build_for_date(date_id=101)
    # Expected: [NaN, 4.0]
    assert np.isnan(feat_vec[0])
    assert feat_vec[1] == 4.0

def test_feature_vector_builder_too_many_tickers():
    # DB has [1, 2, 3], model expects [1, 2]
    X_flat = np.array([
        [1.0, 2.0, 3.0], # date 100
        [4.0, 5.0, 6.0]  # date 101
    ])
    dates = np.array([100, 101])
    db_ticker_ids = [1, 2, 3]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 1

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    feat_vec, actual_date_id = builder.build_for_date(date_id=101)
    # Expected: [4.0, 5.0] (Ticker 3 is filtered out)
    np.testing.assert_array_equal(feat_vec, [4.0, 5.0])

def test_feature_vector_builder_mixed_tickers():
    # DB has [2, 3], model expects [1, 2]
    # Ticker 1: missing -> NaN
    # Ticker 2: present
    # Ticker 3: extra -> filtered out
    X_flat = np.array([
        [2.0, 3.0], # date 100
        [5.0, 6.0]  # date 101
    ])
    dates = np.array([100, 101])
    db_ticker_ids = [2, 3]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 1

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    feat_vec, actual_date_id = builder.build_for_date(date_id=101)
    # Expected: [NaN, 5.0]
    assert np.isnan(feat_vec[0])
    assert feat_vec[1] == 5.0

def test_feature_vector_builder_intermittent_ticker():
    # DB has tickers [1, 2].
    # On some days, Ticker 1 might have NaN if it was missing in raw data (simulated here)
    # X_flat layout: [T1, T2]
    X_flat = np.array([
        [np.nan, 10.0], # date 100 (Day 1) - T1 missing
        [1.1,    11.0], # date 101 (Day 2) - T1 present
        [np.nan, 12.0], # date 102 (Day 3) - T1 missing
        [1.3,    13.0]  # date 103 (Day 4) - T1 present
    ])
    dates = np.array([100, 101, 102, 103])
    db_ticker_ids = [1, 2]
    model_ticker_ids = [1, 2]
    num_features = 1
    num_days = 4 # Current + 3 priors

    builder = FeatureVectorBuilder(X_flat, dates, db_ticker_ids, model_ticker_ids, num_features, num_days)

    feat_vec, actual_date_id = builder.build_for_date(date_id=103)
    # Layout: current (103) then priors (102, 101, 100)
    # T1: 1.3 (103), nan (102), 1.1 (101), nan (100)
    # T2: 13.0 (103), 12.0 (102), 11.0 (101), 10.0 (100)
    # Feature vector: [T1_103, T2_103, T1_102, T2_102, T1_101, T2_101, T1_100, T2_100]
    expected = [1.3, 13.0, np.nan, 12.0, 1.1, 11.0, np.nan, 10.0]
    np.testing.assert_array_equal(feat_vec, expected)

if __name__ == "__main__":
    pytest.main([__file__])
