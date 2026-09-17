"""Write collection Apify cost records to PostgreSQL (`costs` schema).

Called by:
    fetch_job_listings.run (after each Apify Actor run)
    __main__ preflight (ensure_cost_tables)

The table shapes are the PostgreSQL data dictionary's contract. One row per
Actor run, UPSERTed on apify_run_id so a refreshed cost overwrites rather
than duplicating. `cost_usd` is always Apify's reported `usageTotalUsd` —
spend is never inferred from result counts.

Cost writes FAIL SOFT by contract: the caller catches exceptions from here,
logs them, and reports the accounting gap — a lost cost row is never a
reason to fail the collection work that produced it.

Main functions:
    record_indeed_cost(record)     one costs.apify_indeed row
    record_linkedin_cost(record)   one costs.apify_linkedin row
    ensure_cost_tables()           CREATE IF NOT EXISTS (run at deploy time)
"""

import logging

from .connection import connect

log = logging.getLogger(__name__)

# Data-dictionary DDL. Executed by `python -m bto preflight` during
# deployment, not by the nightly run.
COST_TABLES_DDL = """
CREATE SCHEMA IF NOT EXISTS costs;

CREATE TABLE IF NOT EXISTS costs.apify_indeed (
    apify_run_id  TEXT PRIMARY KEY,
    bto_run_id    TEXT NOT NULL,
    market        TEXT NOT NULL,
    search_term   TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,
    result_count  INTEGER,
    cost_usd      NUMERIC(12,6),
    recorded_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS costs.apify_linkedin (
    apify_run_id  TEXT PRIMARY KEY,
    bto_run_id    TEXT NOT NULL,
    market        TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,
    result_count  INTEGER,
    cost_usd      NUMERIC(12,6),
    recorded_at   TIMESTAMPTZ NOT NULL
);
"""


def ensure_cost_tables():
    with connect() as conn:
        conn.execute(COST_TABLES_DDL)
    log.info("costs.apify_indeed and costs.apify_linkedin ensured")


def record_indeed_cost(record):
    """UPSERT one costs.apify_indeed row. `record` keys mirror the columns;
    recorded_at is stamped here."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.apify_indeed
                (apify_run_id, bto_run_id, market, search_term, started_at,
                 finished_at, status, result_count, cost_usd, recorded_at)
            VALUES (%(apify_run_id)s, %(bto_run_id)s, %(market)s,
                    %(search_term)s, %(started_at)s, %(finished_at)s,
                    %(status)s, %(result_count)s, %(cost_usd)s, now())
            ON CONFLICT (apify_run_id) DO UPDATE SET
                finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status,
                result_count = EXCLUDED.result_count,
                cost_usd = EXCLUDED.cost_usd,
                recorded_at = now()
            """,
            record,
        )


def record_linkedin_cost(record):
    """UPSERT one costs.apify_linkedin row (no search_term: one Actor run
    covers all configured terms)."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.apify_linkedin
                (apify_run_id, bto_run_id, market, started_at, finished_at,
                 status, result_count, cost_usd, recorded_at)
            VALUES (%(apify_run_id)s, %(bto_run_id)s, %(market)s,
                    %(started_at)s, %(finished_at)s, %(status)s,
                    %(result_count)s, %(cost_usd)s, now())
            ON CONFLICT (apify_run_id) DO UPDATE SET
                finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status,
                result_count = EXCLUDED.result_count,
                cost_usd = EXCLUDED.cost_usd,
                recorded_at = now()
            """,
            record,
        )
