# Read-only database validation

For the recommended update/validation/training/simulation order, see the
[operations guide](operations_guide.md). Shared Stage 1 source-scoped discovery
and the Stage 2 read-only validator are implemented in
`market_data/discovery.py` and `cli/validation_setup.py`. The opt-in Java
validator is documented in [java_performance.md](java_performance.md); Python
remains the default. Stage 10 documentation and acceptance checks are recorded
in [Luna implementation prompts](luna_maintenance_prompts.md).

Run validation independently of downloading, importing, training, or recommending:

```powershell
& '<configured-python>' -m stockguidance.cli.validate_database --help
```

The `validate_database` console entry point is declared in `pyproject.toml` and becomes available after refreshing the editable installation. Use PyCharm's configured interpreter. Python has been repaired; historical base-installation workarounds in `REFACTORING_PROMPTS.md` are no longer current setup guidance. See the operations guide for the verified interpreter and checkout invocation.

The command resolves default `data` and `dbinfo.id` paths from the checkout when
launched from the IDE or another working directory. Explicit paths remain
unchanged. With no history override, the earliest observed database or local
manifest date is used as a validation audit floor. The report keeps that floor
separate from the provider's supported-history boundary; a local minimum does
not prove that the provider has no earlier data.

`--history-evidence` is provenance for an explicit `--history-start`, such as an
account entitlement statement or a documented source-history boundary. It is
not a description of the rows being checked. `--publication-evidence` describes
the evidence for the expected delay between an exchange close and a provider
daily response. It is optional for a local audit; when omitted, database and
source rows are still inspected, but publication/completeness remains
unverified.

Use the same data root, database configuration and adjustment as the updater.
Publication evidence is required when a delay is supplied; otherwise validation
reports an unresolved publication limit while still inspecting local rows and
source evidence. History/date arguments are optional when local retained
evidence establishes a scope. Replace the bracketed values with inspected
evidence when using explicit overrides:

```powershell
& '<configured-python>' -m stockguidance.cli.validate_database `
  --data-root 'data' --dbinfo-file 'dbinfo.id' --adjustment adjusted `
  --history-start '<first-supported-session>' --history-evidence '<boundary provenance>' `
  --start-date '<requested-first-date>' --end-date '<requested-last-date>' `
  --publication-delay-minutes '<documented delay>' --publication-evidence '<publication provenance>'
```

An explicit boundary is used in memory and is **not saved** by validation. When
source-specific metadata exists under `<data-root>/history/`, or local manifests
or legacy rows establish an observed scope, omit `--history-start` and
`--history-evidence`. A conflicting boundary fails. Database minima and local
minimum dates are reported as observations, never as proof that no earlier
history is expected. Cached discovery capability metadata may be used, but
validation never contacts a live provider.

### Audit optional z-score materializations

Add `--zscore-windows 20,60` to the read-only validation command to audit both
the `price` and `price_delta` families for those windows. The option uses the
same distinct integer, minimum-2 parsing rules as `update_zscores`. The audit
loads bounded exchange-session warm-up context, verifies selected series
definitions, schema, row keys, statuses, completion metadata, dependency and
receipt cutoff, then independently recomputes available scores from verified
local inputs. Stored ready scores use float32-aware comparison (`rtol=1e-5`,
`atol=1e-6`).

This option is opt-in: ordinary validation does not require z-score tables. The
audit is Python-only and read-only; it never initializes tables, downloads data,
repairs raw or delta rows, or persists metadata. Missing, stale, invalid, or
unverified materializations produce `zscore_materialization` findings with
family, window, session, ticker, expected status/score, stored status/score,
and manual-review guidance to rerun the explicit z-score updater. A z-score
finding is never an authorization for an automatic raw/delta repair. The Java
validator retains its existing raw/derived comparison scope and does not claim
to perform this z-score audit.

