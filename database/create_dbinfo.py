from __future__ import annotations

import argparse
from pathlib import Path


TEMPLATE_JSON = """{
  "host": "localhost",
  "database": "stockforecasting",
  "user": "Redbeard",
  "password": "3YZs#z5GOJKdea",
  "port": 3306,
  "driver": "auto",
  "schema": "stockforecasting"
}
"""

TEMPLATE_KV = """# Simple KEY=VALUE format accepted by database.db_info.load_dbinfo
host=localhost
database=stockforecasting
user=YOUR_USER
password=YOUR_PASSWORD
port=3306
driver=auto
schema=stockforecasting
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Create an initial dbinfo.id file (JSON or KEY=VALUE)")
    p.add_argument("--output", default="dbinfo.id", help="Path of the file to create (default: dbinfo.id)")
    p.add_argument(
        "--format",
        choices=["json", "kv"],
        default="json",
        help="File format to write: 'json' (default) or 'kv' (KEY=VALUE)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    out = Path(args.output)

    if out.exists() and not args.force:
        print(f"Refusing to overwrite existing file: {out}. Use --force to overwrite.")
        return 2

    content = TEMPLATE_JSON if args.format == "json" else TEMPLATE_KV
    out.write_text(content, encoding="utf-8")
    print(f"Wrote initial DB config to {out} in {args.format} format.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
