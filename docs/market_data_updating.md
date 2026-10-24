# Recoverable daily market-data updating

For independent read-only checks and explicit validation repair plans, see
[database validation](database_validation.md). The updater shares its readiness
and integrity checks; `--repair-plan validation.json` applies automatic proposals
only when explicitly invoked and rejects plans requiring manual review.

Run commands from the repository root using PyCharm's configured Python. The
supported frontend is `python -m stockguidance.cli.update_market_data`.
Installing `pyproject.toml` also exposes the `update_market_data` console command.

## Required evidence and policy

The updater discovers retained local sessions from source-bound manifests and
legacy database observations, while keeping observed dates separate from
verified source coverage. A persisted source-bound boundary and
`--history-start YYYY-MM-DD --history-evidence "..."` remain optional overrides;
a conflicting override fails and requires inspecting the existing evidence.
Neither a local minimum nor an override is treated as current provider
entitlement. Current network requests require fresh, verified provider
capability bounds.

Publication and correction policy arguments have overridable operational defaults.
For the Java-backed recurring maintenance command, see [Daily maintenance](daily_maintenance.md).
The operator chooses a
publication delay supported by their provider/account and an explicit correction
recheck policy, with provenance for both. Recent sessions are checked at the
recent interval; older sessions are checked at the archive interval. Every run
still reconciles source evidence and database records throughout the requested
scope. Rechecking an archive does download its due sessions; it is not an
assumption that old provider data can never change.

The safe operational sequence is `validate -> update/repair -> validate`.
Validation is always offline and read-only. An update may reconcile retained
local evidence before attempting network work, but a missing or corrupt archive
outside current provider capability remains unresolved and is never deleted or
marked complete. A fresh verified capability beginning in 2021 can therefore
bound fetches while preserving a retained 2010 archive.

This PowerShell template intentionally requires real, supplied values:

```powershell
& '<configured-python>' -m stockguidance.cli.update_market_data `
  --data-root '<selected-data-root>' --dbinfo-file '<selected-dbinfo-file>' `
  --end-date '<inclusive-last-requested-date>' `
  --publication-delay-minutes '<provider-supported-delay>' --publication-evidence '<publication-evidence>' `
  --recent-window-days '<recent-window>' --recent-refresh-hours '<recent-recheck>' `
  --archive-refresh-days '<archive-recheck>' --correction-evidence '<correction-policy-evidence>'
