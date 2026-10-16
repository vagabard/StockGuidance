
import torch
from training_instance_provider import TrainingInstanceProvider, TrainingDatasetTrivial
from trainer.datasets import IndexDataset
import numpy as np

def test_reuse():
    print("Testing TrainingInstanceProvider reuse...")
    # Create a trivial provider with torch support
    provider = TrainingDatasetTrivial(
        number_of_dates=100,
        number_of_tickers=10,
        number_of_features=5,
        use_torch=True,
        store_as_torch=True,
        cache_dataset=False,
        cache_format="msgspec" # Required by the implementation
    )
    provider.set_how_many_days_in_past_to_use(2)

    # Test internal reuse
    feat1, label1, date1 = provider.get_feature_and_label_pair(1)
    feat2, label2, date2 = provider.get_feature_and_label_pair(2)

    # Since we didn't pass out_feat, it should use the internal _reusable_feat_tensor
    # So feat1 and feat2 should be the same object
    print(f"Internal reuse feat: {feat1 is feat2}")
    print(f"Internal reuse label (should be False): {label1 is label2}")

    if feat1 is not feat2:
        print("FAIL: Internal feature tensor not reused")
    if label1 is label2:
        print("FAIL: Internal label tensor was reused")
    else:
        print("SUCCESS: Internal label tensor NOT reused (as requested)")

    # Test external reuse
    out_feat = torch.empty_like(feat1)

    feat3, label3, date3 = provider.get_feature_and_label_pair(3, out_feat=out_feat)

    print(f"External reuse feat: {feat3 is out_feat}")

    if feat3 is not out_feat:
        print("FAIL: External feature tensor not used")

    # Test IndexDataset iteration reuse
    indices = [1, 2, 3, 4, 5]
    dataset = IndexDataset(provider, indices)

    prev_x = None
    prev_y = None
    all_same_x = True
    all_same_y = True

    for x, y in dataset:
        if prev_x is not None:
            if x is not prev_x:
                all_same_x = False
            if y is not prev_y:
                all_same_y = False
        prev_x = x
        prev_y = y

    print(f"IndexDataset iteration reuse x: {all_same_x}")
    print(f"IndexDataset iteration reuse y (should be False): {all_same_y}")

    if not all_same_x:
        print("FAIL: IndexDataset did not reuse feature tensors during iteration")
    if all_same_y:
        print("FAIL: IndexDataset reused label tensors during iteration")
    else:
        print("SUCCESS: IndexDataset feature reused, label NOT reused during iteration")

    # Test IndexDataset __getitem__ reuse
    print("Testing IndexDataset __getitem__ reuse...")
    g1_x, g1_y = dataset[0]
    g2_x, g2_y = dataset[1]

    print(f"IndexDataset __getitem__ reuse x: {g1_x is g2_x}")
    print(f"IndexDataset __getitem__ reuse y (should be False): {g1_y is g2_y}")

    if g1_x is not g2_x:
        print("FAIL: IndexDataset did not reuse feature tensors in __getitem__")
    if g1_y is g2_y:
        print("FAIL: IndexDataset reused label tensors in __getitem__")
    else:
        print("SUCCESS: IndexDataset feature reused, label NOT reused in __getitem__")

if __name__ == "__main__":
    try:
        test_reuse()
    except Exception as e:
        print(f"Error during test: {e}")
        import traceback
        traceback.print_exc()
