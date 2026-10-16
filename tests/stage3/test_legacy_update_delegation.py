import pytest


@pytest.mark.parametrize("module_name", ["choose_next_day_stocks", "run_recommender_from_model_dir"])
def test_migrated_recommendation_frontends_delegate_to_shared_cli(module_name):
	from importlib import import_module
	from stockguidance.cli.recommend import main

	assert import_module(module_name).main is main


def test_recommendation_cli_requires_explicit_policy_and_artifact():
	from stockguidance.cli.recommend import parse_args

	with pytest.raises(SystemExit) as error:
		parse_args([])
	assert error.value.code == 2
