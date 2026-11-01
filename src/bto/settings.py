"""Load runtime settings and select supported board × market runs.

Environment variables take precedence over the local .env file. systemd
supplies production settings from /etc/bto/env. PostgreSQL-only commands use
load_postgres() without requiring collection or provider credentials.

Search terms live in each board's private searches.py; source configuration
lives with its adapter.
"""

import math
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


def private_dir():
    """Locate the optional private checkout; BTO_PRIVATE_DIR overrides <repo>/private."""
    value = os.environ.get("BTO_PRIVATE_DIR") or _dotenv().get("BTO_PRIVATE_DIR")
    return Path(value).expanduser() if value else REPO_ROOT / "private"


def load():
    """Validate collection settings before starting work."""
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
        if not math.isfinite(value) or value < minimum:
            raise SystemExit(f"{key} must be finite and >= {minimum}, got {raw!r}")
        return value

    # libpq reads the environment directly, so local .env values must reach it.
    _load_postgres_environment(fallback)

    host = (get("DATABRICKS_HOST") or "").rstrip("/")
    token = get("DATABRICKS_TOKEN") or ""
    if not host or not token:
        raise SystemExit("DATABRICKS_HOST / DATABRICKS_TOKEN missing — "
                         "fill .env locally or /etc/bto/env on the box")
    # The live catalog name is configuration, never hardcoded.
    catalog = get("DATABRICKS_CATALOG") or ""
    if not catalog:
        raise SystemExit("DATABRICKS_CATALOG missing — set it in .env or /etc/bto/env")

    return {
        "databricks_host": host,
        "databricks_token": token,
        "databricks_warehouse_id": get("DATABRICKS_WAREHOUSE_ID"),
        "databricks_catalog": catalog,

        "apify_token": get("APIFY_TOKEN") or "",

        "combined_nightly_email": get("BTO_COMBINED_NIGHTLY_EMAIL", "0") == "1",

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

        # Saved collection run IDs are shared by nightly processing and summaries.
        "run_handoff_path": get("RUN_HANDOFF_PATH",
                                str(REPO_ROOT / "data" / "last_fetch.json")),
    }


def enabled_runs():
    return list(ENABLED)


def supported_runs():
    return list(SUPPORTED)


def _load_postgres_environment(fallback):
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if key not in os.environ and fallback.get(key):
            os.environ[key] = fallback[key]


def load_postgres(fallback=None):
    """Load libpq settings without requiring analytical/provider settings."""
    _load_postgres_environment(_dotenv() if fallback is None else fallback)
    if not os.environ.get("PGDATABASE"):
        raise ValueError("PGDATABASE is missing from private configuration")
