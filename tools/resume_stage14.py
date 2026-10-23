from pathlib import Path
import os
import sys
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CALCULATION_WORKERS = 3
UPDATE_WORKERS = 1
JAVA_HEAP = "64g"
AUTO_TUNE = True


def main(max_chunks: int | None = None) -> int:
	os.chdir(ROOT)
	os.environ["PYTHONPATH"] = str(ROOT / "src")
	for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
		os.environ[name] = "1"
	sys.path.insert(0, str(ROOT / "src"))
	from stockguidance.performance.stage14 import Stage14Error, run_stage14, validate_prerequisites
	from stockguidance.performance.stage14_pipeline import JavaPipeline
	from stockguidance.performance.stage14_batches import BatchJournal
	from stockguidance.performance.stage14_progress import ProgressReporter

	started = perf_counter()
	def progress(state: dict[str, Any]) -> None:
		completed = sum(len(chunk["sessions"]) for chunk in state["chunks"])
		latest = state["chunks"][-1]
		print(f"Chunk {latest['index']:04d} accepted: {completed}/{state['scope']['requested_sessions']} sessions; chunk {latest['seconds']:.1f}s; run {(perf_counter() - started) / 60:.1f} minutes", flush=True)
		if completed == state["scope"]["requested_sessions"]:
			print("Population finished; independent boundary and random-session audits follow.", flush=True)

	try:
		context = validate_prerequisites(ROOT / "output/legacy-migration/decision.json", ROOT / "output/legacy-migration/stage12.json", ROOT / "output/legacy-migration/population.json", ROOT / "output/legacy-migration/stage13/population-manifest.json", ROOT / "output/legacy-migration/snapshot.json")
		print(f"Resuming Stage 14: auto-selecting up to 10 calculation workers, {UPDATE_WORKERS} publication workers, -Xmx{JAVA_HEAP}. Checkpoints: output/legacy-migration/stage14", flush=True)
		with ProgressReporter(ROOT / "output/legacy-migration/zscores.json"), JavaPipeline(CALCULATION_WORKERS, UPDATE_WORKERS, JAVA_HEAP, auto_tune=AUTO_TUNE) as pipeline:
			state = run_stage14(context, ROOT / "dbinfo.id", ROOT / "output/legacy-migration/zscores.json", java_runner=pipeline, pipeline_configuration=pipeline.configuration, on_chunk_complete=progress, batch_manager=BatchJournal(ROOT / "dbinfo.id", ROOT / "output/legacy-migration/stage14"), allow_dependency_migration=True, max_chunks=max_chunks)
		print(f"Stage 14 {state['status']}; report: output/legacy-migration/zscores.json", flush=True)
		return 0 if state["status"] == "complete" or max_chunks is not None and len(state["chunks"]) >= max_chunks else 1
	except KeyboardInterrupt:
		print("Interrupted. Committed checkpoints are retained; rerun this script to resume.", flush=True)
		return 130
	except (OSError, Stage14Error, ValueError) as error:
		print(f"Stage 14 stopped: {error}. Review the failure before restarting.", file=sys.stderr, flush=True)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
