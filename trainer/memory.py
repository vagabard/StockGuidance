from __future__ import annotations

from .utils import fmt_bytes


def estimate_memory(provider, args, input_size: int, output_size: int, n_total: int, train_ds, val_ds, test_ds) -> None:
    """Print an approximate memory usage estimate based on model and dataset sizes."""
    B = int(args.batch_size)
    S = int(input_size)
    channels = 16
    ks = 15
    nhead = 4 if channels % 4 == 0 else 1
    dim_ff = max(64, channels * 4)
    conv1_params = (1 * channels * ks) + channels
    conv2_params = (channels * channels * ks) + channels
    conv3_params = (channels * channels * ks) + channels
    proj_params = (channels * output_size * 1) + output_size
    attn_params_per = (3 * channels * channels) + (3 * channels) + (channels * channels) + channels
    ff_params_per = (channels * dim_ff + dim_ff) + (dim_ff * channels + channels)
    ln_params_per = 4 * channels
    trans_params_per = attn_params_per + ff_params_per + ln_params_per
    trans_total = 2 * trans_params_per
    param_count = conv1_params + conv2_params + conv3_params + proj_params + trans_total
    param_bytes = float(param_count) * 4.0
    grad_bytes = param_bytes
    adam_state_bytes = param_bytes * 2.0
    activ_elems = B * (5 * channels * S + output_size * S)
    activations_bytes = float(activ_elems) * 4.0
    activations_peak_train_bytes = activations_bytes * 2.0
    batch_input_bytes = B * S * 4.0
    batch_target_bytes = B * output_size * 4.0
    D = int(provider._D)
    TF = int(provider._TF)
    T = int(provider._T)
    dates_bytes = provider._dates.size * 4.0
    X_bytes = D * TF * 8.0
    Y_bytes = D * T * 8.0
    provider_ram_bytes = dates_bytes + X_bytes + Y_bytes
    n_train = len(train_ds)
    materialized_train_feats_bytes = n_train * input_size * 4.0
    materialized_train_labels_bytes = n_train * output_size * 4.0
    materialized_train_total = materialized_train_feats_bytes + materialized_train_labels_bytes
    model_bytes_total = param_bytes + grad_bytes + adam_state_bytes
    train_peak_bytes = model_bytes_total + activations_peak_train_bytes + batch_input_bytes + batch_target_bytes
    infer_peak_bytes = param_bytes + batch_input_bytes + batch_target_bytes + activations_bytes
    print("Memory estimate (approximate):")
    print(f"  Input size (sequence length): {S} | Output size: {output_size}")
    print(f"  Instances: total={n_total} train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")
    print("\nModel state (float32):")
    print(f"  Parameters: {param_count:,} -> {fmt_bytes(param_bytes)}")
    print(f"  Gradients: ~{fmt_bytes(grad_bytes)}")
    print(f"  Adam states (m,v): ~{fmt_bytes(adam_state_bytes)}")
    print(f"  Model total (params + grads + Adam): ~{fmt_bytes(model_bytes_total)}")
    print("\nPer-batch (batch_size = %d):" % B)
    print(f"  Inputs: {fmt_bytes(batch_input_bytes)} | Targets: {fmt_bytes(batch_target_bytes)}")
    print(f"  Activations (forward): ~{fmt_bytes(activations_bytes)}")
    print(f"  Training peak (model + activations*2 + batch): ~{fmt_bytes(train_peak_bytes)}")
    print(f"  Inference peak (model + activations + batch): ~{fmt_bytes(infer_peak_bytes)}")
    print("\nProvider cached arrays (host RAM):")
    print(f"  dates int32: {fmt_bytes(dates_bytes)} | X_flat float64: {fmt_bytes(X_bytes)} | Y float64: {fmt_bytes(Y_bytes)}")
    print(f"  Provider total: ~{fmt_bytes(provider_ram_bytes)}")
    print("\nIf you fully materialize all TRAIN features/labels as float32 tensors (not required by script):")
    print(f"  Train features total: {fmt_bytes(materialized_train_feats_bytes)}")
    print(f"  Train labels total:   {fmt_bytes(materialized_train_labels_bytes)}")
    print(f"  Materialized train total: {fmt_bytes(materialized_train_total)}")


__all__ = ["estimate_memory"]
