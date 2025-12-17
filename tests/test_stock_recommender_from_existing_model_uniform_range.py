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


def test_existing_model_random_uniform_inputs_vary_top_id():
    """
    Load the pre-committed model in tests/test_model, generate several random
    feature vectors with values in [0.8, 1.1], and verify that the top-1 output
    id (argmax) is not the same across all trials. Also print the top ids seen.
    """
    _ = _torch_or_skip()
    from StockRecommenderFromModel import StockRecommenderFromModel

    # Locate the test model directory relative to this file
    this_dir = os.path.dirname(__file__)
    model_dir = os.path.join(this_dir, "test_model")

    # Instantiate recommender from directory (it contains *model_manifest.json)
    rec = StockRecommenderFromModel(model_dir, device="cpu")

    # Get required input size from the loaded manifest
    input_size = rec.get_input_size()

    # Random inputs in the requested range [0.5, 1.5]
    rng = np.random.default_rng(20231119)
    trials = 20
    top1_ids = []

    # Prepare to capture full outputs for the first 10 runs
    captured_scores = []  # list of np.ndarray, each shape (T,)
    output_size = None

    for i in range(trials):
        x = rng.uniform(0.5, 1.5, size=input_size).astype(np.float32)
        scores, top_ids = rec.getBuyRecommendations(x, top_k=1, return_scores=True)
        if output_size is None:
            output_size = int(scores.shape[0])
        tid = int(top_ids[0])
        print(tid)
        top1_ids.append(tid)

        # Save the first 10 full output vectors for CSV writing
        if i < 10:
            captured_scores.append(scores.astype(np.float64, copy=False))

    # If we captured any scores, write them to a CSV file as requested
    if captured_scores:
        this_dir = os.path.dirname(__file__)
        csv_path = os.path.join(this_dir, "test_model", "uniform_range_outputs.csv")
        try:
            with open(csv_path, "w", encoding="utf-8") as f:
                # First row: output indexes 0..T-1
                T = int(output_size or captured_scores[0].shape[0])
                header = [str(i) for i in range(T)]
                f.write(",".join(header) + "\n")

                # Next rows: scores from run 1..10
                for arr in captured_scores:
                    row = [
                        (f"{float(v):.6f}" if np.isfinite(v) else "")
                        for v in arr.tolist()
                    ]
                    f.write(",".join(row) + "\n")
            # Helpful for visibility in CI logs
            print(f"wrote CSV with model outputs: {csv_path}")
        except Exception as e:
            # Don't fail the test due to IO issues; surface as print for debugging
            print(f"failed to write outputs CSV: {e}")

    # Print the unique set of top ids observed for easier debugging/output review
    print("unique_top_ids:", sorted(set(top1_ids)))

    # Ensure there was variation in the argmax results
    assert len(set(top1_ids)) >= 2, (
        f"Expected variation in top-1 ids across random inputs in [0.8,1.1], "
        f"but observed only one id: {top1_ids}"
    )
