"""Coordinate loading, filtering, model work and bucketing of job listings.

The command supports history loading, nightly and catch-up Batch runs, free
re-sorting and reports. After sorting, it can refresh Review and send an email
when those actions are requested."""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bto.log_errors import log_failure
from bto.storage.databricks.freshness import StaleMapping
from bto.storage.postgres import connection as pg_connection
from bto.storage.postgres import read as pg_read
from bto.storage.postgres import write as pg_write
from bto.storage.postgres.connection import connect
from bto.storage.postgres.setup import ensure_job_sorting_tables

from . import processing_start
from .bucket_job_listings import bucket_listing
from .config import load_preferences, load_prompt
from .pull_job_listings import pull_job_listings, source_versions, write_checkpoint
from .suppress_repeated_job_listings import repeat_decisions

log = logging.getLogger(__name__)


def refresh_repeats(conn):
    """Recheck repeat links one canonical group at a time and clear invalid pointers."""
    columns = "id,board,market,board_job_id,canonical_job_id,title,listing_first_seen_at,wording_first_seen_at,bucket,reason,lacks,repeat_of_id"
    group = []
    key = None
    changed = 0
    pending = []
    # One canonical group at a time, without holding the full JD population in RAM.
    for row in pg_read.stream_rows(
        conn,
        # Pre-window rows cannot be candidates; retain any stale pointers to clear them.
        f"SELECT {columns} FROM job_sorting.job_listings WHERE canonical_job_id IS NOT NULL AND (wording_first_seen_at>=%s OR repeat_of_id IS NOT NULL) ORDER BY canonical_job_id,wording_first_seen_at,id",
        (processing_start(),),
        batch_size=5000,
    ):
        if key is not None and row["canonical_job_id"] != key:
            changes = repeat_decisions(group)
            pending.extend(changes)
            changed += len(changes)
            if len(pending) >= 500:
                pg_write.update_repeats(conn, pending)
                pending.clear()
            group = []
        key = row["canonical_job_id"]
        group.append(row)
    changes = repeat_decisions(group)
    pending.extend(changes)
    pg_write.update_repeats(conn, pending)
    changed += len(changes)
    conn.execute(
        "UPDATE job_sorting.job_listings SET repeat_of_id=NULL WHERE canonical_job_id IS NULL AND repeat_of_id IS NOT NULL"
    )
    log.info("repeat recheck changed=%d", changed)
    return changed


def read_sorting_batch(conn, predicate, parameters):
    # Select the bounded IDs before joining wide JD and model-answer content.
    statement = (
        "WITH sorting_batch AS MATERIALIZED(SELECT j.id FROM job_sorting.job_listings j "
        f"WHERE {predicate} ORDER BY j.id LIMIT 500)"
        + pg_read.JOIN_RESULTS
        + " JOIN sorting_batch b ON b.id=j.id ORDER BY j.id"
    )
    return pg_read.read_rows(conn, statement, parameters)


def re_sort(conn, preferences):
    """Re-evaluate stored answers without changing their paid input identities."""
    last_id = 0
    count = 0
    while True:
        rows = read_sorting_batch(
            conn, "j.id>%s AND j.wording_first_seen_at>=%s", (last_id, processing_start())
        )
        if not rows:
            break
        decisions = [bucket_listing(row, row["triage"], row["extraction"], preferences)
                     for row in rows]
        pg_write.update_results(conn, decisions)
        count += len(rows)
        last_id = rows[-1]["id"]
    refresh_repeats(conn)
    log.info("deterministic re-sort rows=%d", count)
    return count