```

With omitted bounds, start/end use the discovered retained and currently
fetchable scope, ending at the latest expected published exchange session.
Explicit bounds are inclusive. Requests are clipped by the authoritative
exchange calendar and publication cutoff. If provider capability is unknown or
stale, local audit/reconciliation can extend into older retained archives, but
network work is not authorized and the output names the unresolved capability.
The API performs live reconciliation with an injected clock; it does not offer
historical downloads that pretend today's corrected response was available at a
historical decision. Use the read-only planners for historical cutoffs.

The provider adapter supports the Polygon grouped daily US-stocks endpoint with
explicit `include_otc=false`, recorded as universe `us-stocks-excluding-otc`.
This endpoint scope is not a claim of coverage for every US security.
`--adjustment adjusted|unadjusted`
defaults to `adjusted` and is validated against response metadata. Daily interval
identity is recorded. Other endpoint/universe combinations are rejected by this
adapter; another provider can implement the typed `MarketDataProvider` protocol.

## Database setup and transactions

Add `--initialize-schema` only when explicitly setting up an updater database.
It creates missing tables and binds that database to one complete source/request
identity. It never alters existing table definitions. Existing legacy rows do not
become verified merely because the database is bound: they must be reconciled
against validated source evidence. Use separate databases for different source
scopes, including different adjustment policies; the existing raw schema cannot
represent both simultaneously. Schema setup and live provider operations remain
outside offline acceptance checks. The configured Polygon adapter currently
reports unknown account capability; a run with no fresh capability performs
cached-only local work and records blocked/stale refreshes rather than guessing
an entitlement range.

The database must provide the six InnoDB tables checked by
`assert_transactional_tables`: `stockguidance_source`, `transaction_dates`,
`ticker_symbol`, `raw_stock_data`, `daily_delta_stock_data`, and
`stockguidance_imports`. Incompatible existing schemas fail for inspection rather
than undergoing automatic conversion. `START TRANSACTION` is explicit because
the retained legacy connector enables autocommit. Each session's raw replacement,
ticker/date mappings, and import receipt commit together. Each affected derived
session and its receipt commit together separately. An interruption between those
transactions remains detectable and is repaired from persisted source content.

## Evidence, recovery, and results

Under `<data-root>/market_data/<source-hash>/`, content-addressed response JSON
lives in `responses/`, session evidence in `YYYY-MM-DD.manifest.json`, and run
policy/results in `runs/`. History metadata lives in `<data-root>/history/`.
Schema version 1 manifests record the full non-secret source identity, session,
calendar/observation/actual receipt times, response content hash, normalized record
hash, exact ticker keys/count, and import/derived completion. Response envelopes
retain validated status, all supplied count fields, continuation presence, source
record timestamps, and approved normalized financial fields. Unknown fields,
provider error bodies, and continuation URLs are excluded to avoid persisting
credentials. Downloads and metadata are validated and replaced atomically.

File existence and a complete flag alone prove nothing. Every run checks content
hashes and reconciles source keys/content against actual database rows and
transactional receipts. Missing/corrupt evidence triggers a new request; intact
evidence repairs partial imports without downloading again. Rechecks can replace
previously non-null values, add newly listed tickers, and remove withdrawn source
keys. A complete response does not imply every ticker traded on every date.

Exchange closures come only from XNYS calendar evidence. Empty `OK` responses,
missing count evidence, and pagination remain unverified, never inferred closures
or completed imports. The grouped adapter does not follow continuation links.
Explicit delayed empty responses retain `not_yet_published` status. A trusted
adapter may supply `no_trading` only with authoritative request-scoped evidence;
the Polygon adapter never invents that evidence. Provider/HTTP/malformed-response
errors fail the required refresh. Later successful sessions do not hide an
earlier failure; the next run retries its gap. A session becoming due during a
run prevents premature success and can be fetched on the next run.

JSON output exposes local observed bounds, provider capability/freshness and
fetchable bounds, unresolved limits, sessions, source-response states/counts,
closures, deferred availability, stale refreshes, failures, and the resulting
`data_revision`. Exit codes are:

- `0`: every required available session in scope is reconciled; explicitly
  deferred sessions are listed separately. A calendar-only closed range can
  succeed without claiming tradable data exists.
- `2`: incomplete or unverified coverage, or no currently available trading data.
  Argument parsing also uses argparse's exit code `2`.
- `1`: required setup, provider, storage, or completion-persistence failure.

The offline cross-stage acceptance fixture in
`tests/stage10/test_cross_stage_acceptance.py` exercises this preservation and
bounded-fetch behavior with a fake provider and disposable SQLite storage.

Data revisions include source/request/interval identity, normalized raw contents,
actual stored derived contents, dependency fingerprints, and the derived spec.
Unchanged data retains its revision across a scheduled recheck. Corrections
change the revision. New dataset/prediction caches must include it along with
their feature/target/model specs and cutoff; legacy caches have no such proof
and must not be presented as compatible with the new staged pipeline.

## Migration scope

`legacy-open-to-close-v1` explicitly identifies the existing derived table
semantics. Stage 3 repairs those columns and all existing transformations rather
than silently changing their meaning. Corrections refresh the corrected session,
its predecessor's next-session outcome/transforms, and its successor's prior
ratios, including verified neighbors outside a narrower requested range. Bounded
two-session context comes from exchange chronology, not database IDs. Missing
boundary/dependency observations remain null. This is not Stage 5's close-to-close
target or new model input construction; those datasets and models still require
rebuilding in their assigned stages.

The [recommendation workflow](recommendations.md) calls this typed updater and
stops on required failure before recommendation/training. It requires explicit
close-based artifacts or a typed retraining job. `polygon_fetch.py` retains only
its credential fallback; obsolete discovery/trade-download, database-population,
and duplicate import paths are retired. All supported updater commands use
complete request-scoped reconciliation.

The existing hardcoded provider key remains `DEFAULT_API_KEY` in
`polygon_fetch.py`; the former recommendation parser's duplicate inline default
was removed in Stage 7. Its value is not reproduced here. The new command uses
`POLYGON_API_KEY` when set, otherwise the retained constant. Externalizing the
remaining defaults is future work, not a prerequisite for updating. The retained
credential module is packaged for use outside the checkout. Direct lazy database
connections load one explicit JSON configuration; temporary legacy connectors
and parent-directory discovery have been removed.

Standalone, read-only [database validation](database_validation.md) reuses
`ManifestStore.load`, `MySQLMarketDataRepository.inspect`, `SessionSnapshot`,
`SnapshotIndex`, and `reconciled`; do not invoke this write workflow for a
read-only check. See [frontends.md](frontends.md) for final command examples and
the [source retirement list](stage9_retirement.md) for the completed cleanup.
