"""Queue job-listing research and verify the employer's board before its vacancy.

Each step proposes candidates; separate checks decide whether to accept them.
The first accepted answer ends each part. A PostgreSQL advisory lock keeps
the web worker and CLI from processing the queue together. Interrupted work
is marked failed, and failed retries preserve the previous links.
"""

import argparse
import html
import logging
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .. import settings
from ..log_errors import log_failure
from ..storage.postgres import read as pg_read
from ..storage.postgres import write as pg_write
from ..storage.postgres.connection import connect
from . import websearch_for_employer_board, websearch_for_vacancy
from .board_address import board_from_address
from .check_employer_board import accept_board, board_page_from_job_address
from .fetch_page import fetch, host
from .guess_employer_website import careers_addresses, employer_websites
from .hiring_systems import is_aggregator, is_reposting_address
from .private_limits import daily_limit
from .prove_same_job import accept_vacancy, requisition_ids
from .search_the_board import (
    boards_behind_careers_page,
    search_board,
    search_careers_site,
    sitemap_candidates,
    title_similarity,
)

log = logging.getLogger(__name__)

TABLE = "direct_application_links"
# What request() returns when it queues nothing; contact finding uses the same words.
NOT_ELIGIBLE = "not_eligible"
DAILY_LIMIT_REACHED = "daily_limit"
MAX_CANDIDATES = 6
# Stop starting steps or candidate checks after this budget; in-flight reads finish.
TIME_BUDGET_S = 150.0
# How often an idle worker looks for searches another process queued.
QUEUE_CHECK_INTERVAL_S = 60
# The PostgreSQL advisory lock held while a process runs the research queue.
# Sorting uses 72403104, Review state writes 72403105, people-search requests
# 72403106 and the people-search queue 72403108.
QUEUE_LOCK = 72403107
# Request counts and enqueue share a short lock, separate from the paid worker.
REQUEST_LOCK = 72403109

_worker_thread = None
_worker_start_lock = threading.Lock()
_wake_worker = threading.Event()
_restart_recovered = threading.Event()


@dataclass
class Listing:
    """Stored listing facts used to verify an employer and vacancy.

    The employer's website helps distinguish companies with the same name.
    No fields are inferred or looked up when this record is built.
    """

    title: str
    advertiser_name: str = ""
    hiring_company_name: str = ""
    company_website: str = ""
    job_location: str = ""
    market: str = ""
    jd_text: str = ""
    apply_url: str = ""
    # the same job's apply links on other boards in this market
    copy_apply_urls: list = field(default_factory=list)

    @property
    def employer(self):
        return (self.hiring_company_name or self.advertiser_name or "").strip()


@dataclass
class Resolution:
    """What one search found, and what it did to find it."""

    board: str | None = None
    vacancy: str | None = None
    board_evidence: dict = field(default_factory=dict)
    vacancy_evidence: dict = field(default_factory=dict)
    # ("board" or "vacancy", step name, "found" or "nothing")
    steps: list = field(default_factory=list)
    calls: dict = field(default_factory=lambda: {"http": 0, "api": 0, "search": 0})
    # why Brave would not answer, if it would not
    search_refusals: list = field(default_factory=list)
    ran_out_of_time: bool = False

    @property
    def status(self):
        """resolved (both found), partial (one found), failed (neither, and
        Brave refused), or unresolved (neither, after looking everywhere)."""
        if self.board and self.vacancy:
            return "resolved"
        if self.board or self.vacancy:
            return "partial"
        if self.search_refusals:
            return "failed"
        return "unresolved"

    @property
    def failure_reason(self):
        if self.status != "failed":
            return None
        return f"Brave refused the search: {self.search_refusals[0]}"[:300]