Add `--as-of '<timezone-aware ISO timestamp>'` for a historical cutoff no later than the current/injected clock. Queries never cross supported history or include unfinished/unpublished sessions. Importing old bars today does not prove availability at a historical decision. Validation does not reconstruct earlier revisions of corrected data.

Add `--format json` for machine-readable output. Redirect stdout to save a report; validation itself does not persist one:

```powershell
& '<configured-python>' -m stockguidance.cli.validate_database <same-arguments> --format json > validation.json
```

## Results and exit codes

| Exit | Meaning |
|---|---|
| 0 | All required available sessions in the declared request are verified; expected absences and deferred observations remain explicit. A range containing only calendar closures can succeed. |
| 2 | Validation completed with missing, invalid, incomplete, or unverified data, or with no available trading session. Argument parsing also uses 2. |
| 1 | Validation could not run or a required schema/row inspection failed. Exception text is sanitized. |

JSON includes non-secret source identity, requested bounds/cutoff, local observed
bounds, verified retained coverage and holes, cached provider capability and
freshness, effective scope, latest expected published session, unresolved limits,
inspected sessions including context, source keys/counts, physical database
counts, successfully decoded keys, receipt times, provider count evidence,
checks assessed, typed findings, and a versioned repair plan. Invalid/duplicate
rows remain in physical counts even when excluded from validated snapshots.
Findings identify dates, tickers, and mapping IDs when available. Deferred
sessions do not claim price/derived checks or expose future record keys.

| Finding category | Meaning |
|---|---|
| `missing` | Required session, exact source key, derived row, or mature in-scope outcome is absent. |
| `invalid` | Duplicate/orphan mappings, invalid prices/counts, source-content drift, invalid receipts, or incorrect derived values. |
| `incomplete` | Interrupted imports, stale derived receipts/dependencies, or unfinished manifest markers. |
| `unverified` | Missing/corrupt source evidence, insufficient provider evidence, unknown publication, or incompatible schema/source binding. |
| `expected_absence` | Predecessor history at the supported boundary or ticker absence in an adjacent response; unavailable values stay null. |
| `not_yet_available` | Observations, receipts, or next-session labels were unavailable at the cutoff. Overdue provider-delayed responses additionally remain unverified and prevent readiness. |
| `not_assessed` | Model features without an explicit universe/lookback, or dependencies outside requested coverage without verified observations. |
| `failed` | Required schema/row layout could not be inspected. |

Checks reconcile exact response keys and content, not plausible counts or maximum IDs. Tickers need not appear every session. A complete declared request cannot establish independent market-wide completeness or explain a provider omission. No synthetic price, inferred listing lifetime, or zero-return label is introduced.

Derived checks explicitly use **`legacy-open-to-close-v1`**. They recompute existing features and mature transformations from available chronological dependencies, checking values as well as receipts. Separate Stage 5 checks compute raw `close(T+1) / close(T) - 1` from verified raw prices; legacy columns are never relabeled as these targets. Null final labels are expected while exits are immature. Mature dependencies inside requested coverage must exist. Beyond the requested end, verified context is used when present; otherwise the report says the outcome was not assessed.

Add `--ticker-ids '17,3' --lookback-sessions 3` to validate the exact ordered previous-close model inputs. Both arguments are required together. The default feature is each historical close divided by its ticker's first window close, minus one, with oldest-session/ticker/feature flattening. The feature decision is one microsecond before each entry close; actual receipt availability still applies. Without these arguments, model feature readiness is explicitly not assessed. JSON/text reports include numerical sample states, exact dates/tickers, targets, feature windows, and data identities. See `docs/close_samples.md` for the shared API, transformations, and rebuild requirements.

Queries include at most two available neighboring sessions on each side for legacy dependency identity. Explicit feature validation additionally loads its full historical lookback. Dated queries remain bounded by history and cutoff. Records with no date mapping cannot be assigned a historical date; those unassigned orphans are checked across storage and identified by mapping IDs. Damaged context is reported explicitly.

