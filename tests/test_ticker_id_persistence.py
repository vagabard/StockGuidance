import json
import os
import numpy as np
import pytest

def _torch_or_skip():
    try:
        import torch
    except Exception:
        pytest.skip("PyTorch not available in test environment", allow_module_level=True)
        return None
    return torch

def test_stock_recommender_uses_ticker_ids_mapping(tmp_path):
    """
    Verify that StockRecommenderFromModel correctly uses ticker_ids from the manifest
    to map output indices to ticker IDs.
    """
    torch = _torch_or_skip()
    import models
    from StockRecommenderFromModel import StockRecommenderFromModel

    # Define a small model configuration
    input_size = 4
    output_size = 3
    ticker_ids = [101, 202, 303]  # Custom mapping

    model = models.make_model(
        arch="mlp",
        input_size=input_size,
        output_size=output_size,
        hidden_layers=[8],
        num_layers=2,
    )
    model.eval()

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    weights_path = run_dir / "model_state.pt"
    torch.save(model.state_dict(), str(weights_path))

    manifest = {
        "arch": "mlp",
        "input_size": input_size,
        "output_size": output_size,
        "hidden_layers": [8],
        "mlp_layers": 2,
        "state_dict_path": os.path.basename(str(weights_path)),
        "ticker_ids": ticker_ids,
        "prior_days": 0,
    }
    manifest_path = run_dir / "model_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    rec = StockRecommenderFromModel(str(run_dir), device="cpu")

    # Mock transactions for these ticker_ids
    dummy_transactions = np.ones(output_size, dtype=np.float32) * 1000

    # Case 1: Highest score at index 0 -> should map to ticker_id 101
    x = np.random.randn(input_size).astype(np.float32)
    # We don't control the weights easily here, so let's just check if returned IDs are from our list
    scores, top_ids = rec.getBuyRecommendations(x, top_k=3, transactions_per_ticker=dummy_transactions)

    for tid in top_ids:
        assert tid in ticker_ids

    assert len(top_ids) == 3
    assert set(top_ids) == set(ticker_ids)

def test_stock_recommender_db_fetch_uses_ticker_ids(tmp_path, monkeypatch):
    """
    Verify that when fetching transactions from DB, it uses ticker_ids mapping.
    """
    torch = _torch_or_skip()
    import models
    from StockRecommenderFromModel import StockRecommenderFromModel
    import StockRecommenderFromModel as rec_mod

    input_size = 4
    output_size = 2
    ticker_ids = [500, 600]

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "arch": "mlp",
        "input_size": input_size,
        "output_size": output_size,
        "ticker_ids": ticker_ids,
        "prior_days": 0,
        "state_dict_path": "model_state.pt",
    }
    manifest_path = run_dir / "model_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    # Create a dummy weights file
    weights_path = run_dir / "model_state.pt"
    torch.save({}, str(weights_path))

    # Mock model loading to avoid needing weights
    class MockModel:
        def __init__(self):
            self.input_size = input_size
            self.output_size = output_size
        def eval(self): return self
        def to(self, device): return self
        def __call__(self, x):
            import torch
            # return scores where index 1 (ticker 600) is higher
            res = torch.zeros((1, output_size))
            res[0, 1] = 1.0
            return res

    mock_model_instance = MockModel()
    monkeypatch.setattr(rec_mod, "load_model_from_manifest", lambda *args, **kwargs: (mock_model_instance, manifest, {}))

    # Mock get_connection
    class MockCursor:
        def execute(self, sql, params): pass
        def fetchall(self):
            # Return transactions for ticker 600, but NOT for 500
            return [(600, 1000)]

    class MockConn:
        def cursor(self): return MockCursor()
        def close(self): pass

    monkeypatch.setattr(rec_mod, "get_connection", lambda **kwargs: MockConn())

    rec = StockRecommenderFromModel(str(run_dir), device="cpu")

    x = np.zeros(input_size, dtype=np.float32)
    # Ticker 600 should be recommended because it has enough transactions.
    # Ticker 500 has 0 transactions (not in DB), so it should be filtered out.
    scores, top_ids = rec.getBuyRecommendations(x, top_k=2, current_date_id=123, dbinfo_file="dummy.id")

    assert 600 in top_ids
    assert 500 not in top_ids
    assert len(top_ids) == 1
