from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DbInfo:
    host: str = "localhost"
    database: str = "stockforecasting"
    user: Optional[str] = None
    password: Optional[str] = None
    port: int = 3306
    driver: str = "auto"  # "auto" | "mysqlconnector" | "pymysql"
    schema: Optional[str] = None  # alias of database if provided

    def with_env_defaults(self) -> "DbInfo":
        # Fall back to environment variables when fields are missing
        user = self.user or os.getenv("MYSQL_USER") or os.getenv("DB_USER")
        password = (
            self.password
            if self.password is not None
            else (os.getenv("MYSQL_PASSWORD") or os.getenv("DB_PASSWORD") or "")
        )
        port = int(os.getenv("MYSQL_PORT", self.port)) if self.port is None else self.port
        schema = self.schema or self.database
        return DbInfo(
            host=self.host or os.getenv("MYSQL_HOST", "localhost"),
            database=self.database or os.getenv("MYSQL_DATABASE", "stockforecasting"),
            user=user,
            password=password,
            port=port,
            driver=self.driver or os.getenv("MYSQL_DRIVER", "auto"),
            schema=schema,
        )


def load_dbinfo(file_path: str | os.PathLike = "dbinfo.id") -> Optional[DbInfo]:
    """Load DbInfo from the given file.

    Supported formats:
    - JSON with keys: host, database, user, password, port, driver, schema
    - Simple KEY=VALUE per line (INI-like without sections)
    Returns None if file does not exist or cannot be parsed.
    """
    p = Path(file_path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if not text:
        return None

    # Try JSON first
    try:
        obj = json.loads(text)
        return DbInfo(
            host=str(obj.get("host", "localhost")),
            database=str(obj.get("database", obj.get("db", "stockforecasting"))),
            user=obj.get("user"),
            password=obj.get("password"),
            port=int(obj.get("port", 3306)),
            driver=str(obj.get("driver", "auto")),
            schema=obj.get("schema") or obj.get("database") or obj.get("db"),
        )
    except Exception:
        pass

    # Fallback: parse KEY=VALUE lines
    data: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//") or s.startswith(";"):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            data[k.strip().lower()] = v.strip()
    if not data:
        return None

    def _get(keys: list[str], default=None):
        for k in keys:
            if k in data:
                return data[k]
        return default

    return DbInfo(
        host=_get(["host"], "localhost"),
        database=_get(["database", "db", "schema"], "stockforecasting"),
        user=_get(["user", "username"]),
        password=_get(["password", "pass", "pwd"]),
        port=int(_get(["port"], 3306)),
        driver=_get(["driver"], "auto"),
        schema=_get(["schema", "database", "db"]),
    )
