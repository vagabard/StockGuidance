import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession


def create_run_directory(maintenance: MaintenanceSession, stage: str) -> Path:
	context = maintenance.context
	root = Path(context.arguments.java_checkpoint_directory).resolve()
	root.mkdir(parents=True, exist_ok=True)
	directory = Path(tempfile.mkdtemp(prefix="run-", dir=root)).resolve()
	identity = {"kind": "daily-maintenance-temporary", "version": 1, "stage": stage,
		"database_configuration": str(context.database_path.resolve()), "tables": context.names.tables(),
		"start": context.targets[0].session_date.isoformat(), "end": context.targets[-1].session_date.isoformat()}
	atomic_write(directory / "owner.json", encode_json(identity))
	return directory


def cleanup_successful_run(directory: Path) -> None:
	root = directory.resolve().parent
	identity: dict[str, Any] = json.loads((directory / "owner.json").read_bytes())
	for candidate in root.glob("run-*"):
		resolved = candidate.resolve()
		if resolved.parent != root or candidate.is_symlink() or not candidate.is_dir():
			continue
		try:
			owner = json.loads((candidate / "owner.json").read_bytes())
		except (OSError, ValueError):
			continue
		if owner == identity:
			shutil.rmtree(resolved)
