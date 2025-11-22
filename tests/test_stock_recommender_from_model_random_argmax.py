import json
import os

import numpy as np
import pytest


def _torch_or_skip():
    try:
        import torch  # type: ignore
    except Exception:  # pragma: no cover
        pytest.skip("PyTorch not available in test environment", allow_module_level=True)
        return None
    return torch


def test_stock_recommender_random_inputs_vary_argmax(tmp_path):
    """
    Create a tiny MLP model and manifest in a temp directory, load it with
    StockRecommenderFromModel, then feed several random feature vectors of the
    correct size and assert that the highest-scoring output index is not the same
    for all trials.
    """
    torch = _torch_or_skip()
    import models  # local module
    from StockRecommenderFromModel import StockRecommenderFromModel

    # Deterministic setup for reproducibility of this test
    torch.manual_seed(42)
    np.random.seed(123)

    # Define a small model configuration
    input_size = 16
    output_size = 7
    hidden_layers = 32
    mlp_layers = 3
    activation = "relu"
    arch = "mlp"

    # Build the model and save its randomly initialized weights
    model = models.make_model(
        arch=arch,
        input_size=input_size,
        output_size=output_size,
        hidden_layers=hidden_layers,
        activation=activation,
        num_layers=mlp_layers,
    )
    model.eval()

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    weights_path = run_dir / "model_state.pt"
    torch.save(model.state_dict(), str(weights_path))

    # Write a manifest compatible with models.load_model_from_manifest
    manifest = {
        "arch": arch,
        "input_size": input_size,
        "output_size": output_size,
        "hidden_layers": hidden_layers,
        "mlp_layers": mlp_layers,
        "activation": activation,
        "state_dict_path": os.path.basename(str(weights_path)),
        # Optional metadata used by StockRecommenderFromModel helpers
        "prior_days": 0,
    }
    manifest_path = run_dir / "model_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    # Instantiate the recommender pointing at the run directory
    rec = StockRecommenderFromModel(str(run_dir), device="cpu")
    assert rec.get_input_size() == input_size

    # Generate several random inputs and record the top-1 output id each time
    top1_ids = []
    trials = 12
    for _ in range(trials):
        x = np.random.randn(input_size).astype(np.float32)
        scores, top_ids = rec.getBuyRecommendations(x, top_k=1, return_scores=True)
        #print(top_ids[0])
        assert scores.shape == (output_size,)
        assert len(top_ids) == 1
        top1_ids.append(int(top_ids[0]))

    # Verify that the highest output node is not identical across all trials
    # Allowing index_base=1 mapping; we only care about variation
    assert len(set(top1_ids)) >= 2, (
        "Top-1 output index was identical across all random inputs; expected variation"
    )
