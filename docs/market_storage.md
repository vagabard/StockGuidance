# Canonical daily market-data storage

Daily downloads belong in `data/market_summary/YYYY/YYYY-MM-DD.ndjson`.
`market_summary` retains its existing underscore spelling. The records use
`T,o,h,l,c,v,vw,n,t`; decimal strings and JSON numbers are accepted without a
binary floating-point conversion. Bad source rows and exchange test securities
remain in the raw archive. They are classified separately from trading inputs.

New operational downloads are unadjusted. Existing files are not assumed to be
unadjusted: earlier adjusted downloads can use different historical split bases.
An adjusted database must be migrated before enabling canonical update commands.
Read-only legacy validation remains available during this transition.

## Normal operations

Both `update_market_data` and `maintain_daily` check committed database receipts
first. Completed raw dates with no partial import or pending replacement are
reused without opening their archives or contacting the provider. Missing
derived calculations are rebuilt from database rows, without downloading raw
data again. Missing or partial raw loads use a validated canonical archive, then
fetch that particular date if necessary. Unknown account-wide capability does
not prohibit a specific ordinary update request; the response must still pass
the complete-response, adjustment, count, timestamp and pagination checks.

Scheduled correction intervals no longer trigger routine downloads. To request
a correction explicitly, supply `--refresh-source --start-date YYYY-MM-DD
--end-date YYYY-MM-DD`. `update_market_data --force-verify` explicitly checks
stored content and repairs missing/corrupt raw input. Validation remains an
independent read-only audit, including detection of damage to completed dates.

Both update coordinators use the same database advisory lock. Archive content
and download availability are cataloged in `stockguidance_archive_files`.
`stockguidance_daily_source_files.download_pending` prevents reuse of a stale
receipt while its replacement awaits import. A checksum distinguishes an old
file from the intended replacement after interruption. Raw rows and completion
receipts commit together; partial markers and pending-import state are cleared
only after a matching raw receipt exists. File modification time is not used as
the historical availability of a canonical download.

Explicit schema initialization creates the catalog table. It is never created
by a read-only reader. Database backups must include the catalog, import,
pending-import, source-configuration and quality tables. Run reports now live
under `<data-root-parent>/output/market-data/runs`, outside the data archive.

## Inventory and conflicts

Run from the repository root:

```powershell
.venv/Scripts/python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --output output/storage-consolidation
```

This reads all three existing locations without changing them. The output is
`audit.json`, `inventory.jsonl`, and compressed `conflicts.jsonl.gz`. The conflict
printout includes date, ticker, changed field, both values, source paths,
exchange, asset type, test status and classification evidence where known.
Numeric spelling and record ordering are ignored. Unknown classifications are
reported as null rather than guessed. Supply `--security-metadata FILE` to enrich
the report with independently verified ticker classifications.

Conflict rules:

- Equivalent verified unadjusted copies retain the canonical version.
- Different complete unadjusted copies use the later evidenced provider response.
- Adjusted or unknown-provenance copies require unadjusted verification or a new
  `adjusted=false` download. Price ratios and filesystem timestamps are not proof.
- Ticker additions/removals are reported independently of changed values.
- Missing provider access leaves a date unresolved; it never authorizes deletion
  or labeling adjusted prices as unadjusted.

## Migration and cutover

1. Back up the existing database and archives. Create a separate replacement
   database and an explicit data-configuration JSON with all raw adjustment
   flags false. Keep the old database configuration unchanged.
2. Initialize the replacement schema using the normal explicit schema setup.
   Daily maintenance also requires its existing migration-completion and z-score
   tables; use the documented migration setup for a complete replacement.
3. Review the inventory and verify sources. A decisions JSON contains one entry
   per date, for example:

```json
[
  {
    "date": "2024-07-30",
    "path": "C:/verified-unadjusted/2024-07-30.ndjson",
    "sha256": "<SHA-256 of that file>",
    "adjustment": "unadjusted",
    "complete": true,
    "available_at": "2026-10-25T18:00:00+00:00",
    "evidence": "<actual verification or provider download evidence>"
  }
]
```

`available_at` must reflect known receipt/adoption evidence, not an invented
historical receipt. If only present-day adoption is evidenced, use the adoption
time. Dry-run verification performs no database writes:

```powershell
.venv/Scripts/python.exe -m stockguidance.cli.consolidate_market_data `
  --dbinfo-file replacement-dbinfo.id --decisions verified-sources.json
