"""
Simple MySQL connectivity helper for the local instance.

Defaults:
- host: mysql95
- database/schema: stockforecasting
- port: 3306
- credentials from environment variables if not provided via CLI:
    - MYSQL_USER
    - MYSQL_PASSWORD

Drivers supported (auto-detected by default):
- mysql-connector-python (module: mysql.connector)
- PyMySQL (module: pymysql)

Usage examples:
  python db_connect.py                      # use env creds, default host/schema
  python db_connect.py --user scott --password tiger
  python db_connect.py --driver pymysql
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Tuple


def _try_import_drivers(prefer: str = "auto") -> Tuple[str, object]:
    """Return (driver_name, module) for an available driver.

    prefer: "auto" | "mysqlconnector" | "pymysql"
    """
    prefer = prefer.lower()

    if prefer in ("mysqlconnector", "mysql-connector", "mysql.connector"):
        try:
            import mysql.connector as mc  # type: ignore
            return ("mysqlconnector", mc)
        except Exception as e:
            raise RuntimeError("Requested driver 'mysqlconnector' is not available: " + str(e))
    if prefer in ("pymysql",):
        try:
            import pymysql as pm  # type: ignore
            return ("pymysql", pm)
        except Exception as e:
            raise RuntimeError("Requested driver 'pymysql' is not available: " + str(e))

    # auto-detect: try mysql-connector, then PyMySQL
    try:
        import mysql.connector as mc  # type: ignore
        return ("mysqlconnector", mc)
    except Exception:
        pass
    try:
        import pymysql as pm  # type: ignore
        return ("pymysql", pm)
    except Exception:
        pass

    raise RuntimeError(
        "No MySQL driver found. Please install either 'mysql-connector-python' or 'pymysql'."
    )


def get_connection(
    host: str = "mysql95",
    database: str = "stockforecasting",
    user: Optional[str] = None,
    password: Optional[str] = None,
    port: int = 3306,
    driver: str = "pymysql",
):
    """Create and return a live MySQL connection using the requested driver.

    Returns a connection object compatible with PEP 249 DB API.
    """
    drv_name, drv_mod = _try_import_drivers(driver)

    user = user or os.getenv("MYSQL_USER") or os.getenv("DB_USER")
    password = password or os.getenv("MYSQL_PASSWORD") or os.getenv("DB_PASSWORD")

    if not user:
        raise ValueError("MySQL username not provided. Use --user or set MYSQL_USER.")
    if password is None:
        # allow empty password if explicitly provided via env or CLI as empty string
        password = os.getenv("MYSQL_PASSWORD", "")

    if drv_name == "mysqlconnector":
        # mysql-connector-python
        conn = drv_mod.connect(
            host=host,
            database=database,
            user=user,
            password=password,
            port=port,
            autocommit=True,
#	        compress=True,
	        buffered=True,
        )
        return conn
    elif drv_name == "pymysql":
        conn = drv_mod.connect(
            host=host,
            db=database,
            user=user,
            password=password,
            port=port,
            autocommit=True,
            charset="utf8mb4",
        )
        return conn
    else:
        # Should not happen
        raise RuntimeError(f"Unsupported driver selected: {drv_name}")


def _print_connection_info(conn, cursor):
    try:
        cursor.execute("SELECT VERSION()")
        version = cursor.fetchone()
        print(f"MySQL version: {version[0] if version else 'unknown'}")
    except Exception as e:
        print(f"Warning: could not fetch server version: {e}")

    try:
        cursor.execute("SELECT DATABASE()")
        db = cursor.fetchone()
        print(f"Current database: {db[0] if db else 'unknown'}")
    except Exception as e:
        print(f"Warning: could not fetch current database: {e}")

    try:
        cursor.execute(
            """
            SELECT TABLE_NAME
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            ORDER BY TABLE_NAME
            LIMIT 20
            """
        )
        rows = cursor.fetchall()
        tables = [r[0] for r in rows] if rows else []
        if tables:
            print("Sample tables (up to 20):")
            for t in tables:
                print(f" - {t}")
        else:
            print("No tables found in this schema (or insufficient privileges).")
    except Exception as e:
        print(f"Warning: could not list tables: {e}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Test connection to local MySQL (mysql95/stockforecasting)")
    parser.add_argument("--host", default="mysql95", help="MySQL host name or IP (default: mysql95)")
    parser.add_argument("--db", "--database", dest="database", default="stockforecasting", help="Database/schema name (default: stockforecasting)")
    parser.add_argument("--user", default=None, help="Username (default: from env MYSQL_USER)")
    parser.add_argument("--password", default=None, help="Password (default: from env MYSQL_PASSWORD)")
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", 3306)), help="Port (default: 3306 or env MYSQL_PORT)")
    parser.add_argument(
        "--driver",
        choices=["auto", "mysqlconnector", "pymysql"],
        default="auto",
        help="Driver to use (default: auto)",
    )

    args = parser.parse_args(argv)

    try:
        conn = get_connection(
            host=args.host,
            database=args.database,
            user=args.user,
            password=args.password,
            port=args.port,
            driver=args.driver,
        )
    except Exception as e:
        print(f"Failed to connect: {e}")
        return 2

    print("Connection established successfully.")

    try:
        cur = conn.cursor()
        _print_connection_info(conn, cur)
        # Simple probe
        cur.execute("SELECT 1")
        one = cur.fetchone()
        print(f"Test query SELECT 1 -> {one[0] if one else 'no result'}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
