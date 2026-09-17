"""CLI entrypoints: python -m bto {collect | summarise | preflight}.

Called by:
    the systemd units on the Lightsail box (fetch-job-listings.timer at
    03:30 SGT; send-job-listings-summary-email chained after each fetch
    execution), and by hand.

Calls:
    settings, fetch_job_listings.run, send_notifications.notify,
    storage.databricks

    collect     run enabled board × market pairs (or one, via --board/--market)
    summarise   email the summary for the fetch execution recorded in the
                handoff file (data/last_fetch.json)
    preflight   prove every dependency of the unattended run, collect nothing

One summary per fetch execution, scoped to exactly the runs that execution
launched — never "everything from today". The scheduled path leaves the
summary to the systemd chain (fetch-job-listings.service fires
send-job-listings-summary-email.service on completion, success or failure);
a manual collect sends its own summary as it finishes. Both use the same
summary code and format.

Logging goes to stdout/stderr in plain lines; systemd forwards both to
journald. There is no logging framework of our own.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import settings as settings_module
from .fetch_job_listings import run as run_module
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
    """The board × market pairs this invocation runs: all enabled ones by
    default, narrowed by --board/--market. Naming a supported-but-disabled
    pair explicitly (both flags) runs it — that is the manual override."""
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
    settings = settings_module.load()
    pairs = _selected_pairs(args)
    results = run_module.collect_all(settings, pairs, args.trigger)
    # A scheduled invocation leaves the summary to the systemd chain
    # (OnSuccess=/OnFailure= on fetch-job-listings.service), so it is sent
    # exactly once. A manual invocation has no chain, so it summarises itself.
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
    settings = settings_module.load()
    return 0 if run_module.preflight(settings) else 1


def summarise_execution(settings, handoff_path=None):
    """Email the summary for ONE fetch execution — the one recorded in the
    handoff file. Scoped to that execution's own run_ids (or, if the fetch
    never recorded a clean finish, to its trigger + pairs + start time), so
    a scheduled summary never includes smoke runs and a smoke summary never
    includes scheduled ones."""
    path = handoff_path or settings["run_handoff_path"]
    try:
        handoff = run_module.read_handoff(path)
    except FileNotFoundError:
        log.error("no fetch execution to summarise: %s does not exist", path)
        return False

    catalog = settings["databricks_catalog"]
    # No waiting out a warehouse refusal here: this unit is allowed 15
    # minutes, so a 35-minute wait would be killed before it could email, and
    # the collection it summarises has already waited out any refusal.
    dbx = Databricks(settings["databricks_host"], settings["databricks_token"],
                     settings["databricks_warehouse_id"], refusal_retries=0)
    run_ids = handoff.get("run_ids") or []
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
        if run_ids and not interrupted:
            # run_ids are generated internally ([a-z0-9_] only) — safe literals.
            id_list = ", ".join(f"'{run_id}'" for run_id in run_ids)
            raw_rows = dbx.query(select + f"WHERE run_id IN ({id_list})\n"
                                          "ORDER BY started_at")
        else:
            # The fetch died before recording its run ids. Scope by what the
            # execution declared up front: its trigger, its pairs, its start.
            pair_list = ", ".join(f"'{pair}'"
                                  for pair in handoff.get("pairs", []))
            raw_rows = dbx.query(
                select
                + "WHERE run_trigger = :trigger\n"
                "  AND started_at >= CAST(:started AS TIMESTAMP)\n"
                f"  AND concat(board, '_', market) IN ({pair_list})\n"
                "ORDER BY started_at",
                parameters={
                    "trigger": handoff.get("trigger") or "scheduled",
                    "started": (handoff.get("started_at") or "")
                    .replace("T", " ").split("+")[0],
                })
    except DatabricksError as failure:
        # The summary is built from Databricks, so an outage used to crash
        # this command before it emailed anything. Say so instead; the exit
        # stays non-zero because no summary was produced.
        log.error("fetch summary unavailable for the %s: %s", label, failure)
        notify.send_summary_unavailable(settings, label, failure)
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
                                    help="verify every dependency, collect nothing")
    preflight.set_defaults(func=cmd_preflight)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