```

Add `--apply` to publish the selected sources and catalog their evidence. Every
input is checked against its approved checksum before publication. Migration
uses bounded per-day memory and resumes already-published matching dates.
Displaced canonical bytes are retained under `data/archive-backup/<hash>/`.

4. Load the canonical files into the replacement database with explicitly
   bounded updates. Rebuild derived data and z-scores, validate independently,
   and retrain artifacts whose adjustment identity changed. Switching a source
   flag on the existing database is not a migration. Unadjusted rolling features
   spanning corporate actions still need an explicit corporate-action policy;
   archive consolidation does not silently alter feature definitions.
5. Switch the selected database configuration only after the replacement passes
   the independent audit. Until then, retain the old database and all source
   versions. Use the old database/configuration to roll back a cutover; restore
   displaced archive bytes from their hash-addressed backups if necessary.
6. After canonical coverage is verified for every legacy date, review
   `consolidate_market_data --dbinfo-file replacement-dbinfo.id --retire-legacy`.
   Add `--apply` to move legacy source directories into `archive-backup/retired`.
   No files are deleted; `restore.json` records original and backup locations.
   The independent `market_data/openings` directory is unaffected.

Export provenance alongside database backups with `--export-catalog
output/storage-consolidation/catalog.json --dbinfo-file replacement-dbinfo.id`.
This is a supplement to a database backup, not a replacement for import receipts.

## Security classification

Verified test securities are unavailable to close-based training, eligibility,
recommendation and simulation readers. A small cited baseline covers known
exchange test symbols; names containing `TEST` are not automatically excluded.
`market_summary/securities.json` can supply additional verified classifications,
keyed by ticker, with `exchange`, `asset_type`, boolean `is_test`,
`effective_from`, optional `effective_to`, and `evidence`. Unknown symbols remain
unknown. Classification affects consumers, not retained raw provider records.

## Benchmarks

The exact operational reader comparison reuses the existing archive study:

```powershell
.venv/Scripts/python.exe -m stockguidance.cli.archive_study --canonical `
  --output output/storage-consolidation/benchmark/readers
.venv/Scripts/python.exe -m stockguidance.cli.benchmark_market_storage
```

The first command compares Python processes and the Java Fory reader at one and
three preparation workers, using 24 stratified dates and the largest file.
Parity covers file hashes, exact normalized record identities, record counts,
and retained bad values. It does not use the older four-decimal study decoder.
The publication benchmark alternates direct NDJSON encoding and the legacy
response-envelope path adapted to emit identical NDJSON. It includes fsync and
atomic replacement using captured responses, with no HTTP latency or production
archive writes.

The existing Stage 9a raw benchmark compares Python, Java and SQL publication
in distinct disposable MySQL schemas. Run it with `--run-stage9-raw` and output
under `output/storage-consolidation/benchmark/database`; it includes matching
adoption, partial/different repair and unchanged reruns. Its disposable schemas
are reported in the result and retained for inspection. It never writes the
production schema.

Each comparison records a first invocation and three warm repetitions. First
invocation does not imply a cold operating-system cache. Memory is reported only
when monitoring is available. Run performance trials on an otherwise idle
machine; avoid simultaneous benchmark or audit commands. Select the fastest
correctness-qualified end-to-end implementation, retaining the simpler existing
implementation when within 5%. Normal maintenance uses the Java raw reader and
transactional writer, with one database writer; Python remains available for
independent checking and the typed update API.

## Consolidation and cleanup commands

Run these commands from the repository root. Back up the database and `data`
before applying cleanup: verified redundant files are **permanently deleted**,
not sent to the Recycle Bin. No command below runs automatically.

Set `$db` to the database configuration for the verified **unadjusted** destination.
`replacement-dbinfo.id` is an example, not a file created by the script. Do not
change an adjusted database's configuration flags to bypass this requirement;
complete the replacement-database setup described above first. Preview reads
the archive catalog but makes no database or archive changes. Reports and the
journal are written outside `data` even during previews.

```powershell
$db = 'replacement-dbinfo.id'
```

### 1. Preview consolidation and unnecessary-file cleanup

```powershell
.\.venv\Scripts\python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --dbinfo-file $db `
  --output output/storage-consolidation --consolidate
```

Review `output/storage-consolidation/cleanup-report.json` and
`cleanup-journal.jsonl`. Counts describe proposed actions in preview mode.
The final JSON output includes `instructions` and copyable PowerShell
`next_commands`, with the supplied paths and database configuration.
Progress messages go to stderr while the final machine-readable JSON goes to
stdout, so redirecting the final report does not hide progress.

### 2. Apply consolidation and delete unnecessary copies

```powershell
.\.venv\Scripts\python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --dbinfo-file $db `
  --output output/storage-consolidation --consolidate --apply
```

