"""Conduct pulling, bounded model work, deterministic re-sorts and reporting."""

import argparse
import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from bto.storage.databricks.freshness import StaleMapping
from bto.storage.postgres import job_sorting as store
from bto.storage.postgres.connection import connect
from bto.storage.postgres.setup_job_sorting import ensure_job_sorting_tables
from bto.sync.pull_job_listings import pull_job_listings, source_versions, write_checkpoint

from . import DEFERRED_REASONS, PROCESSING_START, SINGAPORE, TERMINAL_REASONS, night
from .bucket_job_listings import bucket_listing
from .config import load_preferences, load_prompt
from .suppress_repeated_job_listings import repeat_decisions

log = logging.getLogger(__name__)


def refresh_repeats(conn):
    columns = "id,board,market,board_job_id,canonical_job_id,title,listing_first_seen_at,wording_first_seen_at,bucket,reason,lacks,repeat_of_id"
    group = []
    key = None
    changed = 0
    pending = []
    # One canonical group at a time, without holding the full JD population in RAM.
    for row in store.stream_rows(
        conn,
        # Pre-window rows cannot be candidates; retain any stale pointers to clear them.
        f"SELECT {columns} FROM job_sorting.job_listings WHERE canonical_job_id IS NOT NULL AND (wording_first_seen_at>=%s OR repeat_of_id IS NOT NULL) ORDER BY canonical_job_id,wording_first_seen_at,id",
        (PROCESSING_START,),
        batch_size=5000,
    ):
        if key is not None and row["canonical_job_id"] != key:
            changes = repeat_decisions(group)
            pending.extend(changes)
            changed += len(changes)
            if len(pending) >= 500:
                store.update_repeats(conn, pending)
                pending.clear()
            group = []
        key = row["canonical_job_id"]
        group.append(row)
    changes = repeat_decisions(group)
    pending.extend(changes)
    store.update_repeats(conn, pending)
    changed += len(changes)
    conn.execute(
        "UPDATE job_sorting.job_listings SET repeat_of_id=NULL WHERE canonical_job_id IS NULL AND repeat_of_id IS NOT NULL"
    )
    log.info("repeat recheck changed=%d", changed)
    return changed


def inputs(row, prompts):
    from .call_gemini import input_hash
    from .extract_job_listing_details import listing_input as extract_input
    from .triage_job_listings import listing_input as triage_input

    closings = {
        "triage": triage_input(row, prompts["triage"][1]),
        "extraction": extract_input(row, prompts["extraction"][1]),
    }
    return {stage: (input_hash(prompts[stage][0], text), text) for stage, text in closings.items()}


def read_sorting_batch(conn, predicate, parameters, *, descending=False):
    direction = "DESC" if descending else "ASC"
    # Select the bounded IDs before joining wide JD and model-answer content.
    statement = (
        "WITH sorting_batch AS MATERIALIZED(SELECT j.id FROM job_sorting.job_listings j "
        f"WHERE {predicate} ORDER BY j.id {direction} LIMIT 500)"
        + store.JOIN_RESULTS
        + f" JOIN sorting_batch b ON b.id=j.id ORDER BY j.id {direction}"
    )
    return store.read_rows(conn, statement, parameters)


def re_sort(conn, preferences, prompts):
    last_id = 0
    count = 0
    while True:
        rows = read_sorting_batch(
            conn,
            "j.id>%s AND j.wording_first_seen_at>=%s",
            (last_id, PROCESSING_START),
        )
        if not rows:
            break
        decisions = []
        invalid_links = {"triage": [], "extraction": []}
        for row in rows:
            hashes = inputs(row, prompts)
            for stage, invalid in invalid_links.items():
                if (
                    row.get(stage + "_input_hash")
                    and row[stage + "_input_hash"] != hashes[stage][0]
                ):
                    invalid.append({"id": row["id"], "input_hash": None})
            triage = row["triage"] if row.get("triage_input_hash") == hashes["triage"][0] else None
            extraction = (
                row["extraction"]
                if row.get("extraction_input_hash") == hashes["extraction"][0]
                else None
            )
            decisions.append(bucket_listing(row, triage, extraction, preferences))
        for stage, pairs in invalid_links.items():
            store.link_answers(conn, stage, pairs)
        store.update_results(conn, decisions)
        count += len(rows)
        last_id = rows[-1]["id"]
    refresh_repeats(conn)
    log.info("deterministic re-sort rows=%d", count)
    return count