def run_sorting(conn, preferences, *, state_dir=None, report=None):
    """Reapply deterministic rules to stored answers without provider calls."""
    report = report if report is not None else {}
    report.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    report.setdefault(
        "first_id",
        conn.execute("SELECT coalesce(max(id),0) FROM job_sorting.job_listings").fetchone()[0],
    )
    report.setdefault("counters", {})
    state_dir = Path(state_dir) if state_dir else None

    def save():
        if state_dir:
            write_checkpoint(state_dir / "re_sort.json", report)

    if (
        state_dir
        and (state_dir / "history.json").exists()
        and json.loads((state_dir / "history.json").read_text()).get("status") != "complete"
    ):
        raise RuntimeError("The initial history load is unfinished; resume history-load first")
    save()
    rules_start = report.get("timings", {}).get("rules_pages", {}).get("start") or datetime.now(timezone.utc).isoformat()
    re_sort(conn, preferences)
    report.setdefault("timings", {})["rules_pages"] = {
        "start": rules_start,
        "end": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "complete"
    save()
    return report


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Filter and bucket cleaned job listings")
    parser.add_argument(
        "--mode", choices=("night", "catch-up", "re-sort", "history-load", "report"), required=True
    )
    parser.add_argument(
        "--allow-gemini", action="store_true", help="explicitly permit paid Gemini/cache calls"
    )
    parser.add_argument(
        "--execute-load",
        action="store_true",
        help="explicitly permit the real historical population",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="explicitly send the approved summary to the configured recipient",
    )
    parser.add_argument("--refresh-review", action="store_true",
                        help="refresh the configured serving snapshot after final validation")
    parser.add_argument("--ceiling-usd", type=Decimal)
    parser.add_argument(
        "--estimate-basis", choices=("conservative", "expected"), default="conservative",
        help="catch-up admission basis; expected requires explicit spend authorisation",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "data" / "job_sorting",
    )
    args = parser.parse_args(argv)
    if args.mode in {"night", "catch-up"} and not args.allow_gemini:
        parser.error("paid modes require --allow-gemini after separate approval")
    if args.mode == "history-load" and not args.execute_load:
        parser.error("history-load requires --execute-load after separate approval")
    if args.estimate_basis == "expected" and args.mode != "catch-up":
        parser.error("expected-cost admission is only available for explicitly authorised catch-up")
    if args.mode == "catch-up" and args.ceiling_usd is None:
        parser.error("catch-up requires an explicitly approved --ceiling-usd")
    if args.ceiling_usd is not None and (not args.ceiling_usd.is_finite() or args.ceiling_usd <= 0):
        parser.error("ceiling must be finite and positive")
    if args.mode == "night":
        from .config import normal_night_ceiling

        ceiling = normal_night_ceiling()
        if args.ceiling_usd not in (None, ceiling):
            parser.error("normal nights must use the configured private ceiling")
        args.ceiling_usd = ceiling
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from bto import settings

    settings.load_postgres()
    if args.mode in {"night", "catch-up"}:
        return _batch_main(args)
    with connect() as conn, pg_connection.sorting_owner(conn):
        ensure_job_sorting_tables(conn)
        if args.mode == "report":
            from bto.send_notifications.notify import bucket_validation_report

            print(json.dumps(bucket_validation_report(conn), default=str, indent=2))
            return 0
        if args.mode == "history-load":
            from bto.storage.databricks.connection import Databricks

            config = settings.load()
            dbx = Databricks(
                config["databricks_host"],
                config["databricks_token"],
                config["databricks_warehouse_id"],
            )
            pull_job_listings(
                dbx,
                config["databricks_catalog"],
                conn,
                history=True,
                checkpoint=args.state_dir / "history.json",
            )
            return 0
        preferences = load_preferences()
        state_path = args.state_dir / "re_sort.json"
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        if previous and previous.get("status") == "complete":
            previous = None
        report = previous or {"status": "running", "mode": args.mode}
        try:
            run_sorting(
                conn,
                preferences,
                state_dir=args.state_dir,
                report=report,
            )
        except Exception as error:
            report.update(
                status="failed",
                error=type(error).__name__,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            write_checkpoint(state_path, report)
            log_failure(log, "Free sorting failed checkpoint=%s", state_path, error=error)
        from bto.send_notifications.nightly_email import render_sorting_summary
        from bto.send_notifications.notify import bucket_validation_report

        _subject, plain, html = render_sorting_summary(report, bucket_validation_report(conn))
        args.state_dir.mkdir(parents=True, exist_ok=True)
        (args.state_dir / "morning_email.html").write_text(html)
        (args.state_dir / "morning_email.txt").write_text(plain)
        return 0 if report["status"] == "complete" else 1


def _batch_main(args):
    """Run paid sorting, then prepare the terminal report for its caller."""
    from bto import settings
    from bto.send_notifications import notify
    from bto.storage.databricks.connection import Databricks
    from bto.storage.databricks.freshness import require_current_mapping

    from .call_gemini_batch import release_checkpoint_memory, save
    from .estimate_batches import estimate
    from .run_batches import run

    config = settings.load()
    preferences = load_preferences()
    prompts = {s: load_prompt(s, preferences) for s in ("triage", "extraction")}
    values = settings._dotenv()
    keys = {}
    for stage, name in (("triage", "GEMINI_TRIAGE_JOBS_API_KEY"),
                        ("extraction", "GEMINI_EXTRACT_JOB_DETAILS_API_KEY")):
        keys[stage] = os.environ.get(name) or values.get(name)
        if not keys[stage]:
            raise ValueError("Missing " + name)
    root_state = Path(__file__).resolve().parents[3] / "data" / "job_sorting"
    dbx = Databricks(config["databricks_host"], config["databricks_token"], config["databricks_warehouse_id"])
    def reconcile(conn):
        ensure_job_sorting_tables(conn)
        history = root_state / "history.json"
        if history.exists() and json.loads(history.read_text()).get("status") != "complete":
            raise RuntimeError("Resume the unfinished history load before normal pulling")
        first_id = conn.execute("SELECT coalesce(max(id),0) FROM job_sorting.job_listings").fetchone()[0]
        if not first_id:
            raise RuntimeError("Complete the explicitly approved initial history load first")
        versions = source_versions(dbx, config["databricks_catalog"])
        require_current_mapping(dbx, config["databricks_catalog"])
        began = datetime.now(timezone.utc).isoformat()
        pulled = pull_job_listings(dbx, config["databricks_catalog"], conn, checkpoint=args.state_dir / "pull.json")
        if not pulled.get("grouping_available") or versions != source_versions(dbx, config["databricks_catalog"]):
            raise StaleMapping("Source publication changed or grouping is not coherent")
        return {"versions": versions, "first_id": first_id,
            "last_id": conn.execute("SELECT coalesce(max(id),0) FROM job_sorting.job_listings").fetchone()[0], "pull": {
            **pulled,
            "started_at": began, "finished_at": datetime.now(timezone.utc).isoformat()}}
    def fresh_estimate(items):
        with connect() as conn:
            return estimate(conn, items, prompts, preferences, keys)
    publish_review = None
    if getattr(args, "refresh_review", False):
        from bto.review_jds.refresh_review_cache import configuration, refresh

        refresh_config = configuration()

        def publish_review():
            saved_source = json.loads((args.state_dir / "batch.json").read_text())["source"]
            release_checkpoint_memory()
            require_current_mapping(dbx, config["databricks_catalog"])
            if source_versions(dbx, config["databricks_catalog"]) != saved_source:
                raise StaleMapping("Source changed before Review refresh; reconcile before publication")
            publication = refresh(refresh_config)
            publication["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            return publication

    nightly_path = root_state / "nightly.json"
    nightly = json.loads(nightly_path.read_text()) if nightly_path.exists() else {}
    backfill_started = nightly.get("backfill_started_at") if nightly.get("run_id") == args.state_dir.name else None
    del nightly
    release_checkpoint_memory()
    try:
        report = run(preferences, prompts, keys, state_dir=args.state_dir,
            ceiling=args.ceiling_usd, estimate=fresh_estimate,
            owner_dir=root_state, reconcile=reconcile, estimate_basis=args.estimate_basis,
            publish_review=publish_review, backfill_started_at=backfill_started)
    except Exception as error:
        path = args.state_dir / "batch.json"
        if not path.exists():
            path = args.state_dir / "preflight-failure.json"
        report = json.loads(path.read_text()) if path.exists() else {
            "started_at": datetime.now(timezone.utc).isoformat(), "failed_stage": "preparation"}
        report.update(status="failed", error=str(error), error_type=type(error).__name__)
        save(args.state_dir / "batch-failure.json", report)
        log_failure(log, "Sorting stopped stage=%s checkpoint=%s",
                    report.get("failed_stage"), args.state_dir / "batch-failure.json",
                    error=error)
    nightly_path = root_state / "nightly.json"
    nightly = {}
    if nightly_path.exists():
        nightly = json.loads(nightly_path.read_text())
        if nightly.get("run_id") == args.state_dir.name:
            report["collection_night"] = nightly.get("collection_night")
    from bto.send_notifications.nightly_email_data import collect, collect_context

    if report.get("status") == "complete":
        try:
            with connect() as conn:
                report["email_facts"] = collect(conn, report)
        except Exception as error:
            log_failure(log, "Secondary nightly email facts unavailable",
                        error=error, level=logging.WARNING)
        try:
            with connect() as conn:
                report["email_context"] = collect_context(conn, report, config=config, dbx=dbx)
        except Exception as error:
            report["email_report_unavailable"] = True
            log_failure(log, "Nightly email detail unavailable; processing remains complete",
                        error=error, level=logging.WARNING)
    if report.get("status") == "complete" or (args.state_dir / "batch.json").exists():
        save(args.state_dir / "batch.json", report)
    if config.get("combined_nightly_email"):
        # The parent renders the one terminal message from this saved report.
        return 0 if report["status"] == "complete" else 1
    subject, plain, html = notify.render_terminal_nightly({
        "status": report["status"], "started_at": report["started_at"], "sorting": report,
        "failed_stage": report.get("failed_stage"), "error": report.get("error")})
    if args.send_email:
        delivered = notify.send_email(config, subject, plain, html=html)
        if delivered and report["status"] != "complete":
            return 5
    else:
        (args.state_dir / "morning_email.html").write_text(html)
        (args.state_dir / "morning_email.txt").write_text(plain)
    return 0 if report["status"] == "complete" else 1


def main(argv=None):
    try:
        return _main(argv)
    except pg_connection.SortingBusy:
        log.info("Sorting already has an owner; this runner has no work")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
