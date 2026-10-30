import json
from pathlib import Path
import re
import tomllib

from stockguidance.cli.preview_schema import parser as preview_parser
from stockguidance.cli.update_zscores import parser as update_zscores_parser
from stockguidance.cli.validate_database import parser as validate_parser


ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
	return (ROOT / path).read_text(encoding="utf-8")


def test_operator_runbook_has_the_required_order_and_semantics() -> None:
	operations = _text("docs/operations_guide.md")
	steps = (
		"1. Select the authoritative registry",
		"2. Preview the selected names",
		"3. Explicitly initialize the compatible updater tables",
		"4. Preview the requested z-score windows",
		"5. Explicitly initialize and populate the selected z-score tables",
		"6. Run the read-only Python validator",
	)
	positions = tuple(operations.index(step) for step in steps)
	assert positions == tuple(sorted(positions))
	for phrase in (
		"20,60",
		"relative to `config/dbinfo.id`",
		"falls back to the packaged resource",
		"does not rename, migrate or convert existing",
		"FLOAT` persistence has float32 precision",
		"stored `close_ratio_to_prior` value minus one",
		"stale\n   results before a rebuild",
		"cannot recover overwritten source revisions",
	):
		assert phrase in operations


def test_future_feature_and_encoding_boundary_is_documented() -> None:
	features = _text("docs/close_samples.md")
	for phrase in (
		"future versioned feature adapter",
		"normalization window",
		"calculation version",
		"availability policy",
		"input layout",
		"warm-up scope",
		"sample, cache and\nartifact identity",
		"stable database ticker IDs",
		"stock vocabulary",
		"unknown-stock policy",
		"never\nbecome vector offsets",
	):
		assert phrase in features


def test_canonical_registry_is_packaged_and_shared_with_java() -> None:
	registry = json.loads(_text("src/stockguidance/database/table_names.json"))
	assert set(registry) == {"schema_version", "tables", "zscore_templates", "preparation_templates"}
	assert set(registry["tables"]) == {
		"source", "sessions", "tickers", "prices", "deltas", "imports",
		"zscore_series", "zscore_sessions", "schema", "data_states",
		"pending_updates", "repair_metadata",
	}
	assert set(registry["zscore_templates"]) == {"price", "price_delta"}
	assert set(registry["preparation_templates"]) == {"volume"}

	project = tomllib.loads(_text("pyproject.toml"))
	assert "database/table_names.json" in project["tool"]["setuptools"]["package-data"]["stockguidance"]
	pom = _text("java/pom.xml")
	assert "${project.basedir}/../src/stockguidance/database" in pom
	assert "<include>table_names.json</include>" in pom
	assert 'private static final String RESOURCE = "/stockguidance/database/table_names.json";' in _text(
		"java/src/main/java/stockguidance/database/TableNames.java"
	)


def test_supported_production_sources_have_no_physical_name_fallbacks() -> None:
	physical_names = (
		"stockguidance_source",
		"transaction_dates",
		"ticker_symbol",
		"raw_stock_data",
		"daily_delta_stock_data",
		"stockguidance_imports",
		"stockguidance_zscore_series",
		"stockguidance_zscore_sessions",
		"zscores_price_",
		"zscores_price_delta_",
		"zscores_volume_",
	)
	production_files = tuple(
		path for path in (ROOT / "src/stockguidance").rglob("*.py") if "performance" not in path.parts
	) + tuple(
		(ROOT / "java/src/main").rglob("*.java")
	)
	for path in production_files:
		content = path.read_text(encoding="utf-8")
		assert not any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", content) for name in physical_names), path


def test_documented_zscore_frontends_use_current_options() -> None:
	preview_help = preview_parser().format_help()
	update_help = update_zscores_parser().format_help()
	validate_help = validate_parser().format_help()
	assert "--table-names-file" in preview_help and "--windows" in preview_help
	assert all(option in update_help for option in ("--windows", "--start-date", "--end-date", "--initialize-schema"))
	assert "--zscore-windows" in validate_help
