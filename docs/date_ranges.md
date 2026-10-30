# Structured date ranges and dependency previews

Stage 2 adds `stockguidance.calendar.ranges`, a database-free resolver for the
accepted date-expression contract. It resolves once against an injected
exchange calendar, source identity, immutable coverage snapshot, and clock.
It never interprets database IDs as chronology and never contacts a provider.

Expressions are either an ISO `date` or an `anchor` with a signed calendar-day
or calendar-year offset. Ranges use `start`/`end`, or an `anchor` with a
nonzero signed duration. Calendar durations are inclusive after boundary
snapping; positive durations make the anchor the start and negative durations
make it the end. Exchange-session durations count exactly N sessions including
the snapped anchor. February 29 calendar-year arithmetic clamps to February
28.

Every effective session and every planned dependency must be `verified` in the
supplied source snapshot. A known `absent` session raises an out-of-coverage
failure; an unknown or incomplete session raises an unresolved-coverage
failure. `first_data`, `last_data`, and `last_completed_available` use the
snapshot, while `today` uses the injected clock in the exchange-calendar
timezone.

## Dependency planning

`DependencySpec` keeps these quantities independent:

- `model_lookback_sessions` is the number of previous feature sessions.
- `zscore_window_sessions` and `price_delta_predecessor_sessions` add rolling
  feature dependencies. The computed feature warm-up is
  `L + W - 1`, plus the predecessor for price-delta z-scores.
- `target_baseline_sessions` is reported separately from feature warm-up.
- `label_horizon_sessions` plans mature training label exits.
- `training_end_gap_sessions` is the number of exchange sessions excluded after
  the final mature training label before evaluation begins.
- `simulation_horizon_sessions` creates the default evaluation entry range when
  no explicit evaluation range is supplied. The final evaluation entry is
  retained as an open-position boundary; its next-session exit is not required
  by this planner.

`DependencyPlan` records the training and evaluation entries, warm-up and
target-baseline sessions, training/evaluation label exits, training information
cutoff, earliest fitting and simulation decision cutoffs, and the final
evaluation session.

## Read-only preview

The preview CLI consumes only a sanitized JSON configuration containing a source
identity, exchange-calendar bounds, a coverage snapshot, and date settings. It
does not open a database, initialize a schema, contact a provider, load Torch,
or load a model.

```powershell
.venv\Scripts\python.exe -m stockguidance.cli.preview_date_ranges --config examples\stage2\date-preview.json
```

The same command is available after installation as:

```powershell
preview_date_ranges --config examples\stage2\date-preview.json
```

Output is JSON with schema `stockguidance-date-preview-1`. Resolution can be
stored in a Stage 1 run through `persist_resolution(service, run_id, plan)`;
the original typed expressions remain in the run's original configuration and
the resolved evidence is written to its reserved `resolution` section.

Pinned reruns return the saved resolved object unchanged. Re-resolution is an
explicit `RerunResolutionMode.RERESOLVE` operation and records the new source
snapshot/revision in the child run selected by the caller.
