"""PostgreSQL connection mechanics for the operational store.

Called by:
    storage.postgres.write, __main__ (preflight)

psycopg reads the standard libpq PG* environment variables (PGHOST, PGPORT,
PGDATABASE, PGUSER, PGPASSWORD), which is exactly how both environments are
configured: .env locally (through an SSH tunnel) and /etc/bto/env on the box.
Nothing here re-states credentials.

Main function:
    connect() -> psycopg connection (autocommit)
"""

import psycopg


def connect(timeout_s=10):
    """One autocommit connection. Callers keep it short-lived: collection writes
    a handful of cost rows per night, not a stream."""
    return psycopg.connect(autocommit=True, connect_timeout=timeout_s)
