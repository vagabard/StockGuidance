from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from stockguidance.cli import runs as runs_cli
from stockguidance.runs import (
	REDACTED_VALUE,
	ReportState,
	RunConfig,
	RunDirectoryService,
	RunOperation,
	RunStatus,
)


def test_sanitized_configuration_round_trips_without_credentials(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "custom-runs")
	config = RunConfig(
		RunOperation.TRAINING,
		{
			"epochs": 3,
			"password": "fixture-password-never-save",
			"nested": {"api_token": "fixture-token-never-save"},
			"note": "password=fixture-password-never-save",
			"date": date(2026, 10, 29),
			"timestamp": datetime(2026, 10, 29, 12, tzinfo=timezone.utc),
			"amount": Decimal("1000.00"),
			"path": Path("fixture-dbinfo.id"),
		},
	)
	assert config.settings["password"] == REDACTED_VALUE
	handle = service.create(config, provenance={"git_revision": "fixture", "dirty_tree": False})
	serialized = handle.manifest_path.read_text(encoding="utf-8")
	assert "fixture-password-never-save" not in serialized and "fixture-token-never-save" not in serialized
	manifest = service.read(handle.run_id)
	assert manifest.original_configuration["date"] == {"__type__": "date", "value": "2026-10-29"}
	assert manifest.original_configuration["amount"] == {"__type__": "decimal", "value": "1000.00"}
	assert manifest.provenance["dirty_tree"] is False


def test_status_transitions_failure_and_incomplete_simulation_are_distinct(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	training = service.create(RunConfig(RunOperation.TRAINING, {"source": "fixture"}), provenance={})
	running = service.start(training.run_id)
	assert running.status is RunStatus.RUNNING and running.timestamps.started_at is not None
	service.update_sections(training.run_id, resolved_configuration={"resolved": True}, sections={"identity": {"source_revision": "fixture-1"}})
	completed = service.complete(training.run_id)
	assert completed.status is RunStatus.COMPLETED and service.read(training.run_id).resolved_configuration == {"resolved": True}
	with pytest.raises(ValueError, match="transition"):
		service.start(training.run_id)

	simulation = service.create(RunConfig(RunOperation.SIMULATION, {"horizon": 10}), provenance={})
	service.start(simulation.run_id)
	incomplete = service.complete(simulation.run_id, report_state=ReportState.INCOMPLETE, incomplete_report={"reason": "missing final mark"})
	assert incomplete.status is RunStatus.COMPLETED
	assert incomplete.report_state is ReportState.INCOMPLETE
	assert incomplete.failure is None

	failed = service.create(RunConfig(RunOperation.RECOMMENDATION, {"top_n": 5}), provenance={})
	failed_manifest = service.fail(failed.run_id, stage="scoring", code="missing_input", message="password=not-recorded", retryable=True)
	assert failed_manifest.status is RunStatus.FAILED
	assert failed_manifest.failure is not None and "not-recorded" not in failed_manifest.failure.message
	assert failed_manifest.original_configuration["top_n"] == 5


def test_concurrent_creation_and_explicit_collision_do_not_overwrite(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	config = RunConfig(RunOperation.REPLAY, {"fixture": True})

	def create_one(_: int) -> str:
		return service.create(config, provenance={}).run_id

	with ThreadPoolExecutor(max_workers=8) as executor:
		run_ids = tuple(executor.map(create_one, range(24)))
	assert len(set(run_ids)) == 24
	assert all(service.read(run_id).status is RunStatus.PENDING for run_id in run_ids)
	with pytest.raises(ValueError, match="already exists"):
		service.create(config, run_id=run_ids[0], provenance={})


def test_parent_child_integrity_and_parent_immutability(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	parent = service.create(RunConfig(RunOperation.TRAINING, {"seed": 7}), provenance={})
	parent_before = parent.manifest_path.read_bytes()
	child = service.create(RunConfig(RunOperation.RESCORE, {"changed": "fixture"}, parent_run_id=parent.run_id), provenance={})
	assert service.read(child.run_id).parent_run_id == parent.run_id
	assert parent.manifest_path.read_bytes() == parent_before
	assert tuple(item.run_id for item in service.children(parent.run_id)) == (child.run_id,)
	with pytest.raises(ValueError, match="does not exist"):
		service.create(RunConfig(RunOperation.RERUN, {}, parent_run_id="missing-parent"), provenance={})


def test_corrupt_or_incomplete_manifests_are_rejected_and_listed(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	handle = service.create(RunConfig(RunOperation.TRAINING, {}), provenance={})
	handle.manifest_path.write_text("{", encoding="utf-8")
	with pytest.raises(ValueError, match="missing, incomplete, or corrupt"):
		service.read(handle.run_id)
	items = service.list_runs()
	assert len(items) == 1 and not items[0].valid and items[0].error == "Invalid or incomplete run manifest"

	second = service.create(RunConfig(RunOperation.TRAINING, {}), provenance={})
	second.manifest_path.write_text(json.dumps({"schema": "stockguidance-run-1"}), encoding="utf-8")
	with pytest.raises(ValueError):
		service.validate(second.run_id)


def test_artifact_references_are_relative_hash_bound_and_validated(tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	handle = service.create(RunConfig(RunOperation.TRAINING, {}), provenance={})
	artifact = handle.artifacts_directory / "model.sgmodel"
	artifact.write_bytes(b"fixture-model")
	reference = service.register_file(handle.run_id, name="model", relative_path="artifacts/model.sgmodel")
	assert reference.relative_path == "artifacts/model.sgmodel"
	assert service.validate(handle.run_id).artifacts == (reference,)
	artifact.write_bytes(b"changed")
	with pytest.raises(ValueError, match="unavailable or changed"):
		service.validate(handle.run_id)
	with pytest.raises(ValueError, match="inside the run"):
		service.register_file(handle.run_id, name="escape", relative_path="../outside.bin")
	with pytest.raises(ValueError, match="path-safe"):
		service.handle("../outside")


def test_runs_cli_lists_reads_and_validates_without_extra_work(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	service = RunDirectoryService(tmp_path / "runs")
	handle = service.create(RunConfig(RunOperation.RECOMMENDATION, {"top_n": 5}), provenance={})
	assert runs_cli.main(["--runs-root", str(tmp_path / "runs"), "list"]) == 0
	assert json.loads(capsys.readouterr().out)["results"][0]["run_id"] == handle.run_id
	assert runs_cli.main(["--runs-root", str(tmp_path / "runs"), "read", handle.run_id]) == 0
	assert json.loads(capsys.readouterr().out)["manifest"]["schema"] == "stockguidance-run-1"
	assert runs_cli.main(["--runs-root", str(tmp_path / "runs"), "validate", handle.run_id]) == 0
	assert json.loads(capsys.readouterr().out)["successful"] is True


def test_runs_cli_help_has_no_filesystem_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
	monkeypatch.chdir(tmp_path)
	with pytest.raises(SystemExit) as error:
		runs_cli.main(["--help"])
	assert error.value.code == 0
	assert capsys.readouterr().out and not tuple(tmp_path.iterdir())
