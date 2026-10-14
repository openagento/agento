from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseConfig:
    """Framework-level MySQL connection config.

    Attribute names match the mysql_* convention used by get_connection().
    """

    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: str = ""

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        """Build from env vars only (MYSQL_HOST, MYSQL_PORT, etc.)."""
        return cls(
            mysql_host=os.environ.get("MYSQL_HOST", "mysql"),
            mysql_port=int(os.environ.get("MYSQL_PORT", "3306")),
            mysql_database=os.environ.get("MYSQL_DATABASE", "cron_agent"),
            mysql_user=os.environ.get("MYSQL_USER", "cron_agent"),
            mysql_password=os.environ.get("MYSQL_PASSWORD", ""),
        )
