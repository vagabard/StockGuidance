# Data configuration

The active data configuration is a separate JSON document referenced by
`data_configuration_file` in `dbinfo.id`. The example configuration is
`examples/data_configuration.json`.

`source_family` is canonicalized before it is stored. The `polygon` and
`massive` aliases currently resolve to the same `polygon_massive` family.
`endpoint` is retained for provenance and endpoint verification, but it does
not change the semantic configuration key. `market` is the broad market scope;
security records retain their exchange metadata and stable identifiers.

`calendar_id` is the fallback `exchange_calendars` calendar for broad jobs that
do not specify ticker IDs. Explicit ticker queries resolve the calendar from
the selected rows' `ticker_symbol.exchange` values in the database; mixed
database exchanges are rejected because one query cannot use multiple session
calendars. The setting must be a supported calendar name such as `XNYS` or
`XNAS`, and existing configurations that omit it continue to use `XNYS`.
It does not change the semantic data-configuration key.

The raw-price basis is described independently by
`raw_price_split_adjusted`, `raw_price_dividend_adjusted`, and the optional
`raw_price_vendor_defined_adjustment` value.

A source-family change registers a verification run. The default `block` mode
prevents normal database use until 100 stored-overlap session dates have been
compared. `gate_writes` permits reads while blocking writes, and `advisory`
permits both. Endpoint changes register a weaker 50-pull verification and are
advisory by default.