def process_batch(
    conn,
    rows,
    preferences,
    prompts,
    budget,
    keys,
    *,
    workers=32,
    transport=None,
    wait=None,
    registry=None,
    counters=None,
    report_lock=None,
    next_batch=None,
    on_completed=None,
):
    from .call_gemini import call_checked
    from .extract_job_listing_details import response_schema, validate_extraction
    from .triage_job_listings import SCHEMA, validate_triage

    registry = registry if registry is not None else {}
    counters = counters if counters is not None else {}
    database_lock = threading.Lock()
    registry_lock = threading.Lock()
    counter_lock = report_lock or registry_lock
    prepared = {}
    existing = {stage: {} for stage in ("triage", "extraction")}
    visited = set()

    def prepare(batch):
        batch = [row for row in batch if row["id"] not in visited]
        visited.update(row["id"] for row in batch)
        prepared.update({row["id"]: inputs(row, prompts) for row in batch})
        with database_lock:
            for stage, answers in existing.items():
                answers.update(
                    store.find_answers(
                        conn, stage, [prepared[row["id"]][stage][0] for row in batch]
                    )
                )
                links = [
                    {"id": row["id"], "input_hash": prepared[row["id"]][stage][0]}
                    for row in batch
                    if prepared[row["id"]][stage][0] in answers
                ]
                store.link_answers(conn, stage, links)
        return iter(batch)

    def count(key, number=1):
        with counter_lock:
            counters[key] = counters.get(key, 0) + number

    def cost(stage, record):
        with database_lock:
            return store.record_gemini_cost(conn, stage, record)

    def answer(stage, row):
        digest, text = prepared[row["id"]][stage]
        if digest in existing[stage]:
            count(stage + "_reused")
            return {
                "answer": existing[stage][digest],
                "reason": None,
                "retried": False,
                "called": False,
                "tiers": [],
            }
        key = (stage, digest)
        with registry_lock:
            owner = key not in registry
            if owner:
                registry[key] = Future()
            future = registry[key]
        if owner:
            try:
                validate = (
                    (lambda value: validate_triage(value, row))
                    if stage == "triage"
                    else (
                        lambda value: validate_extraction(
                            value, row, [k["name"] for k in preferences["kinds"]]
                        )
                    )
                )
                schema = (
                    SCHEMA
                    if stage == "triage"
                    else response_schema([k["name"] for k in preferences["kinds"]])
                )
                kwargs = {}
                if transport is not None:
                    kwargs["transport"] = transport
                if wait is not None:
                    kwargs["wait"] = wait
                count(stage + "_started", 0)
                with counter_lock:
                    counters.setdefault(stage + "_start", datetime.now(timezone.utc).isoformat())
                outcome = call_checked(
                    stage,
                    keys[stage],
                    prompts[stage][0],
                    text,
                    schema,
                    validate,
                    budget,
                    cost,
                    **kwargs,
                )
                if outcome["answer"]:
                    with database_lock:
                        store.store_answer(conn, stage, digest, outcome["answer"], [])
                with counter_lock:
                    counters[stage + "_end"] = datetime.now(timezone.utc).isoformat()
                future.set_result(outcome)
            except BaseException as error:
                future.set_exception(error)
                raise
        outcome = future.result()
        if outcome["answer"]:
            with database_lock:
                store.link_answers(conn, stage, [{"id": row["id"], "input_hash": digest}])
        if outcome["called"]:
            count(stage + "_sent")
        if outcome["retried"]:
            count(stage + "_retried")
        if outcome["reason"] in TERMINAL_REASONS:
            count(stage + "_no_answer")
        for tier in outcome["tiers"]:
            count(stage + "_" + ("half" if tier == "flex" else "full") + "_listings")
        return outcome

    def process(row):
        early = bucket_listing(row, None, None, preferences)
        if early["bucket"] != "not_sorted":
            count("title_filter" if early["bucket"] == "title_filter" else "language_cut")
            result = early
        else:
            triage = answer("triage", row)
            extraction = None
            if triage["answer"] and triage["answer"]["verdict"] == "pass":
                count("triage_passed")
                extraction = answer("extraction", row)
            reason = (extraction or triage)["reason"]
            result = bucket_listing(
                {**row, "reason": reason},
                triage["answer"],
                extraction["answer"] if extraction else None,
                preferences,
            )
        if result["reason"] in TERMINAL_REASONS:
            with database_lock:
                store.update_results(conn, [result])
        return result

    results = []
    completed = []
    failure = None
    iterator = prepare(rows)
    exhausted = False
    last_poll = time.monotonic()
    last_publish = last_poll

    def publish():
        nonlocal last_publish
        if completed:
            with database_lock:
                store.update_results(conn, completed)
            if on_completed:
                on_completed(completed)
            completed.clear()
            last_publish = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sort") as pool:
        try:
            pending = {}
            while pending or not exhausted:
                if (
                    exhausted
                    and pending
                    and next_batch
                    and not budget.stop.is_set()
                    and time.monotonic() - last_poll >= 60
                ):
                    with database_lock:
                        more = next_batch()
                    last_poll = time.monotonic()
                    if more:
                        iterator = prepare(more)
                        exhausted = False
                while not exhausted and len(pending) < workers * 4:
                    try:
                        row = next(iterator)
                    except StopIteration:
                        if next_batch and not budget.stop.is_set():
                            with database_lock:
                                more = next_batch()
                            last_poll = time.monotonic()
                            if more:
                                iterator = prepare(more)
                                continue
                        exhausted = True
                        break
                    if budget.stop.is_set():
                        exhausted = True
                        break
                    pending[pool.submit(process, row)] = row["id"]
                if not pending:
                    break
                done, _ = wait_futures(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    identity = pending.pop(future)
                    try:
                        result = future.result()
                        results.append(result)
                        completed.append(result)
                    except Exception as error:
                        budget.stop.set()
                        failure = error
                    prepared.pop(identity, None)
                if len(completed) >= 100 or completed and time.monotonic() - last_publish >= 30:
                    publish()
            publish()

        except BaseException:
            budget.stop.set()
            raise

    if failure:
        raise failure
    log.info("sorting batch finished rows=%d", len(results))
    return results


def run_sorting(
    conn,
    preferences,
    prompts,
    *,
    mode,
    budget=None,
    keys=None,
    dbx=None,
    catalog=None,
    state_dir=None,
    workers=32,
    transport=None,
    wait=None,
    report=None,
):
    report = report if report is not None else {}
    report.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    report.setdefault(
        "first_id",
        conn.execute("SELECT coalesce(max(id),0) FROM job_sorting.job_listings").fetchone()[0],
    )
    report.setdefault("counters", {})
    registry = {}
    checkpoint_lock = threading.RLock()
    state_dir = Path(state_dir) if state_dir else None

    def save():
        # Admission, usage and completed batches share the same atomic checkpoint.
        with checkpoint_lock:
            if state_dir:
                write_checkpoint(
                    state_dir / ("re_sort.json" if mode == "re-sort" else "night.json"), report
                )

    if budget:

        def usage_checkpoint(stage, record):
            with checkpoint_lock:
                totals = report.setdefault("usage_totals", {})
                key = stage + ":" + record["service_tier"]
                aggregate = totals.setdefault(
                    key,
                    {
                        "stage": stage,
                        "service_tier": record["service_tier"],
                        "input_tokens": 0,
                        "cached_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": "0",
                    },
                )
                for field in ("input_tokens", "cached_tokens", "output_tokens"):
                    aggregate[field] += record[field]
                aggregate["cost_usd"] = str(Decimal(aggregate["cost_usd"]) + record["cost_usd"])
                report["spent_usd"] = str(budget.spent)
                save()

        budget.on_usage = usage_checkpoint

        def budget_checkpoint(snapshot):
            with checkpoint_lock:
                report["cost_budget"] = snapshot
                save()

        budget.on_budget = budget_checkpoint
        if budget.guarded:
            budget_checkpoint(budget.snapshot())

    def pull():
        if dbx is None:
            return
        began = datetime.now(timezone.utc)
        result = pull_job_listings(
            dbx, catalog, conn, checkpoint=state_dir / "pull.json" if state_dir else None
        )
        if not result.get("unchanged_completed_publication") or not report.get("pull"):
            report["pull"] = {
                "started_at": began.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                **{k: v for k, v in result.items() if k != "inserted_ids"},
            }
        save()

    if (
        state_dir
        and (state_dir / "history.json").exists()
        and json.loads((state_dir / "history.json").read_text()).get("status") != "complete"
    ):
        raise RuntimeError("The initial history load is unfinished; resume history-load first")
    save()
    if (
        dbx is not None
        and conn.execute("SELECT NOT EXISTS(SELECT 1 FROM job_sorting.job_listings)").fetchone()[0]
    ):
        raise RuntimeError("Complete the explicitly approved history-load before normal sorting")
    pull()
    rules_start = report.get("timings", {}).get("rules_pages", {}).get("start") or datetime.now(timezone.utc).isoformat()
    re_sort(conn, preferences, prompts)
    if mode != "re-sort":
        last = None
        last_versions = source_versions(dbx, catalog) if dbx is not None else None
        last_source_poll = time.monotonic()

        def next_batch():
            nonlocal last, last_versions, last_source_poll
            if dbx is not None and not budget.reason("triage") and time.monotonic() - last_source_poll >= 60:
                last_source_poll = time.monotonic()
                versions = source_versions(dbx, catalog)
                if versions != last_versions:
                    try:
                        pull()
                    except StaleMapping:
                        log.info("New source publication is not completed; keeping the current sorting input")
                    else:
                        re_sort(conn, preferences, prompts)
                        last_versions = versions
                        last = None
            predicate = "j.wording_first_seen_at>=%s AND (j.bucket IS NULL OR j.reason=ANY(%s))"
            params = [PROCESSING_START, list(DEFERRED_REASONS)]
            if last is not None:
                predicate += " AND j.id<%s"
                params.append(last)
            rows = read_sorting_batch(conn, predicate, params, descending=True)
            if rows:
                last = rows[-1]["id"]
            return rows

        process_batch(
            conn,
            next_batch(),
            preferences,
            prompts,
            budget,
            keys,
            workers=workers,
            transport=transport,
            wait=wait,
            registry=registry,
            counters=report["counters"],
            report_lock=checkpoint_lock,
            next_batch=next_batch,
            on_completed=lambda _: save(),
        )
        # A publication may refresh facts while older reads are still in flight.
        # Reuse their valid answers against the final facts before suppression.
        re_sort(conn, preferences, prompts)
        # A signal stops scheduling batches; remaining unanswered rows stay eligible.
        if budget.stop.is_set():
            conn.execute(
                "UPDATE job_sorting.job_listings SET bucket='not_sorted',reason='not read: the night stopped early',decided_by=NULL WHERE wording_first_seen_at>=%s AND (bucket IS NULL OR reason=ANY(%s)) AND (bucket,reason,decided_by) IS DISTINCT FROM ('not_sorted','not read: the night stopped early',NULL)",
                (PROCESSING_START, list(DEFERRED_REASONS)),
            )
        # The final re-sort already checked repeats; changing an unread reason
        # leaves those rows unjudged and does not change repeat candidates.
    report.setdefault("timings", {})["rules_pages"] = {
        "start": rules_start,
        "end": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "interrupted" if budget and budget.stop.is_set() else "complete"
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
    parser.add_argument("--workers", type=int, default=32)
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
    if args.mode == "night" and args.ceiling_usd not in (None, Decimal(10)):
        parser.error("normal nights have the shared USD 10 ceiling")
    if args.workers < 1 or args.workers > 100:
        parser.error("workers must be between 1 and 100")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from bto import settings

    settings.load_postgres()
    if args.mode in {"night", "catch-up"}:
        return _batch_main(args)
    with connect() as conn, store.sorting_owner(conn):
        ensure_job_sorting_tables(conn)
        if args.mode == "report":
            from bto.send_notifications.notify import bucket_validation_report

            print(json.dumps(bucket_validation_report(conn), default=str, indent=2))
            return 0
        config = settings.load() if args.mode != "re-sort" else {}
        dbx = None
        if args.mode != "re-sort":
            from bto.storage.databricks.connection import Databricks

            dbx = Databricks(
                config["databricks_host"],
                config["databricks_token"],
                config["databricks_warehouse_id"],
            )
        if args.mode == "history-load":
            pull_job_listings(
                dbx,
                config["databricks_catalog"],
                conn,
                history=True,
                checkpoint=args.state_dir / "history.json",
            )
            return 0
        preferences = load_preferences()
        prompts = {stage: load_prompt(stage, preferences) for stage in ("triage", "extraction")}
        budget = None
        keys = {}
        if args.mode != "re-sort":
            from .call_gemini import CallBudget

            fallback = settings._dotenv()
            for stage, name in (
                ("triage", "GEMINI_TRIAGE_JOBS_API_KEY"),
                ("extraction", "GEMINI_EXTRACT_JOB_DETAILS_API_KEY"),
            ):
                keys[stage] = os.environ.get(name) or fallback.get(name)
                if not keys[stage]:
                    raise ValueError(f"Missing {name}")
            budget = CallBudget(args.mode, args.ceiling_usd or Decimal(10))
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: budget.stop.set())
        state_path = args.state_dir / ("re_sort.json" if args.mode == "re-sort" else "night.json")
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        saved_budget = previous.get("cost_budget") if previous else None
        if previous and previous.get("status") == "complete":
            previous = None
        if (
            previous
            and args.mode == "night"
            and night(datetime.fromisoformat(previous["started_at"]))
            != night(datetime.now(timezone.utc))
        ):
            previous = None
        report = previous or {"status": "running", "mode": args.mode}
        if budget and previous:
            budget.spent = max(
                Decimal(previous.get("spent_usd", "0")),
                sum(
                    (
                        r["cost_usd"]
                        for r in store.cost_totals(
                            conn, datetime.fromisoformat(previous["started_at"])
                        )
                    ),
                    Decimal(0),
                ),
            )
        if budget and args.mode == "night":
            since = datetime.combine(
                night(budget.now()), datetime.min.time(), SINGAPORE
            ) + timedelta(hours=3)
            recorded = sum((row["cost_usd"] for row in store.cost_totals(conn, since)), Decimal(0))
            budget.spent = max(budget.spent, recorded)
        if budget and saved_budget and saved_budget.get("guarded"):
            same_scope = saved_budget.get("mode") == args.mode and (
                saved_budget.get("night") == night(budget.now()).isoformat()
                if args.mode == "night"
                else previous is not None
            )
            if same_scope:
                budget.restore(saved_budget, budget.spent)
        try:
            run_sorting(
                conn,
                preferences,
                prompts,
                mode=args.mode,
                budget=budget,
                keys=keys,
                dbx=dbx,
                catalog=config.get("databricks_catalog"),
                state_dir=args.state_dir,
                workers=args.workers,
                report=report,
            )
        except Exception as error:
            report.update(
                status="failed",
                error=type(error).__name__,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            write_checkpoint(state_path, report)
            log.error("sorting run failed error_type=%s", type(error).__name__)
        from bto.send_notifications.notify import (
            build_morning_context,
            render_morning_email,
            send_email,
        )

        context = build_morning_context(conn, report, budget, preferences, config=config, dbx=dbx)
        subject, plain, html = render_morning_email(context)
        if args.send_email and args.mode != "re-sort":
            delivered = send_email(config, subject, plain, html=html)
            if delivered and report["status"] != "complete":
                return 5
        else:
            args.state_dir.mkdir(parents=True, exist_ok=True)
            (args.state_dir / "morning_email.html").write_text(html)
            (args.state_dir / "morning_email.txt").write_text(plain)
        return 0 if report["status"] == "complete" else 1


def _batch_main(args):
    """Normal paid execution uses durable two-stage Batch, without a clock gate."""
    from bto import settings
    from bto.send_notifications import notify
    from bto.storage.databricks.connection import Databricks
    from bto.storage.databricks.freshness import require_current_mapping

    from .call_gemini_batch import save
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
        return {"versions": versions, "first_id": first_id, "pull": {
            **{k: v for k, v in pulled.items() if k != "inserted_ids"},
            "started_at": began, "finished_at": datetime.now(timezone.utc).isoformat()}}
    def fresh_estimate(items):
        with connect() as conn:
            return estimate(conn, items, prompts, preferences, keys)
    publish_review = None
    if getattr(args, "refresh_review", False):
        from bto.review_jds.refresh_review_cache import configuration, refresh

        refresh_config = configuration()

        def publish_review():
            saved = json.loads((args.state_dir / "batch.json").read_text())
            require_current_mapping(dbx, config["databricks_catalog"])
            if source_versions(dbx, config["databricks_catalog"]) != saved["source"]:
                raise StaleMapping("Source changed before Review refresh; reconcile before publication")
            return refresh(refresh_config)

    try:
        report = run(preferences, prompts, keys, state_dir=args.state_dir,
            ceiling=args.ceiling_usd or Decimal(10), estimate=fresh_estimate,
            owner_dir=root_state, reconcile=reconcile, estimate_basis=args.estimate_basis,
            publish_review=publish_review)
    except Exception as error:
        path = args.state_dir / "batch.json"
        if not path.exists():
            path = args.state_dir / "preflight-failure.json"
        report = json.loads(path.read_text()) if path.exists() else {
            "started_at": datetime.now(timezone.utc).isoformat(), "failed_stage": "preparation"}
        report.update(status="failed", error=str(error), error_type=type(error).__name__)
        save(args.state_dir / "batch-failure.json", report)
    if report.get("status") == "complete":
        email_report = dict(report)
        email_report["usage_totals"] = {identity: receipt for identity, receipt in report.get("receipts", {}).items()}
        counters = {}
        for stage, jobs in report.get("jobs", {}).items():
            counters[stage + "_sent"] = sum(j["item_count"] for j in jobs)
            counters[stage + "_half_listings"] = counters[stage + "_sent"]
            counters[stage + "_retried"] = sum(i["answer_attempts"] > 1 for i in report["items"][stage].values())
            counters[stage + "_no_answer"] = sum(i.get("outcome") == "terminal" for i in report["items"][stage].values())
            if jobs:
                counters[stage + "_start"] = min(j["submission_at"] for j in jobs)
                counters[stage + "_end"] = max(j["ingested_at"] for j in jobs)
        email_report["counters"] = counters
        with connect() as conn:
            report["email_context"] = notify.build_morning_context(conn, email_report, None, preferences,
                config=config, dbx=dbx)
        save(args.state_dir / "batch.json", report)
    subject, plain, html = notify.render_terminal_nightly({
        "status": report["status"], "started_at": report["started_at"], "sorting": report,
        "failed_stage": report.get("failed_stage"), "error": report.get("error")})
    if args.send_email:
        # A scheduled full chain owns its one terminal message in bto.nightly.
        if not config.get("combined_nightly_email"):
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
    except store.SortingBusy:
        log.info("Sorting already has an owner; this runner has no work")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
