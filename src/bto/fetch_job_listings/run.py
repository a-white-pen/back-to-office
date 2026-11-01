"""Collect each board and market while preserving raw responses and run outcomes.

Card-based sources fetch new or changed job descriptions after the search sweep.
Actor-based sources return descriptions with their results. Each run records
its own progress and failures; one failed source does not stop the others.
"""

import hashlib
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import settings as settings_module
from ..log_errors import log_failure
from ..storage.databricks import read as dbx_read
from ..storage.databricks import write as dbx_write
from ..storage.databricks.connection import (
    JOB_LISTINGS_VOLUME,
    SEARCH_RESULTS_VOLUME,
    Databricks,
    table_name,
    volume_path,
)
from ..storage.postgres import setup as pg_setup
from ..storage.postgres import write as pg_write
from .apify_client import ApifyClient
from .indeed import fetch as indeed_fetch
from .indeed import parse as indeed_parse
from .linkedin import fetch as linkedin_fetch
from .linkedin import parse as linkedin_parse
from .mcf import fetch as mcf_fetch
from .mcf import parse as mcf_parse
from .seek import fetch as seek_fetch
from .seek import parse as seek_parse

log = logging.getLogger(__name__)

SGT = timezone(timedelta(hours=8))          # Singapore never observes DST
CARD_BOARDS = ("mcf", "jobstreet", "jobsdb", "seek")

# Detail-fetch circuit breaker: the board is misbehaving, stop hammering it.
FETCH_FAILURE_ABORT_RATIO = 0.30
MIN_FETCHES_BEFORE_ABORT = 20
# Sweep tolerance: single terms may fail (partial beats failed), but a board
# failing this share of its terms is broken, not unlucky.
TERM_FAILURE_ABORT_RATIO = 0.30
OBSERVATION_FLUSH_ROWS = 400

# Limit concurrent raw-file uploads across parallel source runs.
# Bronze table writes remain batched and sequential within each run.
ACTOR_UPLOAD_WORKERS = 8      # actor boards: pure upload phase, nothing else
CARD_UPLOAD_WORKERS = 4       # card boards: uploads overlap the polite fetch loop


class AbnormalSourceError(RuntimeError):
    """The board's behaviour or volume is outside anything normal; the run
    stops safely, keeps the work already landed, and notifies."""


def slugify(value):
    """Lowercase ascii slug for filenames. A non-Latin term hashes instead of
    collapsing to a shared placeholder — two terms must never share a name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        slug = "t" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return slug


def new_run_id(board, market, now_utc=None):
    """Create a source run ID with a Singapore timestamp and random suffix."""
    now_utc = now_utc or datetime.now(timezone.utc)
    stamp = now_utc.astimezone(SGT).strftime("%Y%m%d_%H%M%S")
    return f"{board}_{market}_{stamp}_{secrets.token_hex(2)}"


# ── the fetch-execution handoff file ────────────────────────────────────────
# Save the intended source pairs before collection and the exact run IDs at
# completion. Nightly processing and manual summaries use this execution scope.

def write_handoff(path, payload):
    """Replace the execution handoff atomically for downstream readers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    temporary.replace(path)      # atomic: the chain never reads a half-write


def read_handoff(path):
    """Read the saved scope and outcome of one collection execution."""
    return json.loads(Path(path).read_text())


