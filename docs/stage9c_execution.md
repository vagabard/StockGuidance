# Stage 9c: independent steps and compact comparison

> Historical benchmark record. The Stage 9c Python/SQL/Java trial runners were
> retired after the database simplification handoff; the preserved results
> below are evidence only and the commands in this document are not supported.

Stage 9c compares Python, Java and SQL calculation **plus durable persistence**.
It is the last measurement stage before Stage 10 makes a backend choice.
The compact profile is a revised, explicitly narrower comparison on the original
immutable manifest. Stage 9a/9b evidence remains reusable.

## What failed

The preserved `stage9c-optimized-20261021-final2/stage9c-zscores.json` contains:

| Backend | Fixed 25 dates | Chronological 20 dates |
| --- | --- | --- |
| Python | First trial passed, 1,934 seconds | First trial passed, 1,775 seconds |
| Java | Repetitions 0 and 1 failed correctness | Repetitions 0 and 1 passed, 1,671/1,655 seconds |
| SQL | First trial timed out at 3,600 seconds, 63/100 series committed | First trial timed out at 3,600 seconds, 65/80 series committed |

Java's failure was ticker 19479 on 2018-08-29, price/window 20. Its SIMD result
for an exactly constant 26.69 window correctly required scalar fallback, but the
fallback's rounded mean produced a tiny nonzero variance. It now detects an
exactly constant window before computing moments. Both 20/60 windows have a
regression test. Reducing the workload alone would not fix this failure.

The old worker timeout applied only to measurement, not independent auditing.
Auditing could take substantial additional time. Previously the timed result
was saved only after the entire block audit, and the runner required fresh output
on every invocation. An interrupted conversation could leave its processes
running. The old attempt and its active worker were stopped before this revision.

## Smaller but comparable workload

`--stage9-profile compact` selects five fixed dates by evenly spaced manifest
positions, replacing the midpoint with the known 2018-08-29 regression date if
necessary. It also selects the final three chronological dates. Exact selections
are frozen in the report's protocol. Original manifest files are unchanged.

For the current manifest this reduces 45 target slots to eight, or about 82%
less target work. The two blocks share their final date, so those eight slots
cover seven distinct dates. Wall time need not fall by the same percentage: startup,
archive validation, resets and audit work still contribute.

Every selected date retains all 36,866 ticker IDs, both families, windows 20/60,
full preceding dependency history, dense NULL/status populations, float32
storage, one writer and startup-through-commit/checkpoint timing. All backends
use identical dates and inputs. Four rotated candidate orders retain one first
and three warm repetitions: 24 complete backend/block/repetition cells.

This supports a performance comparison on these dates. It does not establish
full-history runtime or the behavior of a 20-date chronological batch. Review
per-block warm samples and ranges as well as medians. If differences are near
the 10% selection threshold or warm variability is large, extend the paired
comparison before choosing a production backend. The synthetic diagnostic
continues to check recovery and edge cases separately from timing evidence.

## Independent deliverables

1. **Correctness preflight:** build Java and run the bounded three-backend
   diagnostic. This includes rollback/restart/idempotency/source-change checks.
   Resolve correctness failures before starting timed comparisons.
2. **Freeze profile and seed:** use a fresh output directory and a verified
   disposable schema. The report records code/class hashes, manifest hash,
   input fingerprint/checksum, schema identity and selected dates.
3. **Measure one cell:** `--stage9-phase measure --stage9-max-trials 1` resets
   output tables, runs one full block and atomically saves the timing before
   auditing. It ends with that cell `awaiting_audit`.
4. **Audit that cell:** resume with `--stage9-phase audit`. Each successful
   series audit is saved immediately. `--stage9-audit-series-limit 4` limits an
   invocation to at most four series (one date). A limit of one is also valid.
5. **Continue cells:** `--stage9-resume --stage9-max-trials 1` skips completed,
   audited cells and processes the next cell in the original candidate order.
   With `--stage9-audit-series-limit 4`, repeated invocations finish pending
   audit work before measuring another cell. Work is sequential on one schema.
6. **Aggregate:** each CLI invocation refreshes the study. It becomes complete
   only after every backend/block has repetitions 0, 1, 2 and 3 audited. Stage 10
   is a separate task.

Measurement and audit must use the same persisted disposable outputs. Resume
checks their checksum before accepting pending audit progress; do not run another
writer or diagnostic on that schema between these steps. A MySQL advisory lock
rejects competing Stage 9c orchestrators on that schema. Interrupting a worker's
parent cleans up the child and monitoring thread. Successful historical audit results remain valid
after the runner intentionally resets tables for the next cell.

Interrupted measurements restart the whole cell with a fresh checkpoint path;
partially committed series are not combined into a new timing. A failed or timed
out cell can be retried with `--stage9-retry-failed`; its earlier attempt is kept
in `attempt_history`. A code/input/schema/profile change requires a fresh paired
comparison. Old full-profile cells cannot be merged with compact-profile cells.
Timeout is part of the frozen protocol. Use a fresh run to change it.

## Commands

Run from the repository root. Set `$run` to a fresh output path for the first
invocation, then keep exactly the same path and profile on resume.

```powershell
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = 'src'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'
mvn -o -f java/pom.xml test package
& $pythonExe -m stockguidance.performance.stage9_zscore_diagnostic `
  --output output/legacy-migration/benchmark/candidates/stage9c-diagnostic-<fresh-run>

$run = 'output/legacy-migration/benchmark/candidates/stage9c-compact-<fresh-run>'
$common = @(
  '--run-stage9-zscores',
  '--manifest', 'output/legacy-migration/benchmark/candidates/manifest.json',
  '--dbinfo-file', 'dbinfo.id',
  '--output', $run,
  '--stage9-profile', 'compact',
  '--stage9-timeout-seconds', '3600',
  '--stage9-max-trials', '1'
)
& $pythonExe -m stockguidance.cli.legacy_migration_benchmark @common `
  --stage9-disposable-schema stockguidance_disposable_s9c_shared_5769a5a6 `
  --stage9-phase measure

& $pythonExe -m stockguidance.cli.legacy_migration_benchmark @common `
  --stage9-resume --stage9-phase audit --stage9-audit-series-limit 4

& $pythonExe -m stockguidance.cli.legacy_migration_benchmark @common `
  --stage9-resume --stage9-audit-series-limit 4
```

Repeat the last command until complete. A deliberate work-limit pause returns
exit code 0 with `comparison_complete: false` and `execution.status: paused`.
A failure returns nonzero. Check `stage9c-zscores.json` for the exact pending or
failed cell. Worker timeouts remain explicit; an audit-series limit bounds the
number of audit units, not the wall time of an individual unit.

To retain the original workload, use `--stage9-profile full` in a fresh output
directory; the same pause/resume controls apply.
