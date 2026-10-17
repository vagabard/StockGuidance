# StockGuidance Java foundation

Stage 7 establishes the current Java contract under `src/main/java`. The
existing files under `src/stockguidance` are historical prototypes and are not
compiled by this build. Their model readers and CSV simulator do not implement
the current Python contracts and must not be treated as compatible inputs.

## Build and test

The build supports Java 21 bytecode. The acceptance run used OpenJDK 26.0.2.1
and Maven 3.9.9 on Windows 11. From `C:\Projects\StockGuidance`:

```powershell
Set-Location StockGuidance\java
mvn -o test
mvn -o -DskipTests package
```

The offline build pins Jackson Databind 2.15.2, JUnit Jupiter 5.13.4,
Maven Compiler 3.13.0, and Maven Surefire 3.5.3. MySQL Connector/J 9.6.0 is
the bundled jar in `lib`; it is a local system dependency rather than a
downloaded Maven artifact. A dependent project must provide its own JDBC
dependency instead of relying on this system-scoped path.

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
Python daily-price precision. No provider downloader, live writer, or model
training port is included in Stage 7.

## Stage 8 bounded validator

Stage 8 adds the opt-in `stockguidance.validation.ValidatorMain` backend for
`raw_derived_reconciliation`. It consumes a versioned request containing the
non-secret source identity, an authoritative exported calendar, the requested
date scope and cutoff, source-manifest evidence, and history diagnostics. The
database configuration remains an explicit JSON path; credentials are never
placed in subprocess arguments or report output.

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
