# Java Benchmarking

This guide covers the supported Java checks and benchmarks for the current
archive migration implementation. `ArchiveCandidateReader` uses Apache Fory
JSON for archive decoding; the archive path has no alternate parser.

Run commands from `C:\Projects\StockGuidance` unless noted otherwise. Use a
distinct disposable MySQL schema for every writer trial. Never use the
production schema.

## Build and tests

Build the Java classes and runtime dependencies:

```powershell
Set-Location java
mvn -o test package
```

Run the focused archive-reader and writer tests:

```powershell
mvn -o -Dtest=JavaWriterCandidateTest test
```

These tests cover exact decimal values, fractional volumes, extreme prices,
case-sensitive tickers, duplicate JSON fields, source hashing and records
identity.

## Writer correctness trial

The correctness trial provisions and validates a disposable MySQL candidate,
then exercises recovery and restart behavior. It is not the Stage 9 timing
benchmark.

```powershell
Set-Location C:\Projects\StockGuidance
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$schema = 'stockguidance_disposable_java_benchmark'

& $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
  --run-java-writer-trial `
  --dbinfo-file dbinfo.id `
  --disposable-schema $schema `
  --manifest output\legacy-migration\benchmark\candidates\manifest.json `
  --output output\legacy-migration\benchmark\candidates\java-writer-manual.json
```

The schema must be distinct from the configured production database and must
match the manifest, table registry, corrected schema and validation-object
identities.

## Direct Java benchmark

`JavaRawBenchmarkMain` measures archive preparation plus MySQL raw publication
for the supplied sessions. Its JSON report separates preparation and write
time, but the process wall clock below also includes JVM startup and shutdown.

The schema must first contain the disposable candidate marker; the correctness
trial above is one way to provision and validate it.

```powershell
Set-Location C:\Projects\StockGuidance\java
$manifest = '..\..\output\legacy-migration\benchmark\candidates\manifest.json'
$sessions = ((Get-Content $manifest -Raw | ConvertFrom-Json).fixed_targets -join ',')
$report = '..\..\output\legacy-migration\benchmark\candidates\java-raw-manual-p3.json'
$checkpoint = '..\..\output\legacy-migration\benchmark\candidates\java-raw-manual-p3-checkpoints'

$javaArgs = @(
  '-Xmx512m', '-XX:ActiveProcessorCount=3', '-XX:+UseSerialGC',
  '-cp', 'target/classes;target/dependency/*;lib/mysql-connector-j-9.6.0.jar',
  'stockguidance.migration.JavaRawBenchmarkMain',
  '--manifest', $manifest,
  '--dbinfo-file', '..\..\dbinfo.id',
  '--disposable-schema', 'stockguidance_disposable_java_benchmark',
  '--preparers', '3',
  '--sessions', $sessions,
  '--checkpoint-directory', $checkpoint
)

$wall = Measure-Command { & java @javaArgs | Set-Content -Path $report -Encoding utf8 }
Write-Output ("Process wall seconds: " + $wall.TotalSeconds)
Get-Content $report
```

Run the same command with `--preparers 1` and separate report/checkpoint paths
to compare one versus three Java preparation threads. The internal
`workload_seconds` excludes JVM startup; `Measure-Command` includes it.

## Stage 9a comparison

The official Stage 9a runner measures Python, Java and SQL sequentially under
the same fixed manifest, candidate order rotation, disposable MySQL protocol,
four repetitions and three raw scenarios. It includes process startup through
durable commit and checkpoint work, and samples process-tree CPU/RSS when
available.

```powershell
Set-Location C:\Projects\StockGuidance
$pythonExe = 'C:\Users\formm\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$env:PYTHONPATH = Join-Path (Get-Location) 'src'

& $pythonExe -m stockguidance.cli.legacy_migration_benchmark `
  --run-stage9-raw `
  --dbinfo-file dbinfo.id `
  --manifest output\legacy-migration\benchmark\candidates\manifest.json `
  --output output\legacy-migration\benchmark\candidates\stage9a-java-comparison `
  --stage9-timeout-seconds 3600
```

Java results are in:

```text
output/legacy-migration/benchmark/candidates/stage9a-java-comparison/raw/java-p1.json
output/legacy-migration/benchmark/candidates/stage9a-java-comparison/raw/java-p3.json
```

The `matching_adoption` scenario uses all 25 fixed sessions. The repair and
unchanged-rerun scenarios use the first fixed session. Each scenario has one
first invocation and three warm repetitions. `warm_median_seconds` is the
median of the three warm end-to-end trials for that scenario; the configuration
warm median is the sum of the three scenario medians.

The current Stage 9a CLI runs all three backends; it does not provide a
Java-only filter. Do not interpret the Stage 9 aggregate as complete Stage 9:
derived and z-score timing remains pending until Stages 9b and 9c.

## Reading results

Inspect Java p3's aggregate warm median and throughput:

```powershell
$report = Get-Content output\legacy-migration\benchmark\candidates\stage9a-java-comparison\raw\java-p3.json -Raw | ConvertFrom-Json
$report.warm_median_seconds
$report.throughput_rows_per_second
$report.status
```

Inspect the individual scenario medians:

```powershell
$report.scenarios.matching_adoption.warm_median_seconds
$report.scenarios.missing_or_different_repair.warm_median_seconds
$report.scenarios.unchanged_rerun.warm_median_seconds
```

Hash reports when retaining results as evidence:

```powershell
Get-FileHash `
  output\legacy-migration\benchmark\candidates\stage9a-java-comparison\stage9a-raw.json, `
  output\legacy-migration\benchmark\candidates\stage9a-java-comparison\study.json `
  -Algorithm SHA256
```

The retired parser-only A/B harness is not a supported current benchmark. Its
historical measurements exclude database publication and must
not be compared directly with Stage 9a end-to-end timings.
