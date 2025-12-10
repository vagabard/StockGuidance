import math
import unittest


# Import target functions from the training module
from train_pytorch_network import (
	ranking_max_score,
	ranking_score,
	ranking_surrogate_loss,
)


try:
	import torch  # type: ignore
except Exception:
	torch = None  # type: ignore


class TestRankingMetrics(unittest.TestCase):
	def test_ranking_max_score_small_N(self):
		# N == 0
		self.assertEqual(ranking_max_score(0), 0.0)

		# N == 1: only the first item, denom = 1
		self.assertAlmostEqual(ranking_max_score(1), 1.0, places=12)

		# N == 2: 2 + 1/log2(2) = 2 + 1
		self.assertAlmostEqual(ranking_max_score(2), 3.0, places=12)

		# N == 3: 3 + 2/log2(2) + 1/log2(3)
		expected_n3 = 3.0 + 2.0 / math.log2(2.0) + 1.0 / math.log2(3.0)
		self.assertAlmostEqual(ranking_max_score(3), expected_n3, places=12)

	def test_ranking_score_perfect_vs_reverse(self):
		truth = ["A", "B", "C", "D"]
		perfect = ["A", "B", "C", "D"]
		reverse = ["D", "C", "B", "A"]

		s_perfect = ranking_score(perfect, truth)
		s_max = ranking_max_score(len(truth))
		s_reverse = ranking_score(reverse, truth)

		print(s_perfect, s_max, s_reverse)
		# Perfect ordering should achieve maximum
		self.assertAlmostEqual(s_perfect, s_max, places=12)

		# Reverse ordering should be worse than maximum but still positive
		self.assertGreater(s_reverse, 0.0)
		self.assertLess(s_reverse, s_max)

	def test_numeric_values_higher_value_higher_rank(self):
		# In numeric lists, higher value should correspond to higher rank.
		# For (0.1, 0.2, 0.3), the proper ranking from first to last is (0.3, 0.2, 0.1).
		truth = [0.3, 0.2, 0.1]  # ideal order: highest first
		pred_best = [0.3, 0.2, 0.1]  # matches the ideal
		pred_worst = [0.1, 0.2, 0.3]  # lowest first (ascending)

		s_best = ranking_score(pred_best, truth)
		s_max = ranking_max_score(len(truth))
		s_worst = ranking_score(pred_worst, truth)

		print("test numeric values high",s_best, s_max, s_worst)

		# Perfect descending should achieve the max score
		self.assertAlmostEqual(s_best, s_max, places=12)
		# Ascending order should be strictly worse
		self.assertLess(s_worst, s_best)

	def test_ranking_score_errors(self):
		# Mismatched lengths
		with self.assertRaises(ValueError):
			ranking_score([1, 2, 3], [1, 2])

		# Predicted contains item not in truth
		with self.assertRaises(ValueError):
			ranking_score(["x", "y"], ["x", "z"])

	@unittest.skipIf(torch is None, "torch_interface not available")
	def test_ranking_surrogate_loss_zero_when_equal(self):
		assert torch is not None
		# Two samples, four items each
		y_true = torch.tensor([
			[0.1, 0.3, 0.2, 0.4],
			[1.0, 0.0, -0.5, 0.5],
		], dtype=torch.float32)
		y_pred = y_true.clone()

		loss = ranking_surrogate_loss(y_pred, y_true, temperature=1.0)
		self.assertLess(float(loss.item()), 0.000001, "loss should be zero when predictions are identical")

	@unittest.skipIf(torch is None, "torch_interface not available")
	def test_ranking_surrogate_loss_increases_when_permuted(self):
		assert torch is not None
		# One sample, deterministic labels
		y_true = torch.tensor([[0.0, 1.0, 2.0, 3.0]], dtype=torch.float32)
		# Prediction with reversed order (worst case relative to truth order)
		y_pred_bad = torch.tensor([[3.0, 2.0, 1.0, 0.0]], dtype=torch.float32)

		# Perfectly matching prediction
		y_pred_good = y_true.clone()

		loss_good = ranking_surrogate_loss(y_pred_good, y_true, temperature=1.0)
		loss_bad = ranking_surrogate_loss(y_pred_bad, y_true, temperature=1.0)
		print("basic surrogate loss ",loss_good, loss_bad)

		loss_good_temp = ranking_surrogate_loss(y_pred_good, y_true, temperature=0.6)
		loss_bad_temp = ranking_surrogate_loss(y_pred_bad, y_true, temperature=0.6)
		print("basic surrogate loss temp ",loss_good_temp, loss_bad_temp)

		self.assertLess(float(loss_good.item()), 0.000001, "loss should be zero when predictions are identical")
		self.assertGreater(float(loss_bad.item()), float(loss_good.item()) + 1e-4)

	@unittest.skipIf(torch is None, "torch_interface not available")
	def test_ranking_surrogate_loss_batchmean_reduction_stability(self):
		assert torch is not None
		# Single sample
		y_true_single = torch.tensor([[0.2, -0.1, 0.5]], dtype=torch.float32)
		y_pred_single = y_true_single + torch.tensor([[0.01, -0.02, 0.03]], dtype=torch.float32)

		# Duplicate into a batch of size 2
		y_true_batch = torch.cat([y_true_single, y_true_single], dim=0)
		y_pred_batch = torch.cat([y_pred_single, y_pred_single], dim=0)

		loss_single = ranking_surrogate_loss(y_pred_single, y_true_single, temperature=0.7)
		loss_batch = ranking_surrogate_loss(y_pred_batch, y_true_batch, temperature=0.7)
		print("batchmean surrogate loss ",loss_batch, loss_single)

		# With reduction='batchmean', duplicating identical samples should keep the loss approximately the same
		self.assertAlmostEqual(float(loss_batch.item()), float(loss_single.item()), places=6)

	@unittest.skipIf(torch is None, "torch_interface not available")
	def test_surrogate_emphasizes_top_ranks(self):
		"""Verify that mistakes involving the very top item are penalized more.

		We construct a label vector with strictly decreasing values so that
		the highest value should be ranked first, second-highest second, etc.
		We then compare two prediction permutations where only one pair is swapped:
		- Swap top (rank 1) with bottom (rank 5)
		- Swap second (rank 2) with bottom (rank 5)
		The former should incur a larger KL loss.
		"""
		assert torch is not None
		# True labels define desired ranking: idx 0 > 1 > 2 > 3 > 4
		y_true = torch.tensor([[5.0, 4.0, 3.0, 2.0, 1.0]], dtype=torch.float32)
		# Perfect prediction (for reference, should be near-zero loss)
		y_pred_perfect = y_true.clone()

		# Helper to swap two indices in a prediction copy
		def swap_pred(i, j):
			v = y_true.clone()
			v[0, i], v[0, j] = v[0, j].clone(), v[0, i].clone()
			return v

		# Case A: Swap top (idx 0) with bottom (idx 4)
		y_pred_swap_top_bottom = swap_pred(0, 4)
		# Case B: Swap second (idx 1) with bottom (idx 4)
		y_pred_swap_second_bottom = swap_pred(1, 4)

		T = 0.99  # slightly sharp to emphasize top mass
		loss_perfect = ranking_surrogate_loss(y_pred_perfect, y_true, temperature=T)
		loss_swap_top_bottom = ranking_surrogate_loss(y_pred_swap_top_bottom, y_true, temperature=T)
		loss_swap_second_bottom = ranking_surrogate_loss(y_pred_swap_second_bottom, y_true, temperature=T)

		print("surrogate emphasize top:", loss_perfect.item(), loss_swap_top_bottom.item(), loss_swap_second_bottom.item())

		# Perfect should be essentially zero
		self.assertLess(float(loss_perfect.item()), 1e-6)
		# Swapping the very top with the bottom should be worse than swapping the second with the bottom
		self.assertGreater(float(loss_swap_top_bottom.item()), float(loss_swap_second_bottom.item()) + 1e-6)


if __name__ == "__main__":
	unittest.main()
