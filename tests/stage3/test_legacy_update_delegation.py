import ast
import __future__
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockguidance.workflows.failures import StageExecutionError


def load_legacy_main(namespace):
	path = Path(__file__).resolve().parents[2] / "choose_next_day_stocks.py"
	source = ast.parse(path.read_text(encoding="utf-8"))
	function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "main")
	code = compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag)
	exec(code, namespace)
	return namespace["main"]


@pytest.mark.parametrize("failure", [False, True])
def test_legacy_frontend_propagates_update_failure_before_training(failure, capsys):
	arguments = SimpleNamespace(target_date=None, data_dir="custom-root")
	calls = []

	def update(args):
		calls.append(args)
		assert args.data_root == "custom-root"
		if failure:
			raise RuntimeError("fixture secret")
		return SimpleNamespace(successful=False)

	namespace = {"_build_arg_parser": lambda: SimpleNamespace(parse_args=lambda argv: arguments), "assemble_and_update": update, "StageExecutionError": StageExecutionError, "date": date}
	assert load_legacy_main(namespace)([]) == 1
	assert calls == [arguments]
	assert "fixture secret" not in capsys.readouterr().out


def test_target_date_id_is_resolved_only_after_successful_ingestion():
	arguments = SimpleNamespace(target_date="2024-07-02", data_dir="custom-root", end_date="2024-07-08", dbinfo_file="custom.id")
	events = []

	def update(args):
		events.append("update")
		assert args.end_date == "2024-07-02"
		return SimpleNamespace(successful=True)

	def latest(**kwargs):
		events.append("latest")
		assert kwargs["dbinfo_file"] == "custom.id"
		return 901, date(2024, 7, 2)

	def lookup(session_date, **kwargs):
		events.append("lookup")
		assert session_date == date(2024, 7, 2)
		return None

	namespace = {"_build_arg_parser": lambda: SimpleNamespace(parse_args=lambda argv: arguments), "assemble_and_update": update, "get_latest_raw_stock_data": latest, "_lookup_date_id_for_transaction_date": lookup, "StageExecutionError": StageExecutionError, "date": date}
	assert load_legacy_main(namespace)([]) == 1
	assert events == ["update", "latest", "lookup"]
