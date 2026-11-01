"""Open PostgreSQL connections and prevent competing sorting operations.

Connections use the configured libpq PG* environment variables. The sorting
ownership lock is released when the operation exits or its connection closes.
"""

from contextlib import contextmanager

import psycopg

OWNER_LOCK = 72403104


class SortingBusy(RuntimeError):
    """The sorting advisory lock is owned by another runner."""


def connect(timeout_s=10):
    """Open an autocommit connection using the configured PostgreSQL credentials."""
    return psycopg.connect(autocommit=True, connect_timeout=timeout_s)


@contextmanager
def sorting_owner(conn):
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (OWNER_LOCK,)).fetchone()[0]:
        raise SortingBusy("Another job-sorting owner is active")
    try:
        yield conn
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (OWNER_LOCK,))
