"""Collect the recorded facts needed by the nightly email.

Read saved run reports and query collection, sorting and cost records. These
reporting reads do not call models or change stored listing results."""

import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from bto.filter_and_bucket_job_listings import SINGAPORE, night
from bto.filter_and_bucket_job_listings.config import load_triage_policy
from bto.log_errors import log_failure
from bto.storage.postgres.read import read_rows

log = logging.getLogger(__name__)


def _collection_cost(conn, ids):
    return conn.execute(
        "SELECT sum(cost_usd) FROM ("
        "SELECT bto_run_id,cost_usd FROM costs.apify_indeed UNION ALL "
        "SELECT bto_run_id,cost_usd FROM costs.apify_linkedin) runs "
        "WHERE bto_run_id=ANY(%s)",
        (sorted(ids),),
    ).fetchone()[0]


def collect_collection_cost(report, config):
    """Read recorded collection costs for the run IDs retained by this logical night."""
    import json
    from pathlib import Path

    from bto.storage.postgres.connection import connect

    previous_ids = {v for v in report.get("collection_run_ids", []) if isinstance(v, str) and v}
    ids = set(previous_ids)
    if report.get("backfill") and not previous_ids and str(report.get("prior_failure", {}).get("stage", "")).casefold() == "collection":
        # Without the original source IDs, a later handoff cannot prove the cost.
        report["collection_cost_incomplete"] = True
    ids.update(r["run_id"] for r in report.get("collection_rows", []) if r.get("run_id"))
    stage = report.get("stages", {}).get("collection", {})
    try:
        handoff = json.loads(Path(config["run_handoff_path"]).read_text())
        begin = datetime.fromisoformat(stage["started_at"]).replace(microsecond=0)
        end = datetime.fromisoformat(stage["finished_at"]) if stage.get("finished_at") else datetime.now(timezone.utc)
        at = datetime.fromisoformat(handoff["started_at"])
        if handoff.get("state") == "finished" and begin <= at <= end:
            ids.update(v for v in handoff.get("run_ids", []) if isinstance(v, str) and v)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    report["collection_run_ids"] = sorted(ids)
    if not ids:
        report.setdefault("collection_cost_usd", None)
        return
    try:
        with connect(timeout_s=5) as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='5s'")
            value = _collection_cost(conn, ids)
        report["collection_cost_usd"] = str(value) if value is not None and not report.get("collection_cost_incomplete") else None
    except Exception as error:
        log_failure(log, "Nightly collection cost unavailable", error=error, level=logging.WARNING)
        if ids != previous_ids:
            report["collection_cost_usd"] = None
        else:
            report.setdefault("collection_cost_usd", None)


