# Java Benchmarking

> Historical migration-trial reference. The Python archive/migration CLI
> commands shown below are retired and are no longer shipped. The maintained
> checks are the Java Maven suite, the offline Stage 6 benchmark, and the
> current database-simplification acceptance runners.

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

`stockguidance_disposable_java_benchmark` is a MySQL database name, not a
folder in this checkout. The command above creates it (and the temporary
`stockguidance_disposable_java_benchmark_fx` fixture database) when the writer
trial completes. The direct Java benchmark does not create a schema; it only
validates and uses an existing one. This checkout's completed trial used
`stockguidance_disposable_s7a_20261020d`, as recorded in
`output/legacy-migration/benchmark/candidates/java-writer-mysql.json`.

## Direct Java benchmark

The JDBC connection enables `rewriteBatchedStatements`; raw ticker INSERTs,
price INSERTs and price UPDATEs execute in batches of at most 1,000 rows while
retaining one transaction per session. Archive reads hash original bytes in
64 KiB blocks, preserving exact file and records identities.

The benchmark uses `JavaWriterCandidate.openExclusiveRawWriteSession(connection)`
to reuse ticker mappings and the next ID across its sessions. This API requires
exclusive use of the connection and ticker table for the scope of the run;
do not modify ticker mappings through another connection or writer during it.
New mappings enter the cache after commit, and a failed write invalidates the
cache before retry. Closing the scope clears the cache without closing the
connection. Standalone `writeRaw` calls continue to load current mappings for
each transaction. A scope cannot be reused after closing or changing its catalog.

`JavaRawBenchmarkMain` measures archive preparation plus MySQL raw publication
for the supplied sessions. Its JSON report separates preparation and write
time. The command writes the JSON report to standard output so the class can be
run directly from IntelliJ IDEA for profiling.

The schema must first contain the disposable candidate marker; the correctness
trial above is one way to provision and validate it.

```powershell
Set-Location C:\Projects\StockGuidance\java
java -Xmx512m -XX:ActiveProcessorCount=3 -XX:+UseSerialGC -cp "target/classes;target/dependency/*;lib/mysql-connector-j-9.6.0.jar" stockguidance.migration.JavaRawBenchmarkMain --manifest ..\output\legacy-migration\benchmark\candidates\manifest.json --dbinfo-file ..\dbinfo.id --disposable-schema stockguidance_disposable_java_benchmark --preparers 3 --sessions "2006-12-08,2007-09-11,2008-05-07,2008-08-04,2009-10-14,2010-05-10,2010-10-04,2011-11-03,2012-10-17,2012-12-24,2013-11-15,2014-12-04,2015-08-06,2017-03-13,2018-08-02,2018-08-29,2020-03-09,2020-03-12,2021-10-04,2021-12-09,2022-02-11,2023-02-17,2024-01-31,2024-10-14,2026-04-29" --checkpoint-directory ..\output\legacy-migration\benchmark\candidates\java-raw-manual-p3-checkpoints
```

Run the same command with `--preparers 1` and a separate checkpoint directory
to compare one versus three Java preparation threads. Redirect standard output
to a file if you want to retain the JSON report.

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
