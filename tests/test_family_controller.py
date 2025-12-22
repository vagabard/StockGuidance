from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, List, Set
from pathlib import Path

from trainer.state.family_run import FamilyRun


class DummyArgs(SimpleNamespace):
	"""Lightweight stand-in for argparse.Namespace used in tests."""


def _make_dummy_args(tmp_path, initial_run: str | None = None) -> Any:
	metrics_file = os.path.join(tmp_path, "metrics.csv")
	return DummyArgs(
		mode="train",
		initial_run=initial_run,
		prior_days="4",
		mlp_layers="3",
		lr="0.001",
		activation="relu",
		final_activation="none",
		day_change_centered="one",
		metrics_file=metrics_file,
		device="cpu",
		volume_ratio=0.0,
		vwap_ratio=0.0,
		transaction_ratio=0.0,
		limit=None,
		hidden_layers=128,
		log_interval=0,
		rank_lower=1,
		rank_upper=10,
		rank_temp=1.0,
		use_ranking_error_loss=False,
		enable_rank_err=False,
		split_mode="sequential",
		seed=None,
		precision="auto",
		arch="mlp",
		dont_use_dataloader=False,
		shuffle=False,
		num_workers=0,
		pin_memory=False,
		simulate_after=False,
		trading_interval=0,
		simulate_top_k="10",
		simulate_prior_only=False,
		ensemble_interval=0,
		run_repeats=1,
	)


def test_create_options_for_individual_runs_single_combination(tmp_path, monkeypatch) -> None:
	"""FamilyRun.create_options_for_individual_runs should produce exactly one command
	for a single-point Cartesian product and store matching argv in combinations.
	"""
	args = _make_dummy_args(tmp_path, initial_run="testfamily")
	# Use the real wrapper under project root so path resolution does not depend on tmp_path
	wrapper_path = str(Path(__file__).resolve().parents[1] / "trainer" / "train_single_run.py")

	argv: List[str] = ["train_pytorch_network.py", "--dummy"]
	day_change_centered_list = ["one"]
	prior_days_list = [4]
	mlp_layers_list = [3]
	lr_list = [0.001]
	activation_list = ["relu"]
	final_activation_list = ["none"]
	include_one_feature_list: List[str] = []
	base_optional_features: Set[str] = set()
	initial_run_id = "testfamily"
	metrics_dir = str(tmp_path)
	run_family_prefix = "run_family_"

	family_run = FamilyRun.from_args(
		args=args,
	)

	family_run.create_options_for_individual_runs()
	family_dir = str(family_run.file_structure.family_dir)

	assert os.path.isdir(family_dir)
	assert os.path.commonpath([family_dir, metrics_dir]) == metrics_dir



def test_save_and_load_family_state_roundtrip(tmp_path) -> None:
	"""FamilyRun.save and FamilyRun.load should round-trip family state and
	commands.
	"""
	initial_run_id = "roundtrip"
	args = _make_dummy_args(tmp_path, initial_run=initial_run_id)
	# Use the real wrapper under project root so path resolution does not depend on tmp_path
	wrapper_path = str(Path(__file__).resolve().parents[1] / "trainer" / "train_single_run.py")

	argv: List[str] = []
	day_change_centered_list = ["one", "half"]
	prior_days_list = [4]
	mlp_layers_list = [3]
	lr_list = [0.001]
	activation_list = ["relu"]
	final_activation_list = ["none"]
	include_one_feature_list: List[str] = []
	base_optional_features: Set[str] = set()
	metrics_dir = str(tmp_path)
	run_family_prefix = "run_family_"

	family_run_before = FamilyRun.from_args(
		args=args,
	)

	family_run_before.create_options_for_individual_runs()
	family_dir = str(family_run_before.family_dir)
	state_path = str(family_run_before.state_path)
	base_args_list = list(family_run_before.base_args_list or [])

	family_state = family_run_before.save()
	assert os.path.isfile(state_path)
	with open(state_path, "r", encoding="utf-8") as sf:
		loaded = json.load(sf)
	assert loaded["initial_run_id"] == initial_run_id
	assert loaded["base_args"] == base_args_list

	family_run_after = FamilyRun.load(family_dir)
	commands_after = [list(c.get("argv", [])) for c in (family_run_after.single_runs_for_all_combinations or [])]

	assert family_run_after.initial_run_id == initial_run_id
	assert str(family_run_after.family_dir) == family_dir
	assert str(family_run_after.state_path) == state_path

