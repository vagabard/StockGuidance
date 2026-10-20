# StockGuidance Java foundation

Stage 7 establishes the current Java contract under `src/main/java`. The
historical prototypes formerly kept under `src/stockguidance` were retired
because their model readers and CSV simulator did not implement the current
Python contracts. Recover them from Git history only for source archaeology;
future Java work starts from the current contract and parity tests.

## Build and test

The build supports Java 21 bytecode. The acceptance run used OpenJDK 26.0.2.1
and Maven 3.9.9 on Windows 11. From `C:\Projects\StockGuidance`:

```powershell
Set-Location StockGuidance\java
mvn -o test
mvn -o -DskipTests package
```

The build pins Jackson Databind 2.15.2, Apache Fory JSON 1.7.4, JUnit Jupiter 5.13.4,
Maven Compiler 3.13.0, and Maven Surefire 3.5.3. MySQL Connector/J 9.6.0 is
the bundled jar in `lib`; it is a local system dependency rather than a
downloaded Maven artifact. A dependent project must provide its own JDBC
dependency instead of relying on this system-scoped path.

## Stage 7a disposable writer

`stockguidance.migration.JavaWriterCandidate` is the bounded Java writer for
the Stage 6 disposable schema. It revalidates the manifest audit identity,
archive file hashes and exact decimal record IDs before each session. Raw
publication preserves unchanged rows, uses exact-case ticker mappings and
batch statements, and commits raw data, import evidence and completion metadata
as one session transaction. Derived publication reuses
`LegacyDerivedCalculator`, requires matching raw evidence for both neighboring
sessions, and publishes the derived rows and dependency metadata atomically.

Use the existing explicit MySQL configuration with a distinct
`--disposable-schema stockguidance_disposable_...` override. Credentials stay
in the existing configuration. The target database must contain
the Stage 6 `stage6_disposable_metadata` marker with `production_target=false`
and matching schema, manifest, registry, actual DDL and validation-object hashes.
The bounded entry point processes one raw or derived
session and supports recovery probes:

```powershell
java -Xmx512m -cp "target/classes;target/dependency/*;lib/mysql-connector-j-9.6.0.jar" `
  stockguidance.migration.JavaWriterMain `
  --manifest ..\..\output\legacy-migration\benchmark\candidates\manifest.json `
  --dbinfo-file ..\..\dbinfo.id --disposable-schema <stockguidance_disposable_schema> `
  --workload raw --session <YYYY-MM-DD> `
  --checkpoint output\legacy-migration\benchmark\candidates\java-raw-checkpoint.json
```

`--interrupt before_commit` proves rollback, while
`--interrupt after_commit` leaves durable publication for the next idempotent
rerun. Checkpoints are rejected when their manifest, workload, session or
content identity is stale. This candidate is not a production writer and does
not implement Stage 7b z-scores.

From the checkout root, provision and validate a fresh disposable MySQL target
with Python/Java shared precision, case, transaction and recovery fixtures plus
the fixed 25 real raw/derived sessions:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
# Set $pythonExe using PyCharm get_python_environment.
& $pythonExe -m stockguidance.cli.legacy_migration_benchmark --run-java-writer-trial `
  --dbinfo-file dbinfo.id --disposable-schema stockguidance_disposable_s7a_<unique_suffix> `
  --output output/legacy-migration/benchmark/candidates/java-writer-mysql.json
```

The harness uses corrected Stage 5 DECIMAL types, binary ticker collation,
InnoDB, retained IDs and equivalent ticker-validation triggers/procedure. It
refuses existing targets whose identities changed and never resets schemas.
SQLite is available only as an explicit offline Python fixture option; its
historical write timings are excluded from the MySQL candidate comparison.

## Archive parser

`ArchiveCandidateReader` uses Apache Fory JSON as its sole archive decoder.
It retains strict duplicate-key rejection, exact decimal fields, source
hashing, timestamp validation and records identity checks. The parser is used
by `JavaRawBenchmarkMain` and the disposable writer path.

