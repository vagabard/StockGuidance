from pathlib import Path
import os
import sys


def main() -> int:
	root = Path(__file__).resolve().parents[1]
	os.chdir(root)
	sys.path.insert(0, str(root / "src"))
	os.environ["PYTHONPATH"] = str(root / "src")
	from stockguidance.performance.stage14 import Stage14Error, run_stage14, validate_prerequisites
	from stockguidance.performance.stage14_batches import BatchJournal
	try:
		context = validate_prerequisites(root / "output/legacy-migration/decision.json", root / "output/legacy-migration/stage12.json", root / "output/legacy-migration/population.json", root / "output/legacy-migration/stage13/population-manifest.json", root / "output/legacy-migration/snapshot.json")
		run_stage14(context, root / "dbinfo.id", root / "output/legacy-migration/zscores.json", batch_manager=BatchJournal(root / "dbinfo.id", root / "output/legacy-migration/stage14"), pipeline_configuration={"pipeline": "shared_java_db_copy_v1"}, allow_dependency_migration=True, prepare_only=True)
		print("Unaccepted batches restored. Accepted batches retained. No new population work started.", flush=True)
		return 0
	except (OSError, Stage14Error, ValueError) as error:
		print(f"Recovery stopped: {error}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