def listing_from_row(row):
    """Build a Listing from stored facts and same-market copies.

    Ignore copies from other markets because their apply links may lead to a
    different vacancy.
    """
    facts = row.get("board_facts") or {}
    market = row["market"]
    copy_apply_urls = []
    for copy in row.get("same_market_copies") or []:
        if copy.get("market", market) != market:
            continue
        apply_url = _fact_text(copy.get("board_facts") or {}, "apply_url")
        if apply_url and apply_url not in copy_apply_urls:
            copy_apply_urls.append(apply_url)
    return Listing(
        title=row.get("title") or "",
        advertiser_name=row.get("advertiser_name") or "",
        hiring_company_name=row.get("hiring_company_name") or "",
        company_website=_fact_text(facts, "company_website"),
        job_location=_fact_text(facts, "job_location"),
        market=market,
        jd_text=row.get("description_text") or "",
        apply_url=_fact_text(facts, "apply_url"),
        copy_apply_urls=copy_apply_urls,
    )


def _fact_text(facts, name):
    """A board fact that is plain text, or "" when it is missing or not text."""
    value = facts.get(name)
    return value if isinstance(value, str) else ""


# ── steps that propose the employer's job board ───────────────────────────

def _boards_from_links_we_hold(listing, resolution):
    """The board pages behind the apply link and the same job's apply links on
    other boards in this market, those links themselves, and the company
    website named on the listing. Free and immediate."""
    candidates = []
    for url in ([board_page_from_job_address(listing.apply_url), listing.apply_url]
                + [board_page_from_job_address(u) for u in listing.copy_apply_urls]
                + list(listing.copy_apply_urls)):
        if url and url not in candidates:
            candidates.append(url)
    if listing.company_website:
        website = listing.company_website
        candidates.append(website if website.startswith("http") else "https://" + website)
    return candidates


def _boards_from_web_search(listing, resolution):
    """Brave's answers for where this employer publishes its jobs, on hosts it controls."""
    return websearch_for_employer_board.board_candidates(listing, resolution.calls,
                                                         resolution.search_refusals)


def _boards_from_employer_website(listing, resolution):
    """The usual careers addresses on a website the employer controls."""
    candidates = []
    for website in employer_websites(listing, resolution.calls)[:2]:
        candidates += careers_addresses(website)
    return candidates


# ── steps that propose this vacancy on that board ─────────────────────────

def _vacancies_from_links_we_hold(listing, resolution, board):
    """The apply links on this listing and on its copies in this market, as they are."""
    candidates = []
    for url in [listing.apply_url] + list(listing.copy_apply_urls):
        if url and url not in candidates:
            candidates.append(url)
    return candidates


def _vacancies_from_board_api(listing, resolution, board):
    """Ask the board's own vendor for this title, a printed requisition number first."""
    searchable = board_from_address(board)
    if searchable is None:
        # The board is often the employer's own careers page in front of a
        # vendor whose tenant is named nowhere in its address, so the page is
        # read for the vendor behind it before giving up.
        behind = boards_behind_careers_page(board, resolution.calls)[:2]
        searchable = behind[0] if behind else None
    if searchable is None:
        return []
    try:
        candidates = search_board(searchable, listing, resolution.calls)
    except Exception as error:
        log_failure(log, "Vendor job API search failed board=%s", searchable.label(),
                    error=error, level=logging.WARNING)
        return []
    requisitions = [r.lower() for r in requisition_ids(listing)]

    def rank(candidate):
        printed = (candidate.get("url", "") + " " + (candidate.get("text") or "")).lower()
        requisition_hit = any(r in printed for r in requisitions) if requisitions else False
        return (requisition_hit, candidate.get("containment") or 0,
                candidate.get("title_similarity") or 0)

    ranked = sorted(candidates, key=rank, reverse=True)[:MAX_CANDIDATES]
    return [c["url"] for c in ranked if c.get("url")]


