"""
Simple test for training_instance_provider.

Creates a provider, configures the number of prior days to include,
fetches one feature/label pair, and prints the lengths of the returned
feature and label tensors/arrays as requested.

Run:
  python test_training_instance_provider.py

Optional env vars (to override DB creds if needed):
  - MYSQL_USER
  - MYSQL_PASSWORD
"""
from __future__ import annotations

import os
import sys

from training_instance_provider import create_provider
import numpy as np


def main() -> int:
    # You can tweak these defaults if desired
    use_torch = False
    num_prior_days = 2  # include current day + 2 prior days
    instance_index = 1  # 1-based index
    limit = None  # set to e.g. 5000 to cap rows pulled from DB

    try:
        provider = create_provider(
            use_torch=use_torch,
            limit=limit,
            # DB connection can be overridden via environment if needed
            user=os.getenv("MYSQL_USER", "Redbeard"),
            password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
        )
        # Loop prior-day window from 0 to 5 (inclusive)
        one_day_feature_length = None  # captured when n_prior == 0
        baseline_idx1_vals = None  # type: ignore[var-annotated]
        baseline_idx2_vals = None  # type: ignore[var-annotated]
        # Counters for summary
        days_compared_1 = 0
        days_equal_1 = 0

        for n_prior in range(0, 6):
            try:
                provider.set_how_many_days_in_past_to_use(n_prior)
            except Exception as e:
                print(f"n_prior={n_prior}: cannot set prior days (reason: {e})")
                continue

            max_n = provider.get_maximum_number_of_test_train_instances()
            if max_n <= 0:
                print(f"n_prior={n_prior}: No instances available. Skipping.")
                continue

            idx = instance_index if instance_index <= max_n else max_n
            features, labels, date_id_extra = provider.get_feature_and_label_pair(idx)

            # Print basic info for this configuration
            print(f"n_prior={n_prior}: max_instances={max_n}, feature_length={len(features)}, label_length={len(labels)}")

            # Capture the single-day feature length when n_prior == 0
            if n_prior == 0:
                one_day_feature_length = int(len(features))

            if one_day_feature_length is None:
                # Shouldn't happen, but safeguard
                print(f"n_prior={n_prior}: one_day_feature_length unavailable; skipping comparisons.")
                continue

            # Compute start indices that should refer to the SAME absolute date across n_prior
            idx1_int = int(200 * 7 + one_day_feature_length * n_prior)
            idx2_int = int(201 * 7 + one_day_feature_length * n_prior)

            feat_len = int(len(features))
            def slice7(base: int):
                vals = []
                for off in range(7):
                    i = base + off
                    if 0 <= i < feat_len:
                        try:
                            vals.append(float(features[i]))
                        except Exception:
                            vals.append(None)
                    else:
                        vals.append(None)
                return vals

            try:
                seq1 = slice7(idx1_int)
                seq2 = slice7(idx2_int)
            except Exception as ex:
                print(f"n_prior={n_prior}: error extracting sequences: {ex}")
                continue

            # Initialize baseline on first run, print once
            if baseline_idx1_vals is None and baseline_idx2_vals is None:
                baseline_idx1_vals = seq1
                baseline_idx2_vals = seq2
                print("Baseline (n_prior=0) sequences:")
                print(f"  idx1 start={200*7} -> values: {baseline_idx1_vals}")
                print(f"  idx2 start={201*7} -> values: {baseline_idx2_vals}")
                continue

            # Compare to baseline; report mismatches only, and return if mismatch occurred
            def report_mismatch(tag: str, base_start: int, expected, actual) -> bool:
                mismatches = []
                for k, (e, a) in enumerate(zip(expected, actual)):
                    if (e is None) != (a is None) or (e is not None and a is not None and not np.isclose(e, a, equal_nan=True)):
                        mismatches.append((k, e, a))
                if mismatches:
                    print(f"ERROR n_prior={n_prior}: {tag} start={base_start + one_day_feature_length * n_prior} does not match baseline.")
                    for k, e, a in mismatches:
                        print(f"  offset {k}: expected {e}, got {a}")
                    return True
                return False

            mism1 = report_mismatch("idx1", 200*7, baseline_idx1_vals, seq1)
            mism2 = report_mismatch("idx2", 201*7, baseline_idx2_vals, seq2)
            # Only count as compared when we have a baseline and were able to extract sequences
            days_compared_1 += 1
            if not mism1 and not mism2:
                days_equal_1 += 1

        # Repeat the entire loop but using instance index 233
        print("\n=== Re-running with instance_index=233 ===")
        instance_index_233 = 233
        # Reset baselines for the second run (instance 233)
        baseline_idx1_vals = None
        baseline_idx2_vals = None
        days_compared_233 = 0
        days_equal_233 = 0
        for n_prior in range(0, 6):
            try:
                provider.set_how_many_days_in_past_to_use(n_prior)
            except Exception as e:
                print(f"[233] n_prior={n_prior}: cannot set prior days (reason: {e})")
                continue

            max_n = provider.get_maximum_number_of_test_train_instances()
            if max_n <= 0:
                print(f"[233] n_prior={n_prior}: No instances available. Skipping.")
                continue

            idx = instance_index_233 if instance_index_233 <= max_n else max_n
            features, labels, date_id_extra = provider.get_feature_and_label_pair(idx)

            # Print basic info for this configuration
            print(f"[233] n_prior={n_prior}: max_instances={max_n}, feature_length={len(features)}, label_length={len(labels)}")

            # Capture single-day feature length if still unknown
            if one_day_feature_length is None and n_prior == 0:
                one_day_feature_length = int(len(features))

            if one_day_feature_length is None:
                print(f"[233] n_prior={n_prior}: one_day_feature_length unavailable; skipping comparisons.")
                continue

            idx1_int = int(200 * 7 + one_day_feature_length * n_prior)
            idx2_int = int(201 * 7 + one_day_feature_length * n_prior)

            feat_len = int(len(features))
            def slice7_233(base: int):
                vals = []
                for off in range(7):
                    i = base + off
                    if 0 <= i < feat_len:
                        try:
                            vals.append(float(features[i]))
                        except Exception:
                            vals.append(None)
                    else:
                        vals.append(None)
                return vals

            try:
                seq1 = slice7_233(idx1_int)
                seq2 = slice7_233(idx2_int)
            except Exception as ex:
                print(f"[233] n_prior={n_prior}: error extracting sequences: {ex}")
                continue

            if baseline_idx1_vals is None and baseline_idx2_vals is None:
                baseline_idx1_vals = seq1
                baseline_idx2_vals = seq2
                print("[233] Baseline (n_prior=0) sequences:")
                print(f"  idx1 start={200*7} -> values: {baseline_idx1_vals}")
                print(f"  idx2 start={201*7} -> values: {baseline_idx2_vals}")
                continue

            def report_mismatch_233(tag: str, base_start: int, expected, actual) -> bool:
                mismatches = []
                for k, (e, a) in enumerate(zip(expected, actual)):
                    if (e is None) != (a is None) or (e is not None and a is not None and not np.isclose(e, a, equal_nan=True)):
                        mismatches.append((k, e, a))
                if mismatches:
                    print(f"ERROR [233] n_prior={n_prior}: {tag} start={base_start + one_day_feature_length * n_prior} does not match baseline.")
                    for k, e, a in mismatches:
                        print(f"  offset {k}: expected {e}, got {a}")
                    return True
                return False

            mism1 = report_mismatch_233("idx1", 200*7, baseline_idx1_vals, seq1)
            mism2 = report_mismatch_233("idx2", 201*7, baseline_idx2_vals, seq2)
            days_compared_233 += 1
            if not mism1 and not mism2:
                days_equal_233 += 1

        # Final summary prints
        print("\n=== Summary ===")
        print(f"Instance index=1: compared {days_compared_1} day-window(s), equal to baseline: {days_equal_1}")
        print(f"Instance index=233: compared {days_compared_233} day-window(s), equal to baseline: {days_equal_233}")

        # After the current test flow has run: take the largest number of prior days tested,
        # and iterate through the feature vector at positions
        #   200*7 + one_day_feature_length*day_index
        #   201*7 + one_day_feature_length*day_index
        # for day_index in 0..max_prior_tested
        print("\n=== Post-run: iterate features at max prior-days tested ===")

        # Determine the largest valid prior-days window among 0..5
        def find_max_valid_prior() -> int:
            for cand in range(5, -1, -1):
                try:
                    provider.set_how_many_days_in_past_to_use(cand)
                    if provider.get_maximum_number_of_test_train_instances() > 0:
                        return cand
                except Exception:
                    continue
            return 0

        max_prior_tested = find_max_valid_prior()
        print(f"Max prior-days tested and available: {max_prior_tested}")

        # Ensure we know the one-day feature length
        if one_day_feature_length is None:
            try:
                provider.set_how_many_days_in_past_to_use(0)
                max_n0 = provider.get_maximum_number_of_test_train_instances()
                if max_n0 > 0:
                    idx0 = 1 if 1 <= max_n0 else max_n0
                    feats0, _ = provider.get_feature_and_label_pair(idx0)
                    one_day_feature_length = int(len(feats0))
            except Exception:
                one_day_feature_length = None

        if one_day_feature_length is None:
            print("Cannot determine one_day_feature_length; skipping post-run iteration.")
            return 0

        def slice7_at(features_vec, start_idx: int):
            n = int(len(features_vec))
            vals = []
            for off in range(7):
                i = start_idx + off
                if 0 <= i < n:
                    try:
                        vals.append(float(features_vec[i]))
                    except Exception:
                        vals.append(None)
                else:
                    vals.append(None)
            return vals

        # Run for both instance indices to mirror earlier checks
        for inst in (1, 233):
            try:
                provider.set_how_many_days_in_past_to_use(max_prior_tested)
            except Exception as e:
                print(f"[post] Could not set max_prior={max_prior_tested}: {e}")
                continue

            max_n = provider.get_maximum_number_of_test_train_instances()
            if max_n <= 0:
                print(f"[post] max_prior={max_prior_tested}: No instances available.")
                continue

            idx = inst if inst <= max_n else max_n
            features, labels, date_id_extra = provider.get_feature_and_label_pair(idx)
            print(f"[post inst={inst}] Using max_prior={max_prior_tested}, feature_length={len(features)}, label_length={len(labels)}")

            # Iterate day_index 0..max_prior_tested and print 7-value sequences at the requested starts
            for day_index in range(0, max_prior_tested + 1):
                idx1 = int(200 * 7 + one_day_feature_length * day_index)
                idx2 = int(201 * 7 + one_day_feature_length * day_index)
                seq1 = slice7_at(features, idx1)
                seq2 = slice7_at(features, idx2)
                print(f"  day_index={day_index}: idx1_start={idx1} values={seq1}")
                print(f"  day_index={day_index}: idx2_start={idx2} values={seq2}")

        return 0
    except Exception as e:
        print("Test failed with exception:", repr(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