## Explicit repairs

Validation issues read queries and rolls back its transaction. It never invokes schema initialization, provider fetch, import, derived replacement, history save, or manifest completion. There is no repair/delete/schema flag. Missing/incompatible schemas require review before explicit updater setup; validation never converts engines or deletes records.

Apply automatic proposals by passing saved validation JSON (or its standalone
`repair_plan` object) to the updater explicitly. Plans with schema version 2 may
contain `blocked_out_of_access` items with a reason; those are manual-review
records and are rejected by the updater until an authorized, evidenced scope is
provided:

```powershell
& '<configured-python>' -m stockguidance.cli.update_market_data `
  --data-root 'data' --dbinfo-file 'dbinfo.id' --adjustment adjusted `
  --repair-plan 'validation.json' `
  --publication-delay-minutes '<documented delay>' --publication-evidence '<publication provenance>' `
  --recent-window-days '<correction window>' --recent-refresh-hours '<recent interval>' `
  --archive-refresh-days '<archive interval>' --correction-evidence '<correction provenance>'
```

The updater discovers a retained scope from source manifests and eligible local
database observations when history bounds are omitted. Plan bounds become the
update bounds unless supplied explicitly; supplied bounds must match. Source,
adjustment, interval, capability and cutoff are validated before provider or
database side effects. Plans containing manual schema, orphan/duplicate
integrity, outside-scope context, policy-review items, or refreshes outside
fresh provider capability are rejected until reviewed. Automatic actions are
`refresh_source`, `reimport`, `rebuild_legacy_derived`, and
`complete_manifest`. Refresh proposals force provider verification even before
the ordinary correction interval expires. Recovery remains idempotent and
reconciles the entire declared scope; plans contain no arbitrary SQL.

Updater and validator share source/raw/derived assessment and database integrity reading. Forged derived-content receipts cannot hide incorrect mature values, and unassigned orphans cannot produce a successful update. Re-run validation after explicit repairs.

## Legacy download comparison

The slower `compare_database_downloads` command compares raw database rows with
the retained legacy archive under `data/market_summary/YYYY/*.ndjson`. It is
implemented by the Java streaming backend so file parsing and one ordered JDBC
result stream run with bounded memory inside a repeatable-read transaction. It
does not fetch, repair, initialize schema, or write metadata. It checks dates,
ticker keys, timestamps, OHLCV/VWAP/transaction values, duplicate records, and
malformed files. Derived columns and model readiness remain Python validation
checks.

Build the Java module first, then provide the compiled classes and dependencies
on the classpath:

```powershell
Set-Location StockGuidance\java
mvn -o package
Set-Location ..\..
& $pythonExe -m stockguidance.cli.compare_database_downloads `
  --data-root data --dbinfo-file dbinfo.id --adjustment adjusted `
  --java-classpath 'StockGuidance\java\target\classes;StockGuidance\java\target\dependency\*;StockGuidance\java\lib\mysql-connector-j-9.6.0.jar' `
  --format json > output/download-comparison.json
```

Exit `0` means exact raw equality, `2` means differences or invalid/incomplete
download evidence, and `1` means setup or database inspection failure.

The retained hardcoded key is `DEFAULT_API_KEY` in `polygon_fetch.py`; it should eventually become external configuration. Validation does not load it or contact the provider. Reports and repair plans contain no credentials.

The implemented entry-opening workflow adds `--signal-mode entry_open` with explicit `--ticker-ids` and
`--lookback-sessions`. It audits read-only opening receipts, including the current
unfinished daily session, without querying that session's final daily bar.
`entry_open_observations` findings distinguish missing/unverified/late openings.
`capture_opening_evidence` proposals are not automatic daily repairs: original
historical receipt times cannot be manufactured by a download today. See
[entry_open.md](entry_open.md) for the exact provider/method and missing policy.
