import torch
import unittest
from stockguidance.models.sparse import sparse_linear_type

SparseLinear = sparse_linear_type()

class TestSparseLinear(unittest.TestCase):
    def test_connection_pattern(self):
        input_size = 20
        output_size = 10
        layer = SparseLinear(input_size, output_size, bias=False)

        # We verify that create_indices matches our expectations.
        # The logic in create_indices uses a 'mod_val' that increases.
        # mod_val=1: output index 0
        # k=1, mod_val=1, remainder=1%1=0. i%1==0 for all i in range(20).
        # So output 0 should have ALL inputs connected.
        for i in range(input_size):
            # Check if (0, i) is in indices
            is_connected = ((layer.indices[0] == 0) & (layer.indices[1] == i)).any()
            self.assertTrue(is_connected, f"Output 0 should be connected to input {i}")

        # mod_val=2: output indices 1, 2
        # k=2, mod_val=2, remainder=2%2=0. i%2==0 for i in [0, 2, 4, ..., 18]
        # k=3, mod_val=2, remainder=3%2=1. i%2==1 for i in [1, 3, 5, ..., 19]
        for i in range(input_size):
            is_connected_1 = ((layer.indices[0] == 1) & (layer.indices[1] == i)).any()
            is_connected_2 = ((layer.indices[0] == 2) & (layer.indices[1] == i)).any()
            if i % 2 == 0:
                self.assertTrue(is_connected_1)
                self.assertFalse(is_connected_2)
            else:
                self.assertFalse(is_connected_1)
                self.assertTrue(is_connected_2)

        # mod_val=3: output indices 3, 4, 5
        # k=4, mod_val=3, remainder=4%3=1. i%3==1
        # k=5, mod_val=3, remainder=5%3=2. i%3==2
        # k=6, mod_val=3, remainder=6%3=0. i%3==0
        for i in range(input_size):
            is_connected_3 = ((layer.indices[0] == 3) & (layer.indices[1] == i)).any()
            is_connected_4 = ((layer.indices[0] == 4) & (layer.indices[1] == i)).any()
            is_connected_5 = ((layer.indices[0] == 5) & (layer.indices[1] == i)).any()
            self.assertEqual(is_connected_3, True if i % 3 == 1 else False)
            self.assertEqual(is_connected_4, True if i % 3 == 2 else False)
            self.assertEqual(is_connected_5, True if i % 3 == 0 else False)

    def test_forward_pass(self):
        input_size = 10
        output_size = 5
        layer = SparseLinear(input_size, output_size, bias=True)

        x = torch.randn(2, input_size)
        out = layer(x)

        self.assertEqual(out.shape, (2, output_size))

        # Verify manual calculation for one output
        # We can construct dense weight for verification
        dense_weight = torch.zeros(output_size, input_size)
        dense_weight[layer.indices[0], layer.indices[1]] = layer.weight_values

        expected_out = x @ dense_weight.t() + layer.bias
        self.assertTrue(torch.allclose(out, expected_out, atol=1e-6))

if __name__ == '__main__':
    unittest.main()