def _vacancies_from_board_pages(listing, resolution, board):
    """Read the board itself: the job links it prints, its search page, its sitemap.

    A board address is sometimes a bare search page with no query on it,
    which lists nothing, so the host's home page is read as well.
    """
    requisitions = [r.lower() for r in requisition_ids(listing)]
    base = f"https://{host(board)}"
    ranked = []
    for address in dict.fromkeys([board, base]):
        resolution.calls["http"] += 1
        page = fetch(address, timeout=25)
        for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page.html or "",
                                 re.DOTALL | re.IGNORECASE):
            href = match.group(1)
            text = re.sub(r"<[^>]+>|\s+", " ", html.unescape(match.group(2))).strip()
            if not re.search(r"(?i)/(job|jobs|career|careers|position|posting|requisition|vacanc)s?"
                             r"/[^/?#]{3,}", href):
                continue
            url = urllib.parse.urljoin(page.final_url or address, href).split("#")[0]
            if is_reposting_address(url) or is_aggregator(url):
                continue
            requisition_hit = (any(r in url.lower() or r in text.lower() for r in requisitions)
                               if requisitions else False)
            similarity = round(title_similarity(listing.title, text), 2)
            if requisition_hit or similarity >= 0.45:
                ranked.append((requisition_hit, similarity, url))
        if ranked:
            break
    for finder in (search_careers_site, sitemap_candidates):
        if len(ranked) >= MAX_CANDIDATES:
            break
        try:
            for candidate in finder(base, listing, resolution.calls):
                similarity = round(title_similarity(listing.title, candidate.get("title", "")), 2)
                ranked.append((False, similarity, candidate["url"]))
        except Exception as error:
            # A source that breaks has nothing to give; the next one is tried.
            log_failure(log, "Board page source failed source=%s", finder.__name__,
                        error=error, level=logging.WARNING)
    ranked.sort(reverse=True)
    return list(dict.fromkeys(url for _hit, _similarity, url in ranked))[:MAX_CANDIDATES]


def _vacancies_from_web_search(listing, resolution, board):
    """Brave's answers for this vacancy, restricted to the board's host."""
    return websearch_for_vacancy.vacancy_candidates(listing, board, resolution.calls,
                                                    resolution.search_refusals)


# ── the order of the steps ────────────────────────────────────────────────
# Stored links first; employer-website guesses remain a fallback without Brave.

BOARD_STEPS = (
    ("links we hold", _boards_from_links_we_hold),
    ("web search", _boards_from_web_search),
    ("employer website", _boards_from_employer_website),     # the route without Brave
)
VACANCY_STEPS = (
    ("links we hold", _vacancies_from_links_we_hold),
    ("board API", _vacancies_from_board_api),
    ("board pages", _vacancies_from_board_pages),
    ("web search", _vacancies_from_web_search),
)


def resolve(listing, budget_s=TIME_BUDGET_S):
    """Find a verified board, then this vacancy; either answer may be missing.

    The budget is checked between steps and candidate checks. An in-flight
    step can finish after it; running out of time is recorded in the result.
    """
    started = time.monotonic()
    resolution = Resolution()

    def out_of_time():
        if time.monotonic() - started < budget_s:
            return False
        resolution.ran_out_of_time = True
        return True

    def first_verified(part, steps, propose, verify):
        for name, step in steps:
            if out_of_time():
                break
            step_started = time.monotonic()
            try:
                candidates = propose(step)
            except Exception as error:
                # A step that breaks has found nothing; the next step still runs.
                log_failure(log, "Job-listing research step failed part=%s step=%s",
                            part, name, error=error, level=logging.WARNING)
                candidates = []
            candidates = list(dict.fromkeys(c for c in candidates if c))[:MAX_CANDIDATES]
            found = False
            for url in candidates:
                if out_of_time():
                    break
                accepted, evidence = verify(url)
                if accepted:
                    found = True
                    break
            resolution.steps.append((part, name, "found" if found else "nothing"))
            log.info("job-listing research step finished part=%s step=%r candidates=%s "
                     "found=%s seconds=%.1f", part, name, len(candidates), found,
                     time.monotonic() - step_started)
            if found:
                return url, {**evidence, "step": name}
        return None, {}

    resolution.board, resolution.board_evidence = first_verified(
        "board", BOARD_STEPS, lambda step: step(listing, resolution),
        lambda url: accept_board(url, listing, resolution.calls))
    if resolution.board:
        resolution.vacancy, resolution.vacancy_evidence = first_verified(
            "vacancy", VACANCY_STEPS, lambda step: step(listing, resolution, resolution.board),
            lambda url: accept_vacancy(url, listing, resolution.calls, board=resolution.board))
    return resolution


# ── the queue ─────────────────────────────────────────────────────────────

