from __future__ import annotations

from datetime import datetime, timezone
import random
from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/legacy-migration/zscores.json"
AUDIT_OUTPUT = ROOT / "output/legacy-migration/stage14/bounded-repair-audit.json"
AUDIT_SEED = 20261022
RANDOM_SESSIONS = 1


def main() -> int:
	os.chdir(ROOT)
	os.environ["PYTHONPATH"] = str(ROOT / "src")
	sys.path.insert(0, str(ROOT / "src"))
	from stockguidance.database.configuration import load_database_configuration
	from stockguidance.performance.stage12 import _active_writers, _connect, _manifest_names
	from stockguidance.performance.stage13 import _database_revision
	from stockguidance.performance.stage14 import (
		FAMILIES,
		WINDOWS,
		Stage14Error,
		_audit_series,
		_json,
		_output_lock,
		_sha256,
		_write_json,
		validate_prerequisites,
	)

	try:
		validate_prerequisites(ROOT / "output/legacy-migration/decision.json", ROOT / "output/legacy-migration/stage12.json", ROOT / "output/legacy-migration/population.json", ROOT / "output/legacy-migration/stage13/population-manifest.json", ROOT / "output/legacy-migration/snapshot.json")
		with _output_lock(OUTPUT):
			state = _json(OUTPUT)
			manifest_path = OUTPUT.parent / "stage14/zscore-manifest.json"
			manifest = _json(manifest_path)
			if state.get("manifest_id") != manifest.get("manifest_id"):
				raise Stage14Error("Stage 14 report and manifest identities differ")
			completed = {str(session) for chunk in state.get("chunks", ()) for session in chunk["sessions"]}
			if not completed:
				raise Stage14Error("The bounded repair audit requires at least one accepted Stage 14 session")
			selection: list[str] = []
			for chunk in state["chunks"]:
				sessions = tuple(str(session) for session in chunk["sessions"])
				selection.extend((sessions[0], sessions[-1]))
			randomizer = random.Random(AUDIT_SEED)
			selection.extend(randomizer.sample(sorted(completed), min(RANDOM_SESSIONS, len(completed))))
			selected = tuple(dict.fromkeys(selection))
			configuration = load_database_configuration(ROOT / "dbinfo.id")
			connection = _connect(ROOT / "dbinfo.id", configuration.database)
			try:
				names = _manifest_names(manifest)
				if _active_writers(connection):
					raise Stage14Error("Competing production writers are active")
				revision_before = _database_revision(connection, names)
				if revision_before != state.get("database_revision_after"):
					raise Stage14Error("Source revision differs from the accepted Stage 14 report")
				existing = _json(AUDIT_OUTPUT) if AUDIT_OUTPUT.exists() else {}
				if existing and (existing.get("manifest_id") != manifest["manifest_id"] or existing.get("source_revision_before") != revision_before or tuple(existing.get("sessions", ())) != selected):
					raise Stage14Error("Existing bounded audit belongs to another Stage 14 scope")
				series = list(existing.get("series", ()))
				completed_keys = {(str(item["session"]), str(item["family"]), int(item["window"])) for item in series}
				base = {"schema_version": 1, "status": "running", "manifest_id": manifest["manifest_id"], "manifest_sha256": _sha256(manifest_path), "source_revision_before": revision_before, "audit_seed": AUDIT_SEED, "sessions": list(selected), "series": series}
				_write_json(AUDIT_OUTPUT, base)
				for session in selected:
					for family in FAMILIES:
						for window in WINDOWS:
							key = (session, family, window)
							if key in completed_keys:
								continue
							result = _audit_series(connection, names, manifest, session, family, window)
							series.append(result)
							completed_keys.add(key)
							_write_json(AUDIT_OUTPUT, {**base, "series": series, "completed_series": len(series)})
							print(f"Audited {session} {family}/{window}: {len(series)}/{len(selected) * len(FAMILIES) * len(WINDOWS)}", flush=True)
				revision_after = _database_revision(connection, names)
				if revision_after != revision_before:
					raise Stage14Error("Source revision changed during independent audit")
				result = {**base, "status": "passed", "source_revision_after": revision_after, "completed_series": len(series), "series": series, "completed_at": datetime.now(timezone.utc).isoformat()}
				_write_json(AUDIT_OUTPUT, result)
				print(f"Bounded Stage 14 audit passed: {len(series)} series across {len(selected)} sessions", flush=True)
			finally:
				connection.rollback()
				connection.close()
		return 0
	except (OSError, Stage14Error, ValueError) as error:
		print(f"Stage 14 audit stopped: {error}", file=sys.stderr, flush=True)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
