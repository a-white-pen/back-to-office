"""Run collection, preflight checks and manual collection summaries.

Manual summaries use the exact run IDs in the saved collection handoff.
The nightly workflow owns its terminal email and downstream processing.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import settings as settings_module
from .log_errors import log_failure
from .send_notifications import notify
from .storage.databricks.connection import Databricks, DatabricksError, table_name

log = logging.getLogger("bto")

SGT = timezone(timedelta(hours=8))


def _configure_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _selected_pairs(args):
    """Select enabled board and market pairs, or an explicit supported pair."""
    if args.board and args.market:
        pair = (args.board, args.market)
        if pair not in settings_module.supported_runs():
            raise SystemExit(f"{args.board} × {args.market} is not a "
                             f"supported pairing")
        return [pair]
    pairs = settings_module.enabled_runs()
    if args.board:
        pairs = [p for p in pairs if p[0] == args.board]
    if args.market:
        pairs = [p for p in pairs if p[1] == args.market]
    if not pairs:
        raise SystemExit("no enabled board × market runs match the filter")
    return pairs


def cmd_collect(args):
    """Collect the selected sources and send a summary for a manual run."""
    from .fetch_job_listings import run as run_module

    settings = settings_module.load()
    pairs = _selected_pairs(args)
    results = run_module.collect_all(settings, pairs, args.trigger)
    # The nightly owner sends the scheduled terminal email. Manual collection
    # retains its explicit handoff-scoped summary.
    if args.trigger != "scheduled":
        summarise_execution(settings)
    # Returning from collect_all is the cleaning gate: its finished handoff
    # makes landed Bronze safe to consume. Provider failures stay visible in
    # their outcomes and reports but do not change this process exit status.
    counts = {}
    for outcome in results.values():
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    log.info("collection reached a safe terminal state (%s); downstream "
             "cleaning may consume Bronze",
             ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_preflight(args):
    from .fetch_job_listings import run as run_module

    settings = settings_module.load()
    return 0 if run_module.preflight(settings) else 1


def summarise_execution(settings, handoff_path=None):
    """Email the run IDs in one handoff; refuse an unknown execution scope."""
    from .fetch_job_listings import run as run_module

    path = handoff_path or settings["run_handoff_path"]
    try:
        handoff = run_module.read_handoff(path)
    except FileNotFoundError:
        log.error("no fetch execution to summarise: %s does not exist", path)
        return False

    run_ids = handoff.get("run_ids") or []
    if not run_ids:
        log.error("collection summary not sent: %s has no saved run IDs; "
                  "this execution's results are unknown", path)
        return False

    catalog = settings["databricks_catalog"]
    # Report a warehouse refusal promptly instead of waiting to build the summary.
    dbx = Databricks(settings["databricks_host"], settings["databricks_token"],
                     settings["databricks_warehouse_id"], refusal_retries=0)
    interrupted = handoff.get("state") != "finished"
    try:
        started_sgt = datetime.fromisoformat(handoff["started_at"]) \
            .astimezone(SGT).strftime("%a %d %b %Y %H:%M SGT")
    except (KeyError, ValueError):
        started_sgt = "unknown start"
    label = f"{handoff.get('trigger', '?')} fetch started {started_sgt}"
    if interrupted:
        label += " (fetch did not record a clean finish)"
    select = (
        "SELECT run_id, board, market, started_at, finished_at, status,\n"
        "       unique_seen, new_jobs, changed_jobs, jds_fetched,\n"
        "       fetch_failures, backlog_remaining, error_reason\n"
        f"FROM {table_name(catalog, 'scrape_runs')}\n")
    try:
        # run_ids are generated internally ([a-z0-9_] only) — safe literals.
        id_list = ", ".join(f"'{run_id}'" for run_id in run_ids)
        raw_rows = dbx.query(select + f"WHERE run_id IN ({id_list})\n"
                                      "ORDER BY started_at")
    except DatabricksError as failure:
        # Standalone summary errors stay in the log. Nightly failure reporting
        # belongs to the terminal email, not a separate collection alert.
        from .send_notifications.nightly_email import error_explanation

        reason = error_explanation(failure) or "See source run records"
        log_failure(log, "fetch summary unavailable for the %s reason=%s",
                    label, reason, error=failure)
        return False

    def _int(value):
        return None if value is None else int(value)

    def _runtime(started, finished):
        if not started or not finished:
            return None
        try:
            start = datetime.fromisoformat(started.replace("Z", "+00:00"))
            end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        except ValueError:
            return None
        seconds = int((end - start).total_seconds())
        return f"{seconds // 60}m{seconds % 60:02d}s"

    rows = []
    seen_pairs = set()
    for (run_id, board, market, started_at, finished_at, status, unique_seen,
         new_jobs, changed_jobs, jds_fetched, fetch_failures,
         backlog_remaining, error_reason) in raw_rows:
        seen_pairs.add(f"{board}_{market}")
        rows.append({
            "run_id": run_id, "board": board, "market": market,
            "status": status,
            "unique_seen": _int(unique_seen), "new_jobs": _int(new_jobs),
            "changed_jobs": _int(changed_jobs), "jds_fetched": _int(jds_fetched),
            "fetch_failures": _int(fetch_failures),
            "backlog_remaining": _int(backlog_remaining),
            "error_reason": error_reason,
            "runtime": _runtime(started_at, finished_at),
        })

    missing = [tuple(pair.split("_", 1)) for pair in handoff.get("pairs", [])
               if pair not in seen_pairs]
    log.info("summarising execution: %s — %d runs, %d intended pairs missing",
             label, len(rows), len(missing))
    return notify.send_summary(settings, rows, missing, label)


def cmd_summarise(args):
    settings = settings_module.load()
    sent = summarise_execution(settings, handoff_path=args.handoff)
    return 0 if sent else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bto")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="run nightly collection")
    collect.add_argument("--board", help="limit to one board slug")
    collect.add_argument("--market", help="limit to one market slug")
    collect.add_argument("--trigger", default="manual",
                         choices=("scheduled", "manual", "recovery", "backfill"),
                         help="scrape_runs.run_trigger provenance")
    collect.set_defaults(func=cmd_collect)

    summarise = commands.add_parser(
        "summarise",
        help="email the summary for the last fetch execution (handoff-scoped)")
    summarise.add_argument("--handoff", default=None,
                           help="path to a fetch-execution handoff file "
                                "(default: the configured RUN_HANDOFF_PATH)")
    summarise.set_defaults(func=cmd_summarise)

    preflight = commands.add_parser("preflight",
                                    help="check collection dependencies, collect nothing")
    preflight.set_defaults(func=cmd_preflight)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