def collect(conn, report):
    """Freeze completion counts for later email retries.

    Review counts grouped jobs; outcome counts pulled wordings. Keep them separate."""
    from bto.web.review_job_listings import DEFAULT_SCOPE, grouped_query

    started = datetime.fromisoformat(report["started_at"])
    day = (
        datetime.fromisoformat(report["collection_night"]).date()
        if report.get("collection_night")
        else night(started)
    )
    begin = datetime.combine(day, time(3), SINGAPORE)
    facts = {}
    last_id = report.get("last_id")
    if last_id is None:
        # Freeze a missing upper boundary once so email retries retain their scope.
        last_id = conn.execute("SELECT coalesce(max(id),0) FROM job_sorting.job_listings").fetchone()[0]
        report["last_id"] = last_id
    ended = datetime.fromisoformat(report["finished_at"]) if report.get("finished_at") else datetime.now(timezone.utc)
    month_start = ended.astimezone(SINGAPORE).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # This table partitions pulled wordings before Review groups board copies.
    # Suppression takes precedence over the underlying bucket, and language
    # ineligibility is split out of the remaining Ineligible population.
    outcomes_sql = grouped_query(queue=True, defer_context=True) + """
 SELECT upper(j.market) market,
 CASE WHEN j.repeat_of_id IS NOT NULL THEN 'suppressed'
 WHEN j.bucket IN ('top_picks','in_lane') OR
  (j.bucket='less_interested' AND """ + DEFAULT_SCOPE.replace("g.", "j.") + """) THEN 'in_review_queue'
 WHEN j.bucket='less_interested' THEN 'less_interested_outside'
 WHEN j.bucket='ineligible' AND (j.reason LIKE 'JD not in English%%' OR t.verdict=%s) THEN 'not_english'
 WHEN j.bucket='ineligible' THEN 'ineligible_other'
 ELSE coalesce(j.bucket,'not_sorted') END category,count(*) n
 FROM job_sorting.job_listings j
 LEFT JOIN job_sorting.triage_results t ON t.input_hash=j.triage_input_hash
 WHERE j.id>%s AND j.id<=%s
 GROUP BY 1,2"""
    operations = {
        "review_counts": (
            grouped_query(queue=True, defer_context=True)
            + "SELECT upper(g.market) market,g.bucket,count(*) n FROM grouped g "
            "WHERE g.listing_first_seen_at>=%s AND g.listing_first_seen_at<%s "
            "AND g.bucket IN ('top_picks','in_lane','less_interested') AND "
            + DEFAULT_SCOPE
            + " GROUP BY 1,2",
            (begin, begin + timedelta(days=1)),
        ),
        "outcomes": (outcomes_sql, (load_triage_policy()["language"], report.get("first_id", 0), last_id)),
        "filter_counts": (
            "SELECT count(*) pulled,"
            "count(*) FILTER(WHERE bucket='title_filter') title_filter,"
            "count(*) FILTER(WHERE bucket='ineligible' "
            "AND decided_by='rules' AND reason LIKE 'JD not in English%%') language_filter "
            "FROM job_sorting.job_listings WHERE id>%s AND id<=%s",
            (report.get("first_id", 0), last_id),
        ),
        "triage_answer_hashes": (
            "SELECT DISTINCT triage_input_hash FROM job_sorting.job_listings "
            "WHERE id>%s AND id<=%s "
            "AND triage_input_hash IS NOT NULL AND bucket IS DISTINCT FROM 'title_filter' "
            "AND NOT (bucket='ineligible' AND decided_by='rules' AND reason LIKE 'JD not in English%%')",
            (report.get("first_id", 0), last_id),
        ),
        "language_counts": (
            "SELECT upper(market) market,count(*) n FROM job_sorting.job_listings "
            "WHERE id>%s AND id<=%s AND reason LIKE 'JD not in English%%' "
            "AND repeat_of_id IS NULL GROUP BY 1",
            (report.get("first_id", 0), last_id),
        ),
        "gemini_month_usd": (
            "SELECT coalesce(sum(cost_usd),0)::text amount FROM ("
            "SELECT cost_usd,recorded_at FROM costs.gemini_triage UNION ALL "
            "SELECT cost_usd,recorded_at FROM costs.gemini_extraction) calls "
            "WHERE recorded_at>=%s AND recorded_at<=%s",
            (month_start, ended),
        ),
    }
    for name, (sql, params) in operations.items():
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='60s'")
                rows = read_rows(conn, sql, params)
                facts[name] = rows[0]["amount"] if name.endswith("_usd") else rows
        except Exception as error:
            log_failure(log, "Nightly email secondary fact unavailable: %s", name,
                        error=error, level=logging.WARNING)
    if "filter_counts" in facts:
        facts["funnel"] = facts.pop("filter_counts")[0]
    if "triage_answer_hashes" in facts:
        sent = set(report.get("items", {}).get("triage", {}))
        facts.setdefault("funnel", {})["triage_reused"] = sum(
            r["triage_input_hash"] not in sent for r in facts.pop("triage_answer_hashes")
        )
    ids = sorted(
        {
            i
            for items in report.get("items", {}).values()
            for item in items.values()
            if item.get("outcome") == "terminal"
            for i in item.get("ids", [])
        }
    )
    if ids:
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='15s'")
                rows = read_rows(
                    conn,
                    "SELECT id,job_url,advertiser_name,board,market,title "
                    "FROM job_sorting.job_listings WHERE id=ANY(%s)",
                    (ids,),
                )
            facts["failure_listings"] = {str(r["id"]): r for r in rows}
        except Exception as error:
            log_failure(log, "Nightly email direct failure links unavailable", error=error, level=logging.WARNING)
    return facts


def collect_context(conn, report, *, config=None, dbx=None, collection_rows=None):
    """Read only the source and cost facts used by the current email."""
    from bto.storage.postgres.read import cost_totals

    from .notify import read_collection

    context = {"SPENT": "—", "COLLECTION_COST": "—"}
    if "receipts" in report:
        usage = list(report["receipts"].values())
    else:
        usage = list(report.get("usage_totals", {}).values())
        if not usage:
            usage = cost_totals(conn, datetime.fromisoformat(report["started_at"]))
    for stage in ("triage", "extraction"):
        rows = [row for row in usage if row["stage"] == stage]
        cost = sum((Decimal(row["cost_usd"]) for row in rows), Decimal(0))
        attempted = any(item.get("answer_attempts", 0)
                        for item in report.get("items", {}).get(stage, {}).values()) or any(
            job.get("name") for job in report.get("jobs", {}).get(stage, []))
        context[stage.upper() + "_COST"] = f"USD {cost:.4f}" if rows or not attempted else "—"
        cached = sum(row["cached_tokens"] for row in rows)
        total = cached + sum(row["input_tokens"] for row in rows)
        context[stage.upper() + "_CACHE"] = f"{100 * cached / total:.0f}%" if total else "—"
        context[stage.upper() + "_NO_ANSWER"] = report.get("counters", {}).get(stage + "_no_answer", 0)

    if collection_rows is None and dbx is not None and config:
        try:
            _, collection_rows = read_collection(dbx, config)
        except Exception as error:
            log_failure(log, "Collection email facts unavailable", error=error, level=logging.WARNING)
    context["collection_runs"] = collection_rows or []
    ids = sorted({row["run_id"] for row in collection_rows or [] if row.get("run_id")})
    if ids:
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='5s'")
                value = _collection_cost(conn, ids)
            if value is not None:
                context["COLLECTION_COST"] = f"USD {value:.4f}"
        except Exception as error:
            log_failure(log, "Collection email cost unavailable", error=error, level=logging.WARNING)
    if config and config.get("apify_token") and dbx is not None:
        try:
            from bto.fetch_job_listings.apify_client import ApifyClient

            account = ApifyClient(config["apify_token"]).account()
            context["SPENT"] = f"{account['spentUsd']:.2f}"
        except Exception as error:
            log_failure(log, "Apify monthly account report unavailable", error=error, level=logging.WARNING)
    return context