# ── orchestration ───────────────────────────────────────────────────────────
def collect_all(settings, pairs, run_trigger):
    """Collect source pairs in parallel and save their exact run IDs in the handoff."""
    log.info("collection starting: %d runs, max %d in parallel — %s",
             len(pairs), settings_module.MAX_PARALLEL_RUNS,
             ", ".join(f"{b}_{m}" for b, m in pairs))
    started_at = datetime.now(timezone.utc)
    handoff_path = settings["run_handoff_path"]
    write_handoff(handoff_path, {
        "state": "started",
        "trigger": run_trigger,
        "started_at": started_at.isoformat(timespec="seconds"),
        "pairs": [f"{board}_{market}" for board, market in pairs],
        "run_ids": [],
    })

    results = {}
    with ThreadPoolExecutor(
            max_workers=settings_module.MAX_PARALLEL_RUNS) as pool:
        futures = {
            pool.submit(collect_one, settings, board, market, run_trigger):
                (board, market)
            for board, market in pairs
        }
        for future, pair in futures.items():
            try:
                results[pair] = future.result()
            except Exception as error:
                # Source failures are handled inside collect_one.
                log_failure(log, "launcher failure for %s_%s", *pair, error=error)
                results[pair] = {"status": "failed", "run_id": None}

    write_handoff(handoff_path, {
        "state": "finished",
        "trigger": run_trigger,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs": [f"{board}_{market}" for board, market in pairs],
        "run_ids": [outcome["run_id"] for outcome in results.values()
                    if outcome.get("run_id")],
    })
    counts = {}
    for outcome in results.values():
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    log.info("collection finished: %s",
             ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results


def collect_one(settings, board, market, run_trigger):
    """Collect one source and close its run record, retaining any failure outcome."""
    started_at = datetime.now(timezone.utc)
    run_id = new_run_id(board, market, started_at)
    run_log = logging.getLogger(f"bto.run.{board}_{market}")
    run_log.info("run %s starting (trigger=%s)", run_id, run_trigger)

    dbx = Databricks(settings["databricks_host"], settings["databricks_token"],
                     settings["databricks_warehouse_id"])
    catalog = settings["databricks_catalog"]
    alerts = []          # [(cause, detail)] retained in the journal and run records

    try:
        dbx_write.insert_running_run(dbx, catalog, run_id, board, market,
                                     started_at, run_trigger)
    except Exception as error:
        log_failure(run_log, "could not open scrape_runs row run_id=%s", run_id, error=error)
        alerts.append(("failed", f"could not open the scrape_runs row: {error}"))
        return {"status": "failed", "run_id": run_id}

    outcome = {
        "status": "failed", "counters": {}, "term_results": {},
        "full_sweep": False, "error_reason": None,
    }
    try:
        if board in CARD_BOARDS:
            outcome = _collect_cards(dbx, settings, board, market, run_id,
                                     run_log, alerts)
        elif board == "indeed":
            outcome = _collect_indeed(dbx, settings, market, run_id,
                                      run_log, alerts)
        elif board == "linkedin":
            outcome = _collect_linkedin(dbx, settings, market, run_id,
                                        run_log, alerts)
        else:
            raise ValueError(f"unknown board {board!r}")
    except AbnormalSourceError as error:
        run_log.error("run %s stopped on abnormal source behaviour: %s",
                      run_id, error)
        outcome["status"] = "failed"
        outcome["error_reason"] = f"abnormal source: {error}"
        alerts.append(("Abnormal volume", str(error)))
    except Exception as error:
        log_failure(run_log, "run %s died", run_id, error=error)
        outcome["status"] = "failed"
        outcome["error_reason"] = f"{type(error).__name__}: {error}"

    finished_at = datetime.now(timezone.utc)
    try:
        dbx_write.finish_run(
            dbx, catalog, run_id,
            finished_at_utc=finished_at,
            status=outcome["status"],
            counters=outcome["counters"],
            term_results=outcome["term_results"],
            full_sweep=outcome["full_sweep"],
            error_reason=outcome["error_reason"],
        )
    except Exception as error:
        # The row stays `running` — durable evidence of a run that never
        # closed. The alert is the operator's signal to look.
        log_failure(run_log, "could not close scrape_runs row run_id=%s", run_id, error=error)
        outcome["status"] = "failed"
        alerts.append(("failed",
                       f"run finished but its scrape_runs row could not be "
                       f"updated and remains `running`: {error}"))

    status = outcome["status"]
    if status == "failed" and not any(cause == "failed" for cause, _ in alerts):
        alerts.insert(0, ("failed", outcome["error_reason"] or "the run died"))
    elif status == "partial" and not any(cause == "partial"
                                         for cause, _ in alerts):
        # Fallback only — a partial that carries its own explanation (failed
        # search terms, failed JD fetches, the breaker) must not be fronted
        # with this generic JD sentence, which would misdescribe it.
        alerts.insert(0, ("partial",
                          "The run finished but some job descriptions did not "
                          "download; counters and outstanding work below are "
                          "known and will be retried by the nightly diff."))
    # Alert details can contain provider data; retain only safe context in logs.
    for cause, _detail in alerts:
        note = "; fetch-journal evidence may be incomplete" if cause == "Evidence preservation" else ""
        run_log.warning("collection issue: %s: run_id=%s%s", cause, run_id, note)

    elapsed = (finished_at - started_at).total_seconds()
    counters = outcome["counters"]
    run_log.info(
        "run %s done in %.1f min — status=%s seen=%s new=%s changed=%s "
        "fetched=%s failed=%s deferred=%s",
        run_id, elapsed / 60, status,
        counters.get("unique_seen"), counters.get("new_jobs"),
        counters.get("changed_jobs"), counters.get("jds_fetched"),
        counters.get("fetch_failures"), counters.get("backlog_remaining"))
    return {"status": status, "run_id": run_id}




# ── card boards: MCF + SEEK family ──────────────────────────────────────────
def _card_adapter(board, market):
    if board == "mcf":
        return mcf_fetch.adapter(market), mcf_parse
    return seek_fetch.adapter(board, market), seek_parse


def _collect_cards(dbx, settings, board, market, run_id, run_log, alerts):
    """Sweep → diff against prior Bronze state → fetch → land. The one loop
    both MCF and the SEEK family run through; the adapter owns the board's
    endpoints, validation and change signal."""
    adapter, parse = _card_adapter(board, market)
    catalog = settings["databricks_catalog"]
    pause = settings["request_pause_s"]
    cap = settings["max_jd_fetches_per_run"]

    terms = adapter.terms
    smoke = bool(settings["terms_limit"])
    if smoke:
        terms = terms[:settings["terms_limit"]]
        run_log.warning("SMOKE TEST: sweep truncated to %d terms — "
                        "full_sweep will be FALSE", len(terms))

    # 1. prior state — one short batched read, then local comparisons only
    prior = dbx_read.load_prior_state(dbx, catalog, board, market)

    # 2. sweep every term, landing every page verbatim as it arrives
    sightings = {}       # board_job_id -> tonight's change_signal (first seen)
    term_results = {}
    failed_terms = 0
    failed_term_names = []
    for index, term in enumerate(terms, 1):
        try:
            total, pages, hits = _sweep_term(
                dbx, catalog, adapter, run_id, term, pause, sightings)
            term_results[term] = {"total": total, "pages": pages, "hits": hits}
            run_log.info("[%d/%d] %-28s total=%-6s pages=%-3d unique so far=%d",
                         index, len(terms), term, total, pages, len(sightings))
        except AbnormalSourceError:
            raise
        except Exception as error:
            failed_terms += 1
            failed_term_names.append(term)
            term_results[term] = {"error": f"{type(error).__name__}: {error}"[:300]}
            log_failure(run_log, "term %r failed run_id=%s failures=%d",
                        term, run_id, failed_terms, error=error, level=logging.WARNING)
            if failed_terms / len(terms) > TERM_FAILURE_ABORT_RATIO:
                raise AbnormalSourceError(
                    f"{failed_terms}/{len(terms)} search terms failed — "
                    "the board is broken, not unlucky") from error
        if len(sightings) > adapter.max_plausible_unique:
            raise AbnormalSourceError(
                f"{len(sightings)} unique jobs mid-sweep exceeds the "
                f"{adapter.max_plausible_unique} ceiling")

    if not smoke and len(sightings) < adapter.min_plausible_unique:
        # A garbage sweep must never be written as healthy evidence.
        raise AbnormalSourceError(
            f"only {len(sightings)} unique jobs across the full sweep "
            f"(floor {adapter.min_plausible_unique}) — refusing to trust it")

    # 3. diff against prior state, locally
    new_ids, changed_ids, unresolved_ids, unchanged_ids = [], [], [], []
    for job_id, signal in sightings.items():
        prior_state = prior.get(job_id)
        if prior_state is None:
            new_ids.append(job_id)
        elif (signal or "") != (prior_state.change_signal or ""):
            changed_ids.append(job_id)
        elif prior_state.content_hash is None or not prior_state.has_payload:
            # Seen before, still fetch-required: either no payload was captured,
            # or the prior row carries a hash without its payload columns and
            # copying it forward would perpetuate the gap.
            unresolved_ids.append(job_id)
        else:
            unchanged_ids.append(job_id)
    run_log.info("diff: new=%d changed=%d unresolved=%d unchanged=%d",
                 len(new_ids), len(changed_ids), len(unresolved_ids),
                 len(unchanged_ids))

    # 4. select fetches: freshest first so a capped run covers tonight's
    #    movement; anything past the cap is deferred, never discarded
    candidates = sorted(new_ids + changed_ids + unresolved_ids,
                        key=lambda job_id: sightings[job_id] or "",
                        reverse=True)
    targets = candidates[:cap]
    deferred_ids = candidates[cap:]
    if deferred_ids:
        run_log.warning("fetch cap %d reached: %d candidates deferred",
                        cap, len(deferred_ids))
        alerts.append(("Cap reached",
                       f"{len(candidates)} full-JD candidates exceeded the "
                       f"{cap} cap; {len(deferred_ids)} deferred to later "
                       f"runs. Reaching the cap is abnormal."))

    # 5. fetch full JDs and land observations incrementally. Detail fetches
    #    stay sequential and polite towards the board; the payload uploads to
    #    raw_job_listings ride a small bounded pool so the fetch pacing, not
    #    Databricks round-trips, sets the run's speed.
    stats = {"fetched": 0, "failures": 0}
    jd_decisions = []                       # jd_fetches lines, all outcomes
    payloads_seen = set()
    pending_rows = []
    in_flight = []       # (job_id, signal, content_hash, payload_dict, future)

    def flush_rows():
        if pending_rows:
            dbx_write.insert_observations(dbx, catalog, board, pending_rows)
            pending_rows.clear()

    def resolve_uploads(wait):
        """Fold finished payload uploads into rows and jd_fetches decisions.
        A job counts as fetched only once its payload file has landed; an
        upload that fails leaves the observation with content_hash NULL, so
        the table never points at a file that is not there."""
        remaining = []
        for entry in in_flight:
            upload_job_id, upload_signal, upload_hash, payload, future = entry
            if not wait and not future.done():
                remaining.append(entry)
                continue
            try:
                future.result()
            except Exception as error:
                stats["failures"] += 1
                jd_decisions.append((upload_job_id, upload_signal, "failed", None))
                pending_rows.append(parse.observation_row(
                    *_row_key(board, market, upload_job_id, run_id),
                    change_signal=upload_signal, content_hash=None,
                    payload=None))
                log_failure(run_log, "payload upload failed run_id=%s board_job_id=%s",
                            run_id, upload_job_id, error=error, level=logging.WARNING)
            else:
                stats["fetched"] += 1
                jd_decisions.append(
                    (upload_job_id, upload_signal, "fetched", upload_hash))
                pending_rows.append(parse.observation_row(
                    *_row_key(board, market, upload_job_id, run_id),
                    change_signal=upload_signal, content_hash=upload_hash,
                    payload=payload))
        in_flight[:] = remaining

    breaker_deferred = 0
    journal_written = False

    def write_jd_journal():
        """One whole-file write of the run's detail-fetch decisions — the
        same builder for the normal path and the failure-recovery path, so
        both produce identical, contract-shaped files. The flag plus the
        overwrite-PUT (an atomic whole-file replace) make a second call
        impossible to turn into duplicate or mixed journal state."""
        nonlocal journal_written
        if journal_written:
            return
        jd_lines = "".join(
            json.dumps({"board_job_id": job_id, "change_signal": signal,
                        "fetch_outcome": outcome, "content_hash": content_hash},
                       ensure_ascii=False, sort_keys=True) + "\n"
            for job_id, signal, outcome, content_hash in sorted(jd_decisions))
        dbx_write.write_search_artifact(
            dbx, catalog, f"{run_id}_{board}_{market}_jd_fetches.jsonl.gz",
            jd_lines.encode())
        journal_written = True

    fetch_started = time.monotonic()
    upload_pool = ThreadPoolExecutor(max_workers=CARD_UPLOAD_WORKERS)
    try:
        try:
            for position, job_id in enumerate(targets, 1):
                signal = sightings[job_id]
                try:
                    raw = adapter.fetch_detail(job_id, pause)
                except Exception as error:
                    stats["failures"] += 1
                    jd_decisions.append((job_id, signal, "failed", None))
                    # The observation survives; the old hash is NOT reused for
                    # a changed/unfetched version.
                    pending_rows.append(parse.observation_row(
                        *_row_key(board, market, job_id, run_id),
                        change_signal=signal, content_hash=None, payload=None))
                    log_failure(run_log, "detail fetch failed run_id=%s board_job_id=%s",
                                run_id, job_id, error=error, level=logging.WARNING)
                    if (position >= MIN_FETCHES_BEFORE_ABORT
                            and stats["failures"] / position
                            > FETCH_FAILURE_ABORT_RATIO):
                        breaker_deferred = len(targets) - position
                        for remaining_id in targets[position:]:
                            jd_decisions.append((remaining_id,
                                                 sightings[remaining_id],
                                                 "deferred", None))
                            pending_rows.append(parse.observation_row(
                                *_row_key(board, market, remaining_id, run_id),
                                change_signal=sightings[remaining_id],
                                content_hash=None, payload=None))
                        alerts.append(("partial",
                                       f"detail-fetch circuit breaker tripped: "
                                       f"{stats['failures']}/{position} fetches "
                                       f"failed; {breaker_deferred} remaining "
                                       f"candidates deferred"))
                        break
                    continue

                content_hash = hashlib.sha256(raw).hexdigest()
                in_flight.append((job_id, signal, content_hash, json.loads(raw),
                                  upload_pool.submit(
                                      dbx_write.write_payload, dbx, catalog,
                                      content_hash, raw, _seen=payloads_seen)))
                resolve_uploads(wait=False)
                if len(pending_rows) >= OBSERVATION_FLUSH_ROWS:
                    flush_rows()
                if position % 100 == 0 or position == len(targets):
                    run_log.info("fetched %d/%d JDs (%d failed)",
                                 position, len(targets), stats["failures"])
            resolve_uploads(wait=True)
        finally:
            upload_pool.shutdown(wait=True)
        if targets:
            elapsed = time.monotonic() - fetch_started
            run_log.info("JD fetch+upload phase: %d landed in %.0fs "
                         "(%.2f/s, %d upload workers)",
                         stats["fetched"], elapsed,
                         stats["fetched"] / elapsed if elapsed else 0.0,
                         CARD_UPLOAD_WORKERS)

        for job_id in deferred_ids:
            jd_decisions.append((job_id, sightings[job_id], "deferred", None))
            pending_rows.append(parse.observation_row(
                *_row_key(board, market, job_id, run_id),
                change_signal=sightings[job_id], content_hash=None,
                payload=None))
        flush_rows()

        # 6. unchanged observations: same payload version, so the prior row is
        #    re-stated server-side — zero detail requests, zero downloads
        unchanged_pairs = [(job_id, prior[job_id].run_id)
                           for job_id in unchanged_ids]
        dbx_write.copy_unchanged_observations(dbx, catalog, board, market,
                                              run_id, unchanged_pairs)
        for job_id in unchanged_ids:
            jd_decisions.append((job_id, sightings[job_id], "unchanged",
                                 prior[job_id].content_hash))

        # 7. the run's detail-fetch decisions, one line per distinct observed job
        write_jd_journal()
        run_log.info("jd_fetches: %d decisions (%d fetched, %d unchanged, "
                     "%d deferred, %d failed)", len(jd_decisions),
                     stats["fetched"], len(unchanged_ids),
                     len(deferred_ids) + breaker_deferred, stats["failures"])
    except Exception:
        # Evidence preservation: a Bronze/write failure fails the run (the
        # original exception is re-raised untouched below), but it must not
        # also take the detail-fetch journal down with it — payloads already
        # captured would otherwise sit in raw_job_listings with no jd_fetches
        # record of what this run fetched. Best-effort, and never allowed to
        # mask the original failure.
        try:
            # The pool is already shut down (the finally above waited), so
            # every submitted upload is terminal: resolve_uploads records the
            # completed ones as fetched and the failed ones as failed — an
            # upload that never finished cleanly is never journaled as a
            # successful fetch.
            resolve_uploads(wait=True)
        except Exception as resolve_error:
            log_failure(run_log, "could not resolve uploads during journal recovery run_id=%s",
                        run_id, error=resolve_error)
        if jd_decisions and not journal_written:
            try:
                write_jd_journal()
                run_log.error(
                    "write failure run_id=%s; jd_fetches journal preserved with "
                    "%d completed decisions before failing the run",
                    run_id, len(jd_decisions))
            except Exception as journal_error:
                log_failure(run_log, "jd_fetches journal preservation also failed run_id=%s",
                            run_id, error=journal_error)
                alerts.append((
                    "Evidence preservation",
                    f"the write failure also prevented preserving the "
                    f"jd_fetches journal ({journal_error}); fetch-journal "
                    f"evidence for this run may be incomplete. Payloads that "
                    f"did upload remain content-addressed in "
                    f"raw_job_listings."))
        raise

    backlog = len(deferred_ids) + breaker_deferred
    # A term that failed after its request-level retries means the sweep did
    # not cover the configured search space — the run is partial, never ok,
    # even though everything the other terms collected is valid and kept.
    status = ("partial" if (stats["failures"] or backlog or failed_terms)
              else "ok")
    if failed_term_names:
        run_log.warning("search coverage incomplete run_id=%s: %d of %d search terms failed",
                        run_id, failed_terms, len(terms))
        shown = ", ".join(repr(term) for term in failed_term_names[:8])
        if len(failed_term_names) > 8:
            shown += f" and {len(failed_term_names) - 8} more"
        alerts.append(("partial",
                       f"search coverage incomplete: {failed_terms} of "
                       f"{len(terms)} search terms failed after retries "
                       f"({shown}); jobs matching only those terms are "
                       f"missing from this run's sweep and diff. Everything "
                       f"the other terms collected landed normally."))
    if stats["failures"]:
        alerts.append(("partial",
                       f"{stats['failures']} of {len(targets)} selected "
                       f"full-JD fetches did not land; their observations "
                       f"survive with a NULL content_hash and stay "
                       f"fetch-required."))
    return {
        "status": status,
        "counters": {
            "terms_swept": len(terms),
            "terms_succeeded": len(terms) - failed_terms,
            "unique_seen": len(sightings),
            "new_jobs": len(new_ids),
            "changed_jobs": len(changed_ids),
            "jds_intended": len(targets),
            "jds_fetched": stats["fetched"],
            "fetch_failures": stats["failures"],
            "backlog_remaining": backlog,
        },
        "term_results": term_results,
        # full_sweep is about search-space coverage: a truncated smoke sweep
        # or failed terms mean absence from this run proves nothing.
        "full_sweep": not smoke and failed_terms == 0,
        "error_reason": (f"{failed_terms} search terms failed"
                         if failed_terms else None),
    }


def _row_key(board, market, job_id, run_id):
    """Positional prefix for parse.observation_row: MCF's signature has no
    board argument (it is Singapore-only MCF by construction)."""
    if board == "mcf":
        return (market, job_id, run_id)
    return (board, market, job_id, run_id)


def _sweep_term(dbx, catalog, adapter, run_id, term, pause, sightings):
    """Sweep one term page by page (re-swept per partition where the market
    defines them), landing every page verbatim before its hits are read."""
    first = adapter.search_page(term, adapter.first_page, pause)
    if term in adapter.canary_terms and first.total == 0:
        raise AbnormalSourceError(
            f"canary term {term!r} returned total=0 — the API is broken")

    partitions = ()
    if hasattr(adapter, "partitions_for"):
        partitions = adapter.partitions_for(first.total)

    if not partitions:
        pages, hits = _sweep_pages(dbx, catalog, adapter, run_id, term, pause,
                                   sightings, first=first)
        return first.total, pages, hits

    # Oversize term (AU): land the probe page, then re-sweep per state so no
    # partition exceeds the platform's paging ceiling.
    _land_page(dbx, catalog, adapter, run_id, f"{term} probe",
               adapter.first_page, first.raw)
    total_pages, total_hits = 1, 0
    for partition in partitions:
        pages, hits = _sweep_pages(dbx, catalog, adapter, run_id, term, pause,
                                   sightings, partition=partition)
        total_pages += pages
        total_hits += hits
    return first.total, total_pages, total_hits


def _sweep_pages(dbx, catalog, adapter, run_id, term, pause, sightings, *,
                 first=None, partition=None):
    page = adapter.first_page
    pages_fetched = 0
    term_hits = 0
    total = first.total if first else None
    while True:
        if first is not None and page == adapter.first_page:
            current = first
        else:
            current = adapter.search_page(term, page, pause,
                                          **({"partition": partition}
                                             if partition else {}))
        if total is None:
            total = current.total
        label = term if partition is None else f"{term} {partition}"
        _land_page(dbx, catalog, adapter, run_id, label, page, current.raw)
        pages_fetched += 1
        for hit in current.hits:
            job_id = adapter.card_id(hit)
            if not job_id:
                continue
            term_hits += 1
            if job_id not in sightings:
                sightings[job_id] = adapter.card_signal(hit)
        if not current.has_next:
            break
        page += 1
        if pages_fetched > (total // adapter.page_size) + 2:
            raise AbnormalSourceError(
                f"pagination runaway for {term!r}: {pages_fetched} pages "
                f"against an advertised total of {total}")
    return pages_fetched, term_hits


def _land_page(dbx, catalog, adapter, run_id, term_label, page, raw):
    name = (f"{run_id}_{adapter.board}_{adapter.market}"
            f"_search_{slugify(term_label)}_p{page:03d}.json.gz")
    dbx_write.write_search_artifact(dbx, catalog, name, raw)


# ── Indeed ──────────────────────────────────────────────────────────────────
def _collect_indeed(dbx, settings, market, run_id, run_log, alerts):
    """Run each configured query and retain the last encountered copy of each job.

    Failed queries and malformed rows leave other results usable. Only the board
    budget or abnormal volume stops the sweep; there is no query-failure cutoff."""
    adapter = indeed_fetch.adapter(market)
    catalog = settings["databricks_catalog"]
    client = ApifyClient(settings["apify_token"])
    actor_metadata = indeed_fetch.validate_actor(client)
    prices = actor_metadata.get("chargedEventPricesUsd") or {}
    run_log.info("Indeed actor verified: build %s, pricing %s (%s)",
                 actor_metadata.get("buildNumber"),
                 actor_metadata.get("pricingModel"),
                 ", ".join(f"{event} USD {price:.6f}"
                           for event, price in sorted(prices.items()))
                 or "no charged events")

    queries = adapter.queries
    smoke = bool(settings["terms_limit"])
    if smoke:
        queries = queries[:settings["terms_limit"]]
        run_log.warning("SMOKE TEST: %d of %d queries — full_sweep FALSE",
                        len(queries), len(adapter.queries))

    budget = settings["apify_indeed_board_budget_usd"]
    max_charge = settings["apify_indeed_max_charge_usd"]
    spent = 0.0
    term_results = {}
    selected = {}            # job_id -> (row, query); LAST ENCOUNTERED WINS
    sweep_error = None       # sweep-wide stop (budget, canary) — never a count
    failed_queries = []      # (query text, reason) — every failure, in order
    malformed_by_query = {}  # query text -> {"rows": n, "first": reason}
    succeeded = attempted = 0
    rows_seen_total = in_market_total = 0
    canary_empty = canary_no_in_market = None
    cost_failures = []

    def query_failed(query, reason, *, error=None, **extra):
        """Record one failed query; the QUERY loop then continues with the
        next query, however many have failed. Only the budget guard at the
        top of the loop stops the sweep itself."""
        failed_queries.append((query.text, reason))
        term_results[query.text] = {**extra, "error": reason}
        if error is not None:
            log_failure(run_log, "query %r failed run_id=%s failures=%d",
                        query.text, run_id, len(failed_queries),
                        error=error, level=logging.WARNING)
        else:
            run_log.warning("query %r failed run_id=%s failures=%d; details in term_results",
                            query.text, run_id, len(failed_queries))

    for index, query in enumerate(queries, 1):
        if budget - spent < 0.001:
            sweep_error = (f"board budget USD {budget:.2f} exhausted before "
                           f"query {query.text!r}")
            break
        attempted += 1
        charge_cap = min(max_charge, budget - spent)
        try:
            evidence = indeed_fetch.run_query(
                client, adapter, query,
                max_charge_usd=charge_cap,
                timeout_s=settings["apify_run_timeout_s"])
        except Exception as error:
            query_failed(query, f"{type(error).__name__}: {error}"[:300], error=error)
            continue

        run = evidence.get("run") or {}
        rows = evidence.get("dataset") or []
        dbx_write.write_search_artifact(
            dbx, catalog,
            f"{run_id}_indeed_{market}_search_{query.key}_actor_run.json.gz",
            indeed_fetch.envelope(adapter, query, actor_metadata, evidence,
                                  settings["apify_token"]))
        _record_indeed_cost(run_id, market, query, run, rows, cost_failures,
                            run_log)

        cost = run.get("usageTotalUsd")
        if not isinstance(cost, (int, float)) or cost < 0:
            # The run happened but its spend is unknown: record the failure
            # and move on. No usage amount is invented — `spent` carries only
            # provider-reported figures.
            query_failed(query, f"actor run {run.get('id')} omitted valid "
                                f"usageTotalUsd")
            continue
        spent += float(cost)
        if run.get("status") != "SUCCEEDED":
            query_failed(
                query, f"actor run {run.get('id')} ended {run.get('status')!r}",
                status=run.get("status"), usage_usd=cost)
            continue

        valid = rejected = malformed = 0
        first_malformed = None
        for row in rows:
            try:
                job_id = indeed_parse.validate_row(row, adapter.config)
            except indeed_parse.MarketMismatchError:
                rejected += 1
                continue            # next ROW — routine out-of-market card
            except ValueError as error:
                # One malformed row is rejected alone and the ROW loop
                # continues, so later valid rows from the same dataset still
                # land. The raw envelope written above keeps the rejected
                # row verbatim.
                malformed += 1
                if first_malformed is None:
                    first_malformed = str(error)[:300]
                log_failure(run_log, "query %r: malformed row rejected run_id=%s",
                            query.text, run_id, error=error, level=logging.WARNING)
                continue
            valid += 1
            selected[job_id] = (row, query)      # last encountered wins
        if malformed:
            malformed_by_query[query.text] = {"rows": malformed,
                                              "first": first_malformed}
        if rows and malformed == len(rows):
            # Every row failed shape validation: that is the Actor's output
            # contract drifting, not one odd row — nothing usable came back,
            # so the query is recorded failed and the sweep moves on.
            query_failed(query,
                         f"actor output shape changed: all {len(rows)} rows "
                         f"malformed (first: {first_malformed})",
                         rows=len(rows), malformed_rows=malformed)
            continue

        succeeded += 1
        rows_seen_total += len(rows)
        in_market_total += valid
        if adapter.is_canary(query):
            if not rows:
                canary_empty = query.text
            elif not valid:
                canary_no_in_market = query.text
        term_results[query.text] = {
            "status": run.get("status"), "usage_usd": cost, "rows": len(rows),
            "valid_rows": valid, "rejected_market_rows": rejected,
            "malformed_rows": malformed,
            "unique_so_far": len(selected),
        }
        run_log.info("[%d/%d] %-30s rows=%-4d unique=%-5d usage=USD %.4f",
                     index, len(queries), query.text[:30], len(rows),
                     len(selected), cost)
        if len(selected) > adapter.max_plausible_unique:
            raise AbnormalSourceError(
                f"{len(selected)} unique jobs exceeds the "
                f"{adapter.max_plausible_unique} ceiling")

    # The canary condemns the sweep only when the WHOLE sweep found nothing:
    # zero rows everywhere is a broken pipe; one quiet term is just quiet.
    if canary_empty and rows_seen_total == 0:
        sweep_error = (f"provider returned zero rows for every query "
                       f"(canary {canary_empty!r})")
    elif canary_no_in_market and in_market_total == 0:
        sweep_error = (f"no valid in-market rows for any query "
                       f"(canary {canary_no_in_market!r})")

    written = _land_actor_observations(
        dbx, catalog, "indeed", market, run_id, selected, indeed_parse, run_log)

    term_results["_apify"] = {"queries_configured": len(queries),
                              "actor_runs": attempted,
                              "usage_usd": round(spent, 6),
                              "budget_usd": budget}
    if cost_failures:
        alerts.append(("Cost not recorded",
                       "the scrape worked but these Apify cost rows could not "
                       "be written to PostgreSQL:\n" + "\n".join(cost_failures)))
    if malformed_by_query:
        total = sum(entry["rows"] for entry in malformed_by_query.values())
        shown = "; ".join(
            f"{text!r}: {entry['rows']} (first: {entry['first']})"
            for text, entry in list(malformed_by_query.items())[:4])
        if len(malformed_by_query) > 4:
            shown += f" … and {len(malformed_by_query) - 4} more queries"
        alerts.append(("Malformed rows",
                       f"{total} rows failed validation and were kept out of "
                       f"the Bronze observations ({shown}). The original rows "
                       f"remain verbatim in this run's raw_search_results "
                       f"envelopes."))
    if failed_queries:
        run_log.warning(
            "search coverage incomplete run_id=%s: %d of %d configured queries failed "
            "(%d attempted, %d succeeded)",
            run_id, len(failed_queries), len(queries), attempted, succeeded)
        shown = "; ".join(f"{text!r}: {reason}"
                          for text, reason in failed_queries[:8])
        if len(failed_queries) > 8:
            shown += f" … and {len(failed_queries) - 8} more"
        alerts.append(("failed" if succeeded == 0 else "partial",
                       f"search coverage incomplete: {len(failed_queries)} of "
                       f"{len(queries)} configured queries failed "
                       f"({attempted} attempted, {succeeded} succeeded). Jobs "
                       f"matching only the failed queries are missing from "
                       f"this run; every other query landed normally. "
                       f"Failed: {shown}"))
    if sweep_error:
        alerts.append(("failed" if succeeded == 0 else "partial", sweep_error))

    status = ("failed" if succeeded == 0
              else "partial" if (sweep_error or failed_queries
                                 or written < len(selected))
              else "ok")
    return {
        "status": status,
        "counters": {
            "terms_swept": attempted,
            "terms_succeeded": succeeded,
            "unique_seen": len(selected),
            "new_jobs": None,            # no prior-state diff for actor boards
            "changed_jobs": None,
            "jds_intended": len(selected),
            "jds_fetched": written,
            "fetch_failures": len(selected) - written,
            "backlog_remaining": 0,
        },
        "term_results": term_results,
        "full_sweep": not smoke and succeeded == len(adapter.queries),
        "error_reason": sweep_error or (
            f"{len(failed_queries)} of {len(queries)} search queries failed"
            if failed_queries else None),
    }


def _record_indeed_cost(run_id, market, query, run, rows, cost_failures,
                        run_log):
    """One costs.apify_indeed row per Actor run. Fail soft: a lost cost row
    is logged and reported, never a reason to fail the scrape."""
    try:
        pg_write.record_indeed_cost({
            "apify_run_id": run.get("id") or f"unknown_{run_id}_{query.key}",
            "bto_run_id": run_id,
            "market": market,
            "search_term": query.text,
            "started_at": run.get("startedAt"),
            "finished_at": run.get("finishedAt"),
            "status": run.get("status") or "UNKNOWN",
            "result_count": len(rows),
            "cost_usd": run.get("usageTotalUsd"),
        })
        run_log.info("cost recorded: apify run %s USD %s",
                     run.get("id"), run.get("usageTotalUsd"))
    except Exception as error:
        log_failure(run_log, "cost write FAILED for apify run %s run_id=%s",
                    run.get("id"), run_id, error=error)
        cost_failures.append(f"{run.get('id')} ({query.text}): {error}")


def _land_actor_observations(dbx, catalog, board, market, run_id, selected,
                             parse, run_log):
    """Land the selected actor rows: content-addressed payload file first,
    then the observation row. Payload uploads ride a bounded worker pool;
    the Bronze table INSERT stays a single batched writer afterwards. A row
    whose payload file cannot be written keeps its observation with
    content_hash NULL (and NULL payload columns) — the raw envelope still
    holds the row, so nothing is lost, and the table never points at a file
    that is not there."""
    started = time.monotonic()
    # One upload per distinct hash: jobs whose canonical rows collide (never
    # seen in practice — a row carries its own id) share one future, so a
    # failed upload downgrades every observation that pointed at it.
    futures_by_hash = {}
    planned = []                              # (job_id, row, content_hash)
    with ThreadPoolExecutor(max_workers=ACTOR_UPLOAD_WORKERS) as pool:
        for job_id, (row, _query) in selected.items():
            canonical = parse.canonical_bytes(row)
            content_hash = hashlib.sha256(canonical).hexdigest()
            if content_hash not in futures_by_hash:
                futures_by_hash[content_hash] = pool.submit(
                    dbx_write.write_payload, dbx, catalog, content_hash,
                    canonical)
            planned.append((job_id, row, content_hash))

        rows_to_insert = []
        written = 0
        for job_id, row, content_hash in planned:
            try:
                futures_by_hash[content_hash].result()
            except Exception as error:
                log_failure(run_log, "payload write failed run_id=%s board_job_id=%s",
                            run_id, job_id, error=error)
                rows_to_insert.append({"board": board, "market": market,
                                       "board_job_id": job_id, "run_id": run_id,
                                       "content_hash": None})
                continue
            written += 1
            rows_to_insert.append(
                parse.observation_row(market, job_id, run_id, content_hash, row))

    elapsed = time.monotonic() - started
    run_log.info("payload uploads: %d files in %.0fs (%.2f/s, %d workers)",
                 written, elapsed, written / elapsed if elapsed else 0.0,
                 ACTOR_UPLOAD_WORKERS)
    dbx_write.insert_observations(dbx, catalog, board, rows_to_insert)
    run_log.info("%s_%s: %d observations landed (%d with payloads)",
                 board, market, len(rows_to_insert), written)
    return written


# ── LinkedIn ────────────────────────────────────────────────────────────────
def _collect_linkedin(dbx, settings, market, run_id, run_log, alerts):
    """One Actor run covering all configured terms; provider-side unique ids;
    rows already carry the JD."""
    adapter = linkedin_fetch.adapter(market)
    catalog = settings["databricks_catalog"]
    client = ApifyClient(settings["apify_token"])
    preflight = linkedin_fetch.validate_actor(client)
    account = client.account()
    ceiling = adapter.config.max_total_charge_usd
    run_log.info("LinkedIn actor verified (result <= USD %.4f/row); included "
                 "credit remaining USD %.2f, run ceiling USD %.2f",
                 preflight["pricing"]["resultTierPricesUsd"][-1],
                 account["remainingIncludedUsd"], ceiling)
    if account["remainingIncludedUsd"] < ceiling:
        run_log.warning("included credit below the ceiling — the remainder "
                        "draws on paid overage, hard-capped at USD %.2f", ceiling)

    smoke = bool(settings["terms_limit"])
    if smoke:
        adapter.terms = adapter.terms[:settings["terms_limit"]]
        run_log.warning("SMOKE TEST: %d terms — full_sweep FALSE",
                        len(adapter.terms))

    evidence = linkedin_fetch.run_all_terms(
        client, adapter, timeout_s=settings["apify_linkedin_run_timeout_s"])
    run = evidence.get("run") or {}
    rows = evidence.get("dataset") or []
    dbx_write.write_search_artifact(
        dbx, catalog,
        f"{run_id}_linkedin_{market}_search_all_terms_actor_run.json.gz",
        linkedin_fetch.envelope(adapter, preflight, evidence,
                                settings["apify_token"]))

    cost_failures = []
    try:
        pg_write.record_linkedin_cost({
            "apify_run_id": run.get("id") or f"unknown_{run_id}",
            "bto_run_id": run_id,
            "market": market,
            "started_at": run.get("startedAt"),
            "finished_at": run.get("finishedAt"),
            "status": run.get("status") or "UNKNOWN",
            "result_count": len(rows),
            "cost_usd": run.get("usageTotalUsd"),
        })
        run_log.info("cost recorded: apify run %s USD %s",
                     run.get("id"), run.get("usageTotalUsd"))
    except Exception as error:
        log_failure(run_log, "cost write FAILED for apify run %s run_id=%s",
                    run.get("id"), run_id, error=error)
        cost_failures.append(f"{run.get('id')}: {error}")
        alerts.append(("Cost not recorded",
                       f"the scrape worked but its Apify cost row could not "
                       f"be written to PostgreSQL: {error}"))

    provider_complete = run.get("status") == "SUCCEEDED"
    if not provider_complete and not rows:
        raise RuntimeError(f"actor run {run.get('id')} ended "
                           f"{run.get('status')!r} with no recoverable rows")
    cost = run.get("usageTotalUsd")
    usage_missing = not isinstance(cost, (int, float)) or cost < 0
    if usage_missing:
        # The harvest is real even when the billing metadata is not: land
        # the rows and flag the run partial below. No usage amount is
        # invented — the cost row above and term_results carry the anomaly
        # verbatim, never a substitute figure.
        run_log.warning("actor run %s omitted valid usageTotalUsd (%r) — "
                        "landing the rows and flagging the run partial",
                        run.get("id"), cost)
        cost = None
    elif float(cost) > ceiling + 1e-9:
        raise RuntimeError(f"actor reported USD {cost:.6f}, beyond the "
                           f"approved ceiling USD {ceiling:.2f}")

    # Shape verdict from the whole dataset, not the first odd row.
    selected = {}
    rejected, duplicates = [], 0
    for index, row in enumerate(rows):
        try:
            job_id = linkedin_parse.validate_row(row, adapter.config)
        except ValueError as error:
            rejected.append({"index": index, "error": str(error)[:200]})
            continue
        if job_id in selected:
            duplicates += 1
            continue
        selected[job_id] = (row, None)
    if rows and not selected:
        raise RuntimeError(
            "actor output shape changed: no row passed validation "
            f"(first: {rejected[0]['error'] if rejected else 'n/a'})")
    if len(selected) > adapter.max_plausible_unique:
        raise AbnormalSourceError(
            f"{len(selected)} unique jobs exceeds the "
            f"{adapter.max_plausible_unique} ceiling")

    written = _land_actor_observations(
        dbx, catalog, "linkedin", market, run_id, selected, linkedin_parse,
        run_log)

    partial_reasons = []
    if not provider_complete:
        partial_reasons.append(f"provider terminal status "
                               f"{run.get('status')!r}")
    if usage_missing:
        partial_reasons.append(
            f"actor run {run.get('id')} omitted valid usageTotalUsd — "
            f"spend for this run is unverified")
    if rejected:
        partial_reasons.append(f"{len(rejected)} rows rejected by validation")
    if not smoke and len(selected) < adapter.config.plausibility_floor:
        partial_reasons.append(
            f"only {len(selected)} unique jobs — below the market's "
            f"{adapter.config.plausibility_floor} plausibility floor")
    if written < len(selected):
        partial_reasons.append(f"{len(selected) - written} payload files "
                               f"failed to land")
    if partial_reasons:
        alerts.append(("partial", "; ".join(partial_reasons)))

    search_counts = {}
    for row, _query in selected.values():
        key = (row.get("searchString") or "(missing)").strip()
        search_counts[key] = search_counts.get(key, 0) + 1
    term_results = {
        "_apify": {"actor_runs": 1, "usage_usd": cost, "ceiling_usd": ceiling,
                   "provider_run_id": run.get("id"),
                   "provider_status": run.get("status")},
        "rows_returned": len(rows),
        "valid_unique": len(selected),
        "rejected_rows": len(rejected),
        "duplicate_ids": duplicates,
        "search_string_counts": dict(sorted(
            search_counts.items(), key=lambda item: (-item[1], item[0]))[:200]),
    }
    return {
        "status": "partial" if partial_reasons else "ok",
        "counters": {
            "terms_swept": len(adapter.terms),
            "terms_succeeded": len(adapter.terms) if provider_complete else 0,
            "unique_seen": len(selected),
            "new_jobs": None,
            "changed_jobs": None,
            "jds_intended": len(selected),
            "jds_fetched": written,
            "fetch_failures": len(selected) - written,
            "backlog_remaining": 0,
        },
        "term_results": term_results,
        "full_sweep": not smoke and provider_complete,
        "error_reason": "; ".join(partial_reasons) if partial_reasons else None,
    }


# ── deployment preflight ────────────────────────────────────────────────────
def preflight(settings):
    """Verify collection storage, Apify actors and SMTP without collecting jobs."""
    ok = True
    catalog = settings["databricks_catalog"]
    dbx = Databricks(settings["databricks_host"], settings["databricks_token"],
                     settings["databricks_warehouse_id"])
    for volume in (SEARCH_RESULTS_VOLUME, JOB_LISTINGS_VOLUME):
        try:
            dbx.volume_exists(volume_path(catalog, volume))
            log.info("volume %s reachable", volume_path(catalog, volume))
        except Exception as error:
            log_failure(log, "volume %s FAILED", volume, error=error)
            ok = False
    for table in ("scrape_runs", "mcf_job_listings", "seek_job_listings",
                  "indeed_job_listings", "linkedin_job_listings"):
        try:
            dbx.query(f"SELECT 1 FROM {table_name(catalog, table)} LIMIT 1")
            log.info("table %s reachable", table_name(catalog, table))
        except Exception as error:
            log_failure(log, "table %s FAILED", table, error=error)
            ok = False

    try:
        pg_setup.ensure_cost_tables()
        log.info("PostgreSQL reachable; cost tables ensured")
    except Exception as error:
        log_failure(log, "PostgreSQL FAILED", error=error)
        ok = False

    client = ApifyClient(settings["apify_token"])
    try:
        summary = indeed_fetch.validate_actor(client)
        log.info("Indeed actor OK: %s build %s", summary.get("name"),
                 summary.get("buildNumber"))
    except Exception as error:
        log_failure(log, "Indeed actor FAILED", error=error)
        ok = False
    try:
        preflight_evidence = linkedin_fetch.validate_actor(client)
        account = client.account()
        log.info("LinkedIn actor OK: %s; included credit remaining USD %.2f",
                 preflight_evidence["actor"].get("name"),
                 account["remainingIncludedUsd"])
    except Exception as error:
        log_failure(log, "LinkedIn actor FAILED", error=error)
        ok = False

    try:
        import smtplib
        import ssl
        if settings["smtp_port"] == 465:
            server = smtplib.SMTP_SSL(settings["smtp_host"],
                                      settings["smtp_port"],
                                      context=ssl.create_default_context(),
                                      timeout=30)
        else:
            server = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"],
                                  timeout=30)
            server.starttls(context=ssl.create_default_context())
        server.login(settings["smtp_username"], settings["smtp_password"])
        server.quit()
        log.info("SMTP login OK (%s)", settings["smtp_host"])
    except Exception as error:
        log_failure(log, "SMTP FAILED", error=error)
        ok = False

    log.info("preflight %s", "PASSED" if ok else "FAILED")
    return ok
