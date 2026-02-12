from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
import torch

from simulate_trading import simulate

class MockProvider:
    def __init__(self, dates):
        self.cumulative_dates_for_train_validation_and_test_sets = np.array(dates)

class MockDataset:
    def __init__(self, provider, data_generator):
        self.provider = provider
        self.data_generator = data_generator
        self.items = list(data_generator)

    def __len__(self):
        return len(self.items)

    def get_next_feature_label_date(self):
        for item in self.items:
            yield item

@pytest.fixture
def mock_recommender():
    recommender = MagicMock()
    recommender.get_required_previous_days.return_value = 1
    recommender.get_output_directory.return_value = "mock_output"
    recommender.get_model_number.return_value = 1
    # getBuyRecommendations returns (scores, top_ids)
    recommender.getBuyRecommendations.return_value = (np.array([0.9, 0.1]), [10, 20])
    return recommender

def test_simulate_basic_flow(mock_recommender, tmp_path):
    # Setup mock data
    dates = [100, 101, 102]
    provider = MockProvider(dates)

    # feature for date 100, trade on date 101
    # feature for date 101, trade on date 102
    data = [
        (torch.tensor([1.0]), torch.tensor([0.0]), 100),
        (torch.tensor([2.0]), torch.tensor([0.0]), 101),
    ]
    dataset = MockDataset(provider, data)

    # Mock prices for trade dates 101 and 102
    # ticker 10: 101 (open 10, close 11 -> ratio 1.1)
    # ticker 10: 102 (open 11, close 12.1 -> ratio 1.1)
    price_data = {
        (101, 10): (10.0, 11.0),
        (101, 20): (20.0, 20.0),
        (102, 10): (11.0, 12.1),
        (102, 20): (20.0, 22.0),
    }

    def mock_fetch_prices(dbinfo_file, date_id, ticker_ids):
        res = {}
        for tid in ticker_ids:
            if (date_id, tid) in price_data:
                res[tid] = price_data[(date_id, tid)]
        return res

    mock_recommender.get_output_directory.return_value = str(tmp_path)
    mock_recommender.ticker_ids = [10, 20, 30, 40]

    with patch("simulate_trading.fetch_open_close_for_date_tickers", side_effect=mock_fetch_prices), \
         patch("simulate_trading.get_ticker_positions") as mock_pos:

        # Mock get_ticker_positions to return some values
        mock_pos.return_value = {10: 0.8, 20: 0.6}

        out_path = simulate(
            [mock_recommender],
            top_k=[1],
            dates_to_handle=dataset,
            dbinfo_file="fake_db",
        )

    assert os.path.exists(out_path)

    # Verify portfolio values
    # Day 1: trade date 101. Top 1 is ticker 10. Ratio 1.1.
    # Portfolio: 1000 * 1.1 = 1100.
    # Day 2: trade date 102. Top 1 is ticker 10. Ratio 1.1.
    # Portfolio: 1100 * 1.1 = 1210.

    with open(out_path, "r") as f:
        lines = f.readlines()
        # header, day 1, day 2
        assert len(lines) == 3
        # Check day 2 portfolio value
        last_row = lines[-1].strip().split(",")
        # feature_date_id, open_date_id, close_date_id, topNRecommends, Return Percent top1, Rank Percent top1, Portfolio return top1
        assert last_row[0] == "101"
        assert last_row[1] == "102"
        assert last_row[6] == "1210.000000"

def test_simulate_missing_price_data(mock_recommender, tmp_path):
    dates = [100, 101]
    provider = MockProvider(dates)
    data = [(torch.tensor([1.0]), torch.tensor([0.0]), 100)]
    dataset = MockDataset(provider, data)

    # Return empty price map
    mock_recommender.get_output_directory.return_value = str(tmp_path)
    mock_recommender.ticker_ids = None

    with patch("simulate_trading.fetch_open_close_for_date_tickers", return_value={}), \
         patch("simulate_trading.get_ticker_positions", return_value={}):
        out_path = simulate(
            [mock_recommender],
            top_k=[1],
            dates_to_handle=dataset,
            dbinfo_file="fake_db",
        )

    with open(out_path, "r") as f:
        lines = f.readlines()
        last_row = lines[-1].strip().split(",")
        # Ratio should be 1.0 (no change) if no valid ratios found
        assert last_row[4] == "0.000000" # Return Percent
        assert last_row[5] == "0.000000" # Rank Percent
        assert last_row[6] == "1000.000000" # Portfolio

def test_simulate_invalid_open_price(mock_recommender, tmp_path):
    dates = [100, 101]
    provider = MockProvider(dates)
    data = [(torch.tensor([1.0]), torch.tensor([0.0]), 100)]
    dataset = MockDataset(provider, data)

    # ticker 10: open 0 (invalid), ticker 20: open 10, close 11 (ratio 1.1)
    # mock_recommender returns [10, 20]
    mock_recommender.getBuyRecommendations.return_value = (np.array([0.9, 0.8]), [10, 20])
    price_data = {
        10: (0.0, 11.0),
        20: (10.0, 11.0),
    }

    mock_recommender.get_output_directory.return_value = str(tmp_path)
    mock_recommender.ticker_ids = None

    with patch("simulate_trading.fetch_open_close_for_date_tickers", return_value=price_data), \
         patch("simulate_trading.get_ticker_positions", return_value={20: 0.7}):
        # top_k=[2] means it will average ratios of 10 and 20
        # 10 is invalid, so only 20 is used. Average ratio = 1.1
        out_path = simulate(
            [mock_recommender],
            top_k=[2],
            dates_to_handle=dataset,
            dbinfo_file="fake_db",
        )

    with open(out_path, "r") as f:
        lines = f.readlines()
        last_row = lines[-1].strip().split(",")
        assert last_row[4] == "0.100000" # Return Percent
        # Rank Percent is at index 5
        assert last_row[6] == "1100.000000" # Portfolio

if __name__ == "__main__":
    pytest.main([__file__])