def queue_search(conn, key, *, by_hand=False):
    """Queue a saved listing and return its search status.

    Automatic requests keep an earlier result; by_hand requests can retry a
    finished search. Pending searches keep their place. Check the daily limit
    and enqueue under the same lock so concurrent requests cannot exceed it.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (REQUEST_LOCK,))
        saved = {(row["board"], row["market"], row["board_job_id"])
                 for row in pg_read.prioritised_listings(conn)}
        if tuple(key) not in saved:
            log.info("job-listing research not queued: not saved to To Apply "
                     "board=%s market=%s board_job_id=%s", *key)
            return NOT_ELIGIBLE
        current = pg_read.direct_application_link(conn, key)
        if current is not None and (current["status"] in ("queued", "searching") or not by_hand):
            return current["status"]
        asked_today = pg_read.searches_today(conn, TABLE)
        limit = daily_limit()
        if asked_today >= limit:
            log.info("job-listing research not queued: daily limit reached "
                     "board=%s market=%s board_job_id=%s asked_today=%d limit=%d",
                     *key, asked_today, limit)
            return DAILY_LIMIT_REACHED
        return pg_write.request_direct_application_link(conn, key)


def research_queued_listing(conn, queued):
    """Run one queued search, the row next_queued_direct_application_link
    gave, and record how it ended.

    A search that cannot run, or breaks part-way, is recorded as failed with
    its reason; it never stops the queue. A failed search keeps the previous
    search's links, because nothing newer was learnt. They are taken from the
    queued row itself: after a merge, the group's other anchors have rows of
    their own, with their own links.
    """
    key = (queued["board"], queued["market"], queued["board_job_id"])
    if not pg_write.start_direct_application_link(conn, key):
        return None                     # no longer queued: another worker took it
    board, vacancy = queued.get("company_job_board_url"), queued.get("vacancy_url")
    saved = {(row["board"], row["market"], row["board_job_id"])
             for row in pg_read.prioritised_listings(conn)}
    if key not in saved:
        # Taken out of To Apply while it waited: end it before anything is spent.
        pg_write.finish_direct_application_link(
            conn, key, status="failed", company_job_board_url=board, vacancy_url=vacancy,
            failure_reason="no longer saved to To Apply when its turn came")
        log.info("job-listing research skipped: no longer saved to To Apply "
                 "board=%s market=%s board_job_id=%s", *key)
        return "failed"
    started = time.time()
    try:
        row = pg_read.listing_for_research(conn, key)
        if row is None:
            status, failure_reason = "failed", "the listing is no longer stored"
        else:
            resolution = resolve(listing_from_row(row))
            status, failure_reason = resolution.status, resolution.failure_reason
            if status != "failed":
                board, vacancy = resolution.board, resolution.vacancy
            log.info("job-listing research board=%s market=%s board_job_id=%s steps=%s calls=%s "
                     "ran_out_of_time=%s", *key, resolution.steps, resolution.calls,
                     resolution.ran_out_of_time)
    except Exception as error:
        log_failure(log, "Job-listing research failed board=%s market=%s board_job_id=%s",
                    *key, error=error)
        status, failure_reason = "failed", f"{type(error).__name__}: {error}"[:300]
    pg_write.finish_direct_application_link(conn, key, status=status, company_job_board_url=board,
                                            vacancy_url=vacancy, failure_reason=failure_reason)
    log.info("job-listing research finished board=%s market=%s board_job_id=%s status=%s "
             "seconds=%.0f", *key, status, time.time() - started)
    return status


def run_queued_searches():
    """Take the research queue and run every queued search, one at a time,
    oldest request first. Returns how many ran, or None when another process
    holds the queue.

    The first time a process takes the queue, research still marked searching
    is failed: only the queue holder searches, so such a row was cut off by a
    restart and can never finish. People searches are left alone; their own
    worker does the same for them.
    """
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (QUEUE_LOCK,)).fetchone()[0]:
            return None
        if not _restart_recovered.is_set():
            pg_write.fail_interrupted_searches(conn, TABLE)
            _restart_recovered.set()
        ran = 0
        while (queued := pg_read.next_queued_direct_application_link(conn)) is not None:
            research_queued_listing(conn, queued)
            ran += 1
        return ran                      # closing the connection releases the lock


def _keep_working_through_queue():
    """The worker thread: run the queue, then wait to be woken or for the next check."""
    while True:
        # Cleared before the queue is read, so a request made during a search
        # wakes the next round at once.
        _wake_worker.clear()
        try:
            run_queued_searches()
        except Exception as error:
            log_failure(log, "Job-listing research worker pass failed; next try in %d s",
                        QUEUE_CHECK_INTERVAL_S, error=error)
            # A pass that broke can leave its search marked searching, and it
            # has let go of the queue, so nothing can finish that search. The
            # next pass that takes the queue fails it, as after a restart.
            _restart_recovered.clear()
        _wake_worker.wait(QUEUE_CHECK_INTERVAL_S)


def start_worker():
    """Make sure this process's research worker thread is running, and wake it.

    Safe to call any number of times. The website calls it from To Apply
    views and research requests, so after a restart the queue resumes on
    the next visit.
    """
    global _worker_thread
    with _worker_start_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_keep_working_through_queue, daemon=True,
                                              name="job-listing-research")
            _worker_thread.start()
    _wake_worker.set()


def request(key, *, by_hand=False):
    """Queue job-listing research for one listing and make sure the worker runs.

    `key` is the (board, market, board_job_id) that prioritised_listings
    gives for the job: its Review group's first retained anchor in
    job_tracking.review_status, the one Review writes reuse. Any other key
    returns NOT_ELIGIBLE. Returns the status word after the request:
    queued or searching, a finished search's word when nothing new was queued,
    NOT_ELIGIBLE or DAILY_LIMIT_REACHED. See queue_search for when a new
    search is queued.
    """
    with connect() as conn:
        word = queue_search(conn, key, by_hand=by_hand)
    start_worker()
    return word


def status(key):
    """Everything the Links section shows for one listing.

    `state` is the latest search's status, or "not_searched". While a new
    search is queued or searching, the links are the previous search's. Times
    are UTC datetimes; the page shows them as h:mm AM/PM Singapore time. The
    queue fields are set only while the search is queued: its place counts
    the search running now ("2nd of 4"), and queue_ahead names the employer
    whose search is just ahead.
    """
    with connect() as conn:
        row = pg_read.direct_application_link(conn, key)
        position = total = ahead = None
        if row is not None and row["status"] == "queued":
            position, total, ahead = pg_read.queue_position(
                conn, TABLE, (row["board"], row["market"], row["board_job_id"]))
    row = row or {}
    return {
        "state": row.get("status") or "not_searched",
        "requested_at": row.get("requested_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "company_job_board_url": row.get("company_job_board_url"),
        "vacancy_url": row.get("vacancy_url"),
        "failure_reason": row.get("failure_reason"),
        "queue_position": position,
        "queue_total": total,
        "queue_ahead": ahead,
    }


# ── by hand ───────────────────────────────────────────────────────────────

def main(argv=None):
    """Queue saved listings from the command line and process the research queue."""
    parser = argparse.ArgumentParser(
        description="Find the employer's own job board and vacancy page for To Apply listings.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--to-apply", action="store_true",
                       help="queue every To Apply listing that has never been searched, "
                            "then run the queue here")
    which.add_argument("--listing", nargs=3, metavar=("BOARD", "MARKET", "BOARD_JOB_ID"),
                       help="search one To Apply listing again, then run the queue here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                        stream=sys.stdout, force=True)
    settings.load_postgres()
    with connect() as conn:
        if args.listing:
            keys, by_hand = [tuple(args.listing)], True
        else:
            keys = [(row["board"], row["market"], row["board_job_id"])
                    for row in pg_read.prioritised_listings(conn)]
            by_hand = False
        outcomes = {}
        for key in keys:
            word = queue_search(conn, key, by_hand=by_hand)
            outcomes[word] = outcomes.get(word, 0) + 1
    log.info("job-listing research requests: %s",
             ", ".join(f"{word}={count}" for word, count in sorted(outcomes.items())) or "none")
    ran = run_queued_searches()
    if ran is None:
        log.info("another process holds the research queue; it will run the queued searches")
    else:
        log.info("job-listing research searches run here: %d", ran)
    return 0


if __name__ == "__main__":
    sys.exit(main())
