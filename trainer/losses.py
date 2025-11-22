from __future__ import annotations

from .compat import torch, F


def ranking_surrogate_loss(y_pred: "torch.Tensor", y_true: "torch.Tensor", temperature: float = 1.0) -> "torch.Tensor":
    """Listwise surrogate loss encouraging predicted ranks to match labels via softmax distribution matching.

    loss = KL( softmax(y_true / T) || softmax(y_pred / T) ), averaged over batch.
    """
    assert y_pred.ndim == 2 and y_true.ndim == 2, "Expected (B, O) tensors"
    T = max(1e-6, float(temperature))
    log_q = F.log_softmax(y_pred / T, dim=1)
    p = F.softmax(y_true.to(y_pred.dtype) / T, dim=1)
    return F.kl_div(log_q, p, reduction='batchmean')


__all__ = ["ranking_surrogate_loss"]
