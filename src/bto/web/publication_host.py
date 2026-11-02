"""Load private serving-host settings and count work that must drain before restart."""

import ipaddress
import json
import os
import re
import stat
import threading
from pathlib import Path
from urllib.parse import urlsplit

from psycopg import sql

from bto.settings import private_dir

_FIELDS = {
    "service",
    "python",
    "working_directory",
    "state_directory",
    "user",
    "group",
    "health_url",
    "additional_job_tables",
    "active_thread_names",
    "active_thread_prefixes",
}


def configuration_file():
    """Locate the ignored host configuration without a production path in source."""
    value = os.environ.get("BTO_REVIEW_PUBLICATION_CONFIG")
    path = Path(value).expanduser() if value else private_dir() / "review_publication.json"
    return path.absolute()


def load(*, privileged=False):
    """Require trusted configuration before selecting a privileged helper or service."""
    path = configuration_file()
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000:
                raise ValueError("Invalid private Review publication configuration file")
            if (privileged or os.geteuid() == 0) and (info.st_uid != 0 or info.st_mode & 0o022):
                raise ValueError(
                    "Privileged Review publication configuration must be root-owned and not writable by others"
                )
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Private Review publication configuration is unavailable") from error
    return validate(value)


def validate(value):
    """Validate host settings before using paths, account names or SQL identifiers."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Private Review publication configuration has missing or unknown fields")
    for field in ("python", "working_directory", "state_directory"):
        item = value[field]
        if not isinstance(item, str) or not Path(item).is_absolute() or "\x00" in item:
            raise ValueError("Review publication paths must be absolute")
    if not isinstance(value["service"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.@:-]+\.service", value["service"]
    ):
        raise ValueError("Invalid Review publication service")
    for field in ("user", "group"):
        if (
            not isinstance(value[field], str)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]*", value[field])
            or value[field] == "root"
        ):
            raise ValueError("Review building requires an unprivileged account")
    parsed = urlsplit(value["health_url"] if isinstance(value["health_url"], str) else "")
    try:
        local = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname or "").is_loopback
        )
        port = parsed.port
    except ValueError:
        local, port = False, None
    if (
        parsed.scheme != "http"
        or not local
        or port is None
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Review health check must use an explicit loopback HTTP port")
    tables = value["additional_job_tables"]
    if not isinstance(tables, list):
        raise ValueError("Review host job tables must be explicitly configured")
    for table in tables:
        if not isinstance(table, dict) or set(table) != {
            "schema",
            "table",
            "status_column",
            "active_statuses",
        }:
            raise ValueError("Invalid Review host job table")
        if any(
            not isinstance(table[key], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table[key])
            for key in ("schema", "table", "status_column")
        ):
            raise ValueError("Invalid Review host SQL identifier")
        statuses = table["active_statuses"]
        if (
            not isinstance(statuses, list)
            or not statuses
            or any(not isinstance(item, str) or not item for item in statuses)
        ):
            raise ValueError("Review host job statuses are required")
    for field in ("active_thread_names", "active_thread_prefixes"):
        if not isinstance(value[field], list) or any(
            not isinstance(item, str) or not item for item in value[field]
        ):
            raise ValueError("Review host worker selectors must be explicitly configured")
    return value


def active_jobs(conn, configuration):
    """Count public research queues and explicitly configured host work in one read."""
    terms = [
        sql.SQL(
            "(SELECT count(*) FROM job_tracking.direct_application_links WHERE status IN ('queued','searching'))"
        ),
        sql.SQL(
            "(SELECT count(*) FROM job_tracking.personnel_searches WHERE status IN ('queued','searching'))"
        ),
    ]
    parameters = []
    for table in configuration["additional_job_tables"]:
        terms.append(
            sql.SQL("(SELECT count(*) FROM {}.{} WHERE {} = ANY(%s))").format(
                sql.Identifier(table["schema"]),
                sql.Identifier(table["table"]),
                sql.Identifier(table["status_column"]),
            )
        )
        parameters.append(table["active_statuses"])
    return conn.execute(sql.SQL("SELECT ") + sql.SQL(" + ").join(terms), parameters).fetchone()[0]


def active_threads(configuration):
    """Include host workers whose queued work has not reached a database row yet."""
    names = set(configuration["active_thread_names"])
    prefixes = tuple(configuration["active_thread_prefixes"])
    return sum(
        thread.is_alive()
        and (thread.name in names or bool(prefixes and thread.name.startswith(prefixes)))
        for thread in threading.enumerate()
    )