See [`docs/benchmarkingJava.md`](../../docs/benchmarkingJava.md) for the
available correctness, direct writer and Stage 9a benchmark commands.

## Current database contract

Use `stockguidance.database.DatabaseConfiguration.load(Path)` with an explicit
JSON path. The accepted fields are `host`, `database`, `schema`, `user`,
`password`, `port`, and `driver`; unknown fields and wrong JSON types fail.
`schema` is an alias for `database` and a mismatch is rejected. The default
host is `localhost`, port is `3306`, driver is `auto`, and password is empty;
database and user remain required. No parent-directory search, environment
fallback, key/value format, or legacy model configuration is active.

Configuration and connection failures use credential-safe messages. Passwords
never appear in `toString` or exception text. `MySqlConnectionFactory` uses
allowlisted database identifiers and JDBC properties rather than putting
credentials in a URL.

`ReadOnlyTransaction` sets `autoCommit=false`, read-only mode, and
`TRANSACTION_REPEATABLE_READ`. It rolls back after both successful and failed
inspection work and always closes the connection. `PreparedDateRangeQuery`
uses prepared date values and allowlists table and column identifiers.

## Python/Java interchange contract v1

`interchange-v1.json` is a neutral fixture generated from the current Python
source, availability, and typed data values by
`tools/stage7_generate_fixtures.py`. `InterchangeCodec` validates these fields:

- source identity, calendar identity, explicit chronological exchange sessions,
  and the ordered ticker ID list; ticker IDs are not sorted or renumbered;
- observed, verified, and hole dates with coverage provenance and provider
  state;
- nullable observation values, actual receipt `observed_at` and `available_at`
  times, revision identities, and an availability policy;
- typed errors and findings, including optional dates and ordered ticker IDs.

All timestamps are serialized as UTC RFC 3339 values ending in `Z`. Canonical
hash bytes are UTF-8 JSON with lexicographically sorted object keys, compact
separators, ASCII escaping, preserved array order, finite decimal numbers in
plain form without trailing zeroes, JSON nulls, and one final newline. Hashes
use SHA-256. The Python reference encoder is
`src/stockguidance/interchange.py`; the fixture hash is
`5e1fa46e012effbb02606d065b410e61a045f7bfddb3b7489f59205e6854ca05`.

Money uses `BigDecimal` with scale four and `HALF_UP` rounding, matching the
Python daily-price precision. The Stage 7a writer is disposable-target-only;
no provider downloader, production writer, or model training port is included.

## Stage 8 bounded validator

Stage 8 adds the opt-in `stockguidance.validation.ValidatorMain` backend for
`raw_derived_reconciliation`. It consumes a versioned request containing the
non-secret source identity, an authoritative exported calendar, the requested
date scope and cutoff, source-manifest evidence, and history diagnostics. The
database configuration remains an explicit JSON path; credentials are never
placed in subprocess arguments or report output.

`stockguidance.validation.DownloadComparisonMain` is a separate read-only
legacy archive comparator. It accepts an explicit database JSON path and data
root, streams `market_summary` NDJSON alongside ordered raw MySQL rows, reports
exact raw differences, and has no provider-history, derived, or model-readiness
policy.

The validator performs one read-only repeatable-read transaction with bounded
date predicates and streaming JDBC result sets. It checks source keys/content,
SQL NULL values, duplicate and orphan mappings, import receipt revisions,
chronological legacy-derived values, numerical tolerances, and deterministic
repair proposals. It does not fetch providers, initialize schema, write SQL,
repair rows, or update manifests. Provider capability, feature, target, and
opening checks are outside this selected operation and are not implied by its
report.

The Python integration boundary is
`stockguidance.validation.java_backend.run_java_validator`. It passes only
`--config` and `--request` paths and accepts exit `0` for complete, `2` for an
incomplete validation, and `1` for a backend failure or failed validation. Keep
the Python validator as the default until a real database benchmark proves an
end-to-end benefit including JVM startup, transport, and memory.
