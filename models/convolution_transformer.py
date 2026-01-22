from __future__ import annotations

from typing import List

try:
    import torch  # type: ignore
    import torch.nn as nn  # type: ignore
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    nn = None  # type: ignore


class ConvolutionAndTransformerFeedForwareNet(nn.Module):
    """Convolution + Transformer architecture with no Linear layers.

    Sequence of layers:
      1) Conv1d (kernel_size=15) on a 1-channel sequence of length input_size
      2) Transformer encoder layer
      3) Conv1d (kernel_size=15)
      4) Transformer encoder layer
      5) Conv1d (kernel_size=15)
      6) 1x1 Conv1d to project channels to output_size, then global average pooling

    The `hidden_layers` argument is retained for CLI compatibility but unused.
    """

    def __init__(self, input_size: int, output_size: int, hidden_layers: List[int]):
        super().__init__()
        channels = 16
        ks = 15
        pad = ks // 2

        # Convolutions
        self.conv1 = nn.Conv1d(1, channels, kernel_size=ks, padding=pad)
        self.conv2 = nn.Conv1d(channels, channels, kernel_size=ks, padding=pad)
        self.conv3 = nn.Conv1d(channels, channels, kernel_size=ks, padding=pad)

        # Transformer encoder layers
        nhead = 4 if channels % 4 == 0 else 1
        dim_ff = max(64, channels * 4)
        enc_layer1 = nn.TransformerEncoderLayer(
            d_model=channels,
            nhead=nhead,
            dim_feedforward=dim_ff,
            activation="gelu",
            batch_first=True,
            norm_first=True,
        )
        enc_layer2 = nn.TransformerEncoderLayer(
            d_model=channels,
            nhead=nhead,
            dim_feedforward=dim_ff,
            activation="gelu",
            batch_first=True,
            norm_first=True,
        )
        self.trans1 = nn.TransformerEncoder(enc_layer1, num_layers=1)
        self.trans2 = nn.TransformerEncoder(enc_layer2, num_layers=1)

        # Final projection via 1x1 conv to output channels
        self.proj = nn.Conv1d(channels, output_size, kernel_size=1)
        self.act = nn.ReLU()

    def forward(self, x: "torch_interface.Tensor") -> "torch_interface.Tensor":
        # Accept (B, S) or (S,)
        if x.dim() == 1:
            x = x.unsqueeze(0)
        B, S = x.shape[0], x.shape[1]

        # Treat as sequence with 1 channel -> (B, 1, S)
        x = x.view(B, 1, S)

        # Conv -> ReLU
        x = self.act(self.conv1(x))
        # Transformer expects (B, S, C)
        x = x.transpose(1, 2)
        x = self.trans1(x)
        x = x.transpose(1, 2)

        # Conv -> ReLU
        x = self.act(self.conv2(x))
        # Transformer again
        x = x.transpose(1, 2)
        x = self.trans2(x)
        x = x.transpose(1, 2)

        # Final Conv -> ReLU
        x = self.act(self.conv3(x))

        # Project to output channels and global average over sequence length
        x = self.proj(x)  # (B, O, S)
        x = x.mean(dim=2)  # (B, O)
        return x
