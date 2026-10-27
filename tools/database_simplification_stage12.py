"""MySQL Stage 12 rehearsal entrypoint.

Use --help for required credential, disposable-prefix, and output arguments.
The old SQLite rehearsal did not test MySQL transaction or capacity behavior.
"""

from database_simplification_stage12_mysql import main


if __name__ == "__main__":
	raise SystemExit(main())
