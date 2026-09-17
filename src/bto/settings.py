"""Runtime configuration: environment, secrets, and which board × market runs.

Called by:
    __main__ (CLI entrypoints), fetch_job_listings.run

Environment variables win; a repo-root .env file fills the gaps for laptop
runs. On the Lightsail box, systemd supplies the environment from /etc/bto/env.

Main functions:
    load()            -> dict of validated runtime settings
    enabled_runs()    -> [(board, market)] switched on for the nightly schedule
    supported_runs()  -> every proven board × market pairing

This module holds no board behaviour. Search terms live in each board's
searches.py; per-market source configuration lives in each board's markets.py.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every proven board × market pairing (see fetch_job_listings/README.md).
# A pairing listed here but not in ENABLED keeps its configuration and can be
# run by hand with `python -m bto collect --board X --market Y`.
SUPPORTED = (
    ("mcf", "sg"),
    ("jobstreet", "sg"),
    ("jobsdb", "hk"),
    ("jobsdb", "th"),
    ("seek", "au"),
    ("seek", "nz"),
    ("indeed", "sg"),
    ("indeed", "hk"),
    ("indeed", "th"),
    ("indeed", "au"),
    ("indeed", "nz"),
    ("indeed", "uk"),
    ("indeed", "us"),
    ("indeed", "ca"),
    ("linkedin", "sg"),
    ("linkedin", "hk"),
    ("linkedin", "th"),
    ("linkedin", "au"),
)

# The 10 runs on the nightly schedule. SEEK AU/NZ, Indeed AU/NZ/UK/US/CA and
# LinkedIn AU stay supported but disabled.
ENABLED = (
    ("mcf", "sg"),
    ("jobstreet", "sg"),
    ("jobsdb", "hk"),
    ("jobsdb", "th"),
    ("indeed", "sg"),
    ("indeed", "hk"),
    ("indeed", "th"),
    ("linkedin", "sg"),
    ("linkedin", "hk"),
    ("linkedin", "th"),
)

# At most 4 board × market collection runs execute at once.
MAX_PARALLEL_RUNS = 4

# MCF/SEEK full-JD runaway guard: candidates past this become `deferred` and
# are picked up by later runs. Reaching it is abnormal and notifies.
MAX_JD_FETCHES_PER_RUN = 5000


def _dotenv():
    values = {}
    dotenv = REPO_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                elif " #" in value:
                    value = value.split(" #", 1)[0].rstrip()
                values[key.strip()] = value
    return values


def load():
    """Validated runtime settings. Garbage config dies here with the variable
    named, not forty minutes into a run with a bare traceback."""
    fallback = _dotenv()

    def get(key, default=None):
        return os.environ.get(key) or fallback.get(key) or default

    def _int(key, default, minimum=0):
        raw = get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SystemExit(f"{key} must be an integer, got {raw!r}")
        if value < minimum:
            raise SystemExit(f"{key} must be >= {minimum}, got {value}")
        return value

    def _float(key, default, minimum=0.0):
        raw = get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SystemExit(f"{key} must be a number, got {raw!r}")
        if not value >= minimum:            # also rejects NaN
            raise SystemExit(f"{key} must be >= {minimum}, got {value}")
        return value

    # psycopg reads the libpq PG* variables from the process environment —
    # which systemd provides on the box. For laptop runs the .env fallback
    # must reach the environment too, or psycopg silently tries a local
    # socket instead of the tunnel.
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if key not in os.environ and fallback.get(key):
            os.environ[key] = fallback[key]

    host = (get("DATABRICKS_HOST") or "").rstrip("/")
    token = get("DATABRICKS_TOKEN") or ""
    if not host or not token:
        raise SystemExit("DATABRICKS_HOST / DATABRICKS_TOKEN missing — "
                         "fill .env locally or /etc/bto/env on the box")
    # The live catalog name is configuration, never hardcoded.
    catalog = get("DATABRICKS_CATALOG") or ""
    if not catalog:
        raise SystemExit("DATABRICKS_CATALOG missing — the target catalog is "
                         "configuration (currently `bto`), never a hardcode")

    return {
        "databricks_host": host,
        "databricks_token": token,
        "databricks_warehouse_id": get("DATABRICKS_WAREHOUSE_ID"),
        "databricks_catalog": catalog,

        "apify_token": get("APIFY_TOKEN") or "",

        # Email. Unset SMTP means alerts are logged and collection carries on.
        "smtp_host": get("SMTP_HOST") or "",
        "smtp_port": _int("SMTP_PORT", "587"),
        "smtp_username": get("SMTP_USERNAME") or "",
        "smtp_password": get("SMTP_PASSWORD") or "",
        "notify_email_to": get("NOTIFY_EMAIL_TO") or "",

        # MCF/SEEK direct-HTTP politeness and the contract fetch cap.
        "request_pause_s": _float("REQUEST_PAUSE_S", "0.5"),
        "max_jd_fetches_per_run": _int(
            "MAX_JD_FETCHES_PER_RUN", str(MAX_JD_FETCHES_PER_RUN), minimum=1),

        # Apify operating envelope (per-actor-run and per-board ceilings are
        # enforced both at Actor start and against the reported cost).
        "apify_run_timeout_s": _int("APIFY_RUN_TIMEOUT_S", "900", minimum=60),
        "apify_indeed_max_charge_usd": _float(
            "APIFY_INDEED_MAX_CHARGE_USD", "0.25", minimum=0.001),
        "apify_indeed_board_budget_usd": _float(
            "APIFY_INDEED_BOARD_BUDGET_USD", "1.00", minimum=0.01),
        "apify_linkedin_run_timeout_s": _int(
            "APIFY_LINKEDIN_RUN_TIMEOUT_S", "7200", minimum=60),

        # Smoke-test control: >0 truncates the sweep. A truncated sweep is
        # written with full_sweep = FALSE so it can never look like a full one.
        "terms_limit": _int("TERMS_LIMIT", "0"),

        # Where one fetch invocation records which runs belong to it, so the
        # next stage in the service chain (today: the summary email) reports
        # on exactly that execution. Local runtime state, never committed.
        "run_handoff_path": get("RUN_HANDOFF_PATH",
                                str(REPO_ROOT / "data" / "last_fetch.json")),
    }


def enabled_runs():
    return list(ENABLED)


def supported_runs():
    return list(SUPPORTED)