Complete, evidenced unadjusted responses are published in
`data/market_summary/YYYY/YYYY-MM-DD.ndjson`. Equivalent copies are deleted
only after verified canonical coverage exists. Differing complete unadjusted
responses use the later evidenced availability; equal-time disagreements remain
unresolved. An existing differing canonical file with unknown provenance is
not overwritten. Conversion that would change numeric source values is refused.

Unresolved legacy files move into `data/market_summary_conflicts`, mirroring
their original paths. Their manifests move with them. Malformed files,
orphaned responses, and shared response references are retained for review,
not silently discarded. Raw test-security rows are preserved. Quarantine moves
do not reclaim disk space; verified deletion does. Displaced canonical files
remain in the existing `archive-backup` safety store.

Cleanup covers daily NDJSON files under `daily-maintenance/*`, and manifests
and response JSON under `market_data/*`. Unrecognized files and unrelated
directories (`history`, `java`, `market_data/openings`, backups) remain untouched.
Only empty ancestor directories of processed files are removed.

### 3. Resolve conflicts needing explicit evidence

Review the unresolved entries and obtain a complete verified unadjusted source.
Create a decisions JSON using the format in “Migration and cutover.” To approve
removal of a differing old source, add `supersedes` to its date's decision:

```json
"supersedes": [
  {
    "sha256": "<SHA-256 of the old response JSON or daily NDJSON file>",
    "evidence": "<specific verification establishing why this source is superseded>"
  }
]
```

This is a field inside a decision object, not a standalone decisions file.
Use the actual old file checksum from the journal; a manifest's `content_id`
identifies its response JSON. Approval applies only to that exact checksum and
date while the selected canonical replacement remains verified. Approving a
date alone is not permission to delete all its versions. Preserve evidence
even for adjusted sources; price ratios and modification times do not suffice.
Orphans without an established date/source and shared references require manual
provenance repair or independent review; these flags never force-delete them.

Preview publication:

```powershell
.\.venv\Scripts\python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --dbinfo-file $db `
  --output output/storage-consolidation --decisions verified-sources.json
```

Repeat with `--apply` after reviewing the result. Publication retains the
approved superseded-source checksums and evidence in the archive catalog.
It does not itself delete quarantined files or load raw database rows; use the
normal bounded updater for database import as described above.

### 4. Preview and apply resolved-conflict cleanup

```powershell
.\.venv\Scripts\python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --dbinfo-file $db `
  --output output/storage-consolidation --cleanup-conflicts
```

Repeat with `--apply` to delete eligible quarantined files. Files are eligible
when equivalent verified canonical data exists, a later complete unadjusted
source resolves the conflict, or an explicit evidenced replacement approves
that source checksum. Other conflicts remain protected; there is no force flag
for destroying unresolved unique data.

To combine consolidation and resolved-conflict cleanup:

```powershell
.\.venv\Scripts\python.exe -m stockguidance.cli.consolidate_market_data `
  --data-root data --dbinfo-file $db `
  --output output/storage-consolidation `
  --consolidate --cleanup-conflicts --apply
```

Omit `--apply` to preview the combined operation. Do not combine cleanup flags
with `--decisions`, `--retire-legacy`, or `--export-catalog`; run those operations
separately. Keep catalog backups alongside the journal and decision files.

### Completion, interruption, and exit status

- Exit `0`: the selected operation completed with no unresolved cleanup entries.
- Exit `2`: processing completed, but unresolved files remain; consult the report.
- Exit `1`: configuration, validation, lock, path, or filesystem failure. Check
  the printed error and journal; already completed per-file operations remain.

After interruption, rerun the **same command with the same `--output` directory**.
The append-only journal records prepared/completed actions, exact checksums,
manifest provenance and response metadata. Matching canonical files and already
processed sources are reused; matching journal evidence permits completion of
a dangling manifest deletion. An interrupted quarantine can require a subsequent
`--cleanup-conflicts` pass once all associated files have moved. Preserve the
journal: deleting it does not restore deleted files and can prevent automatic
resolution of incomplete cleanup actions. A quarantine destination collision
stops processing rather than overwriting a retained version. Resolve the
collision after reviewing both files, then rerun.

Apply operations acquire the same update lock as normal writers. Source
checksums are rechecked immediately before deletion or movement, verified
canonical coverage is rechecked before deletion, and links/path escapes are
rejected. A preview is not authorization to delete a file that changes later.
