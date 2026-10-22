import json
import os
import subprocess
from pathlib import Path

root = Path("output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready")
report = root / "stage9c-zscores.json"
python = r"C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe"
command = [python, "-m", "stockguidance.cli.legacy_migration_benchmark", "--run-stage9-zscores", "--manifest", "output/legacy-migration/benchmark/candidates/manifest.json", "--dbinfo-file", "dbinfo.id", "--output", str(root), "--stage9-profile", "compact", "--stage9-resume", "--stage9-max-trials", "1", "--stage9-audit-series-limit", "4"]
environment = {**os.environ, "PYTHONPATH": "src", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}

for attempt in range(1, 1000):
    state = json.loads(report.read_text())
    if state["status"] == "complete":
        print("comparison complete")
        break
    cell = "complete"
    for name, configuration in state["configurations"].items():
        for block, details in configuration["blocks"].items():
            for trial in details["repetitions"]:
                if trial["status"] != "complete":
                    cell = f"{name}/{block}/repetition-{trial['repetition']} ({trial['status']})"
                    break
            if cell != "complete":
                break
        if cell != "complete":
            break
    print(f"run {attempt}: {cell}", flush=True)
    result = subprocess.run(command, env=environment)
    if result.returncode:
        raise SystemExit(result.returncode)
else:
    raise SystemExit("stage9c did not complete within 999 runs")
