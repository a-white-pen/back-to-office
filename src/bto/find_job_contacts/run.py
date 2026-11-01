"""Queue contact searches and provide their results to the People panel.

A PostgreSQL advisory lock keeps the web worker and CLI from processing the
queue together. Interrupted searches are marked failed and their Apify costs
are reconciled before another search starts. A failed search preserves the
previous people and posts. Individual lookups use the same lookup functions.
"""

import argparse
import logging
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import settings
from ..log_errors import log_failure
from ..storage.postgres import read, write
from ..storage.postgres.connection import connect
from ..storage.postgres.write import PEOPLE_SEARCH_REQUEST_LOCK
from . import config, find_emails, find_online_accounts, find_phones
from .find_relevant_personnel import from_web, in_job_posts, in_listing, in_sgdi, on_linkedin
from .find_relevant_personnel.classify_found_persons import (
    FoundPerson,
    classify,
    is_half_name,
    linkedin_key,
    pick_key_personnel,
    same_name,
    team_words,
)
from .identify_company import Company, identify_company
from .providers import apify, github
from .providers.hunter import HunterClient, HunterUnavailable

log = logging.getLogger(__name__)

SGT = timezone(timedelta(hours=8))          # Singapore never observes DST
TABLE = "personnel_searches"
# How often an idle worker looks for searches another process queued.
WORKER_IDLE_SECONDS = 60
# Held by the process that runs people searches. Sorting uses 72403104,
# Review state writes 72403105, people-search requests 72403106 and
# job-listing research 72403107.
QUEUE_LOCK = 72403108
GROUP_LABELS = {
    "named_on_listing": "Named on the listing",
    "recruiter": "Recruiters",
    "hiring_manager_or_team": "Hiring manager and team",
    "also_at_company": "Also at the employer",
}
PROVENANCE_LABELS = {
    "publicly_observed": "PUBLICLY OBSERVED",
    "provider_observed": "PROVIDER-OBSERVED",
    "provider_inferred": "PROVIDER-INFERRED",
}
CHANNEL_ORDER = ("direct_line", "mobile", "github", "x", "instagram", "personal_site")

_worker = None
_worker_lock = threading.Lock()
_wake = threading.Event()
# Set once this process has failed the people searches a restart cut off.
_restart_recovered = threading.Event()


class SearchFailed(RuntimeError):
    """The search cannot go on; the message is the failure reason shown."""


@dataclass
class Lookup:
    """What the full lookups of one search, or of one Look up click, share.

    `hunter` is None when email checks are not made. `web_searches` is how
    many Brave searches each person gets. `patterns` keeps each email domain's
    address pattern, so it is bought once. `use_github` turns False once GitHub
    refuses, for the people still to look up.
    """

    company: Company
    market: str
    email_domain: str | None
    hunter: HunterClient | None
    web_searches: int
    patterns: dict = field(default_factory=dict)
    use_github: bool = True


# ── asking for a search ─────────────────────────────────────────────────────

def request(key, *, by_hand=False):
    """Queue an eligible listing, wake the worker and return its search status.

    Automatic requests need a saved, starred job and keep earlier results.
    Requests made by_hand can retry any saved job. Ineligible jobs and a full
    daily allowance return not_eligible or daily_limit without starting work.
    """
    with connect() as conn:
        outcome = queue_search(conn, key, by_hand=by_hand)
    if outcome in ("queued", "searching"):
        start_worker()
    return outcome


def queue_search(conn, key, *, by_hand):
    """The database half of `request`: queue the search unless a rule says no."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (PEOPLE_SEARCH_REQUEST_LOCK,))
        eligible = {(row["board"], row["market"], row["board_job_id"])
                    for row in read.prioritised_listings(conn, starred_only=not by_hand)}
        if tuple(key) not in eligible:
            return "not_eligible"
        latest = read.latest_personnel_search(conn, key)
        if latest and latest["status"] in ("queued", "searching"):
            return latest["status"]
        if latest and not by_hand:
            return latest["status"]
        if read.searches_today(conn, TABLE) >= config.PEOPLE_SEARCHES_PER_DAY:
            log.info("people search refused: daily limit of %d reached",
                     config.PEOPLE_SEARCHES_PER_DAY)
            return "daily_limit"
        bto_run_id = new_bto_run_id(key)
        outcome = write.request_personnel_search(conn, key, bto_run_id)
        log.info("people search requested search=%s by_hand=%s status=%s",
                 bto_run_id, by_hand, outcome)
        return outcome


def new_bto_run_id(key, now_utc=None):
    """people_{board}_{market}_{YYYYMMDD_HHMMSS}_{suffix}: Singapore time and
    four random hex characters to distinguish searches started in one second."""
    stamp = (now_utc or datetime.now(timezone.utc)).astimezone(SGT).strftime("%Y%m%d_%H%M%S")
    return f"people_{key[0]}_{key[1]}_{stamp}_{secrets.token_hex(2)}"


# ── the worker ──────────────────────────────────────────────────────────────

def start_worker():
    """Make sure this process's people-search worker thread is running, and wake it.

    Safe to call any number of times. The website calls it from To Apply
    views and research requests, so after a restart the queue resumes on
    the next visit.
    """
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_keep_working_through_queue,
                                       name="find-job-contacts", daemon=True)
            _worker.start()
    _wake.set()


def run_queued_searches():
    """Take the people-search queue and run every queued search, one at a
    time, oldest request first. Returns how many ran, or None when another
    process holds the queue.

    The first time a process takes the queue, people searches still marked
    searching are failed: only the queue holder searches, so such a row was
    cut off by a restart and can never finish. Job-listing research is left
    alone; its own worker does the same for it.
    """
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (QUEUE_LOCK,)).fetchone()[0]:
            return None
        if not _restart_recovered.is_set():
            write.fail_interrupted_searches(conn, TABLE)
            _restart_recovered.set()
        ran = 0
        while (search := read.next_queued_personnel_search(conn)) is not None:
            # Costs an earlier search could not read, such as those of a search
            # cut off by a restart, are read again. Only the queue holder
            # starts Actor runs, so none of these belongs to a running search.
            apify.record_unread_costs(read.apify_runs_without_cost(conn))
            run_search(conn, search)
            ran += 1
        return ran                      # closing the connection releases the lock


def run_search(conn, search):
    """Run one queued search and record how it ended. Never raises."""
    bto_run_id = search["bto_run_id"]
    started = time.monotonic()
    key = (search["board"], search["market"], search["board_job_id"])
    try:
        if not write.start_personnel_search(conn, bto_run_id):
            return                      # no longer queued: another worker took it
        eligible = {(row["board"], row["market"], row["board_job_id"])
                    for row in read.prioritised_listings(conn)}
        if key not in eligible:
            # Taken out of To Apply while it waited: end it before anything is
            # spent. Unstarring does not: a search asked for by hand may be for
            # a job that was never starred.
            write.finish_personnel_search(
                conn, bto_run_id, status="failed",
                failure_reason="no longer saved to To Apply when its turn came")
            log.info("people search skipped: no longer saved to To Apply search=%s", bto_run_id)
            return
        log.info("people search starting search=%s", bto_run_id)
        status = search_for_people(conn, key, bto_run_id)
        log.info("people search finished search=%s status=%s seconds=%.1f",
                 bto_run_id, status, time.monotonic() - started)
        return
    except (SearchFailed, apify.ApifyRefused) as error:
        # A refusal the job line explains, such as the monthly cap: no traceback.
        log.warning("People search refused search=%s error_type=%s; reason saved with the search",
                    bto_run_id, type(error).__name__)
        reason = str(error)
    except Exception as error:
        log_failure(log, "People search failed search=%s", bto_run_id, error=error)
        reason = f"{type(error).__name__}: {error}"
    try:
        write.finish_personnel_search(conn, bto_run_id, status="failed",
                                      failure_reason=reason[:300])
    except Exception as error:
        log_failure(log, "People search not closed search=%s", bto_run_id, error=error)


def _keep_working_through_queue():
    """The worker thread: run the queue, then wait to be woken or for the next check."""
    while True:
        # Cleared before the queue is read, so a request made during a search
        # wakes the next round at once.
        _wake.clear()
        try:
            run_queued_searches()
        except Exception as error:
            log_failure(log, "People search worker pass failed; next try in %d s",
                        WORKER_IDLE_SECONDS, error=error)
            # A broken pass can leave its search marked searching after letting
            # go of the queue, so nothing would finish it. The next pass that
            # takes the queue fails it, as after a restart.
            _restart_recovered.clear()
        _wake.wait(timeout=WORKER_IDLE_SECONDS)


# ── one search ──────────────────────────────────────────────────────────────

def search_for_people(conn, key, bto_run_id):
    """Steps 1 to 9 for one listing. Returns the finished status."""
    listing = _read_listing(conn, key)
    company = identify_company(conn, listing)
    job_words = team_words(listing.get("title"), listing.get("description_text"))
    place = config.place_name(listing["market"])
    listed = in_listing.find_people_in_listing(listing, company.linkedin_copy)

    if company.is_recruitment_firm:
        # Only what the listing prints, and nothing is spent on a firm.
        people = _mark_printed_addresses(classify(listed, job_words, place))
        return _publish_people(conn, key, bto_run_id, people, [], status="recruitment_firm")

    runs = apify.PeopleSearchRuns(bto_run_id)
    try:
        found, posts = listed, []
        if company.name:
            # A refusal here fails the search before the steps start.
            runs.check_monthly_cap()
            more, posts = _find_people_at_company(company, listing, job_words, runs)
            found = listed + more
        people = classify(found, job_words, place)
        key_people = pick_key_personnel(people, recruiters=config.FULL_LOOKUP_RECRUITERS,
                                        hiring_team=config.FULL_LOOKUP_HIRING_TEAM)
        try:
            on_linkedin.read_full_profiles(key_people, runs)
        except apify.ApifyRefused as error:
            log_failure(log, "Full profiles not read search=%s", bto_run_id,
                        error=error, level=logging.WARNING)
        # A name still half after the profile read has no surname to look up
        # by. It keeps Look up, and the next full name takes its place.
        for person in people:
            person.is_key = False
        key_people = pick_key_personnel(
            [p for p in people if p.group == "named_on_listing" or not is_half_name(p.name)],
            recruiters=config.FULL_LOOKUP_RECRUITERS, hiring_team=config.FULL_LOOKUP_HIRING_TEAM)
        log.info("people search ready for lookup search=%s people=%s key_people=%s posts=%s",
                 bto_run_id, len(people), len(key_people), len(posts))
        _look_up_key_people(conn, bto_run_id, key, listing, company, key_people)
        _mark_printed_addresses(people)
    finally:
        runs.record_costs()
    return _publish_people(conn, key, bto_run_id, people, posts, status="done")


def _publish_people(conn, key, bto_run_id, people, posts, *, status):
    """Replace the result and mark it successful together; failures keep the old result."""
    with conn.transaction():
        ids = write.replace_relevant_personnel(conn, key, [p.relevant_personnel_row() for p in people])
        write.replace_linkedin_posts(conn, key, _post_rows(posts, people, ids))
        write.finish_personnel_search(conn, bto_run_id, status=status)
    return status


def _mark_printed_addresses(people):
    """An address printed on the listing or in SGDI that no mailbox check
    covered is shown as seen online, unchecked."""
    for person in people:
        if person.printed_email and not person.email_kind:
            person.email_kind = "unchecked_seen_online"
    return people


def _find_people_at_company(company, listing, job_words, runs):
    """Steps 3 to 5 at the same time. Returns (people, posts). LinkedIn finds
    most of the people, so its failure fails the search; the web, SGDI and
    posts only add people, so their failures are logged and skipped."""
    locations = config.MARKET_LOCATIONS.get(listing["market"], [])
    place = config.place_name(listing["market"])
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="find-job-contacts") as pool:
        linkedin = pool.submit(on_linkedin.find_people_on_linkedin,
                               company, job_words, locations, runs)
        web = pool.submit(_people_on_web_and_in_sgdi, company, job_words, place)
        linkedin_posts = pool.submit(in_job_posts.find_posts_about_the_listing,
                                     company.name, (job_words or [""])[0], listing.get("title"), runs)
        on_web = _result_or_empty(web, "web and SGDI", ([], []))
        posts, post_people = _result_or_empty(linkedin_posts, "LinkedIn posts", ([], []))
        found_on_linkedin = linkedin.result()
    sgdi_people, web_people = on_web
    log.info("people search sources linkedin=%s web=%s sgdi=%s post_people=%s posts=%s",
             len(found_on_linkedin), len(web_people), len(sgdi_people),
             len(post_people), len(posts))
    return found_on_linkedin + sgdi_people + web_people + post_people, posts


def _people_on_web_and_in_sgdi(company, job_words, place):
    """Step 4: (SGDI officers, people found by web search). SGDI is asked
    only for public sector."""
    sgdi_people = (in_sgdi.find_agency_officers(company.name, job_words)
                   if company.is_public_sector else [])
    return sgdi_people, from_web.find_people_on_the_web(company.name, job_words, place)


def _result_or_empty(future, step, empty):
    """A step's result, or `empty` when the step failed; the failure is logged."""
    try:
        return future.result()
    except Exception as error:
        log_failure(log, "People search step failed step=%s; optional results skipped",
                    step, error=error, level=logging.WARNING)
        return empty


def _look_up_key_people(conn, bto_run_id, key, listing, company, key_people):
    """Steps 7 and 8: the email domain, then the full lookup of each key
    person, then the search's email line."""
    hunter = HunterClient(config.read_key(config.HUNTER_API_KEY), bto_run_id)
    email = find_emails.EmailDomain(None, None)
    try:
        credits_left = hunter.credits_left()
        email = find_emails.find_email_domain(
            company, read.direct_application_link(conn, key), hunter)
        if not email.domain:
            email_status = "no_trusted_domain"
        elif credits_left < config.HUNTER_HOLD_BACK_BELOW_CREDITS:
            email_status = "held_back"
        else:
            email_status = "checked"
    except HunterUnavailable as error:
        log_failure(log, "Email checks unavailable search=%s", bto_run_id,
                    error=error, level=logging.WARNING)
        email_status = "unavailable"

    lookup = Lookup(company=company, market=listing["market"], email_domain=email.domain,
                    hunter=hunter if email_status == "checked" else None, web_searches=1)
    for person in key_people:
        if is_half_name(person.name):
            continue                    # no surname to search by; Look up stays
        if lookup.hunter is not None:
            if hunter.last_credits_left < config.HUNTER_HOLD_BACK_BELOW_CREDITS:
                email_status, lookup.hunter = "held_back", None
            elif hunter.credits_used >= config.HUNTER_CREDITS_PER_PEOPLE_SEARCH:
                # The rest keep Look up; their email kind stays "not looked up".
                log.info("email checks stopped at the search's limit search=%s credits=%.1f",
                         bto_run_id, hunter.credits_used)
                lookup.hunter = None
        email_error = look_up_person(person, lookup)
        if email_error is not None:
            log_failure(log, "Email checks unavailable search=%s", bto_run_id,
                        error=email_error, level=logging.WARNING)
            email_status, lookup.hunter = "unavailable", None
    write.set_personnel_search_email(
        conn, bto_run_id, email_status=email_status, email_domain=email.domain,
        email_domain_source=email.source,
        email_domains_tried=email.tried if email_status == "no_trusted_domain" else None)
    log.info("people email lookup finished search=%s status=%s credits_used=%s",
             bto_run_id, email_status, getattr(hunter, "credits_used", None))


def look_up_person(person, lookup):
    """Look up one person's email, phone numbers and online accounts, and
    fill them into the record. Returns the Hunter failure when the email
    check could not be made, else None; everything else still runs."""
    company = lookup.company
    if company.is_public_sector:
        officer = in_sgdi.find_officer(person.name, company.name)
        if officer is not None and officer.email and not person.printed_email:
            person.printed_email = person.email_address = officer.email
        if officer is not None:
            person.channels += in_sgdi.officer_as_person(officer).channels
    email_error = None
    if lookup.hunter is not None:
        try:
            find_emails.look_up_email(person, lookup.email_domain, lookup.hunter,
                                      lookup.patterns)
        except HunterUnavailable as error:
            email_error = error
    # A contact named by its printed address or number has no name to search
    # for, and a hidden employer gives the web search nothing to match.
    if company.name and not re.search(r"[@\d]", person.name):
        pages = find_online_accounts.search_the_web_for_person(
            person.name, company.name, searches=lookup.web_searches)
        person.channels += find_phones.phone_numbers_near_name(person.name, pages, lookup.market)
        try:
            person.channels += find_online_accounts.find_online_accounts(
                person.name, company, pages, use_github=lookup.use_github)
        except github.GitHubUnavailable as error:
            log_failure(log, "GitHub unavailable; remaining people skip its lookup",
                        error=error, level=logging.WARNING)
            lookup.use_github = False
            person.channels += find_online_accounts.find_online_accounts(
                person.name, company, pages, use_github=False)
    person.channels = _ordered_channels(person.channels)
    person.looked_up_at = datetime.now(timezone.utc)
    return email_error


def _ordered_channels(channels):
    """Each channel once, phones first, in the People panel's order."""
    unique = {}
    for channel in channels:
        unique.setdefault((channel["kind"], channel["value"]), channel)
    return sorted(unique.values(), key=lambda c: CHANNEL_ORDER.index(c["kind"])
                  if c["kind"] in CHANNEL_ORDER else len(CHANNEL_ORDER))


def _post_rows(posts, people, ids):
    """`linkedin_posts` rows, with each author and each reactor or commenter
    at the company pointed at their saved `relevant_personnel` row."""
    saved = list(zip(people, ids))

    def person_id(name, url):
        for person, row_id in saved:
            if same_name(person.name, name) or (
                    linkedin_key(url) and linkedin_key(url) == linkedin_key(person.linkedin_url)):
                return row_id
        return None

    def people_rows(found):
        return [{"name": e["name"], "headline": e["headline"], "linkedin_url": e["linkedin_url"],
                 "person_id": person_id(e["name"], e["linkedin_url"])
                 if e["works_at_company"] else None} for e in found]

    rows, seen = [], set()
    for post in posts:
        author_id = person_id(post.author.name, post.author.linkedin_url)
        # linkedin_posts.author_id is required, so a post whose author has no
        # saved row is left out.
        if author_id is None or post.post_url in seen:
            continue
        seen.add(post.post_url)
        rows.append({
            "post_url": post.post_url, "author_id": author_id,
            "repost_of_name": post.repost_of_name, "posted_at": post.posted_at,
            "post_text": post.post_text,
            "reacted_by": people_rows(post.reacted_by),
            "commented_by": people_rows(post.commented_by),
        })
    return rows


def _read_listing(conn, key):
    """The stored listing for `key`, with the key's board, market and board_job_id added.
    Raises SearchFailed when the listing is no longer stored."""
    row = read.listing_for_research(conn, key)
    if row is None:
        raise SearchFailed("the listing is no longer stored")
    board, market, board_job_id = key
    return {**row, "board": board, "market": row.get("market") or market,
            "board_job_id": board_job_id}


# ── Look up ─────────────────────────────────────────────────────────────────

def look_up(person_id):
    """The full lookup for one person, on the admin's click. Fills their row
    in place and returns it as `status` shows a person. Credits are spent
    even below the hold-back level: the admin chose to spend. Raises
    LookupError when there is no such person."""
    with connect() as conn:
        row = read.relevant_person(conn, person_id)
        if row is None:
            raise LookupError(f"no person {person_id}")
        key = (row["board"], row["market"], row["board_job_id"])
        listing = _read_listing(conn, key)
        search = read.latest_personnel_search(conn, key) or {}
        # The search whose people are shown found this person, even while a
        # newer search waits or runs. Its credits include the Look ups.
        found_by = read.latest_finished_personnel_search(conn, key) or search
        direct_link = read.direct_application_link(conn, key)
        company = identify_company(conn, listing)
    hunter = HunterClient(config.read_key(config.HUNTER_API_KEY), found_by.get("bto_run_id"))
    domain, failure = search.get("email_domain"), None
    if not domain:
        try:
            domain = find_emails.find_email_domain(company, direct_link, hunter).domain
        except HunterUnavailable as error:
            failure = f"email check {error}"
    person = _person_from_row(row, _printed_address(listing, company, row["name"]))
    lookup = Lookup(company=company, market=listing["market"], email_domain=domain,
                    hunter=hunter if domain or person.printed_email else None,
                    web_searches=config.WEB_SEARCHES_PER_LOOK_UP)
    email_error = look_up_person(person, lookup)
    failure = failure or (f"email check {email_error}" if email_error else None)
    columns = {"email_address": person.email_address, "email_kind": person.email_kind,
               "email_confidence": person.email_confidence, "channels": person.channels,
               "failure_reason": failure}
    if failure is None:
        columns["looked_up_at"] = person.looked_up_at
    with connect() as conn:
        write.update_relevant_person(conn, person_id, **columns)
        return person_view(read.relevant_person(conn, person_id))


def _printed_address(listing, company, name):
    """The address the listing prints for this person, or None. Look up
    checks that address, as the search does, instead of looking for another.

    The listing is read again because a stored address may instead be one an
    earlier lookup found. A contact named by its own address had that address
    printed on the listing. An SGDI officer's address is read again by the
    lookup itself.
    """
    if "@" in name:
        return name
    for listed in in_listing.find_people_in_listing(listing, company.linkedin_copy):
        if listed.printed_email and same_name(listed.name, name):
            return listed.printed_email
    return None


def _person_from_row(row, printed_email):
    """The FoundPerson a stored row describes, for a Look up."""
    return FoundPerson(
        name=row["name"], found_by="stored", provenance=row["provenance"],
        title=row.get("title"), location=row.get("location"),
        linkedin_url=row.get("linkedin_url"), printed_email=printed_email,
        email_address=row.get("email_address"),
        email_kind=row.get("email_kind"), email_confidence=row.get("email_confidence"),
        channels=list(row.get("channels") or []), group=row.get("person_group"))


# ── what the People panel shows ─────────────────────────────────────────────

def status(key):
    """Everything the People panel and its job line show for one listing.

    Times are UTC datetimes; the page shows them as h:mm AM/PM Singapore
    time. `state` is the latest search's status, or "not_searched".
    """
    with connect() as conn:
        search = read.latest_personnel_search(conn, key)
        found_by = read.latest_finished_personnel_search(conn, key) or {}
        people = [person_view(row) for row in read.relevant_personnel(conn, key)]
        post_rows = read.linkedin_posts(conn, key)
        price = people_search_price(conn)
        queue = credits = None
        if search and search["status"] == "queued":
            queue = read.queue_position(conn, TABLE, search["bto_run_id"])
        if search:
            credits = read.hunter_credits_for_search(conn, search["bto_run_id"])
    search = search or {}
    state = search.get("status") or "not_searched"
    by_id = {person["id"]: person for person in people}
    position, total, ahead = queue or (None, None, None)
    return {
        "state": state,
        "bto_run_id": search.get("bto_run_id"),
        "requested_at": search.get("requested_at"),
        "started_at": search.get("started_at"),
        "finished_at": search.get("finished_at"),
        "failure_reason": search.get("failure_reason"),
        "found": sum(1 for person in people if person["person_group"]),
        "email_status": search.get("email_status"),
        "email_domain": search.get("email_domain"),
        "email_line": find_emails.email_domain_line(
            search.get("email_status"), search.get("email_domain"),
            search.get("email_domain_source"), search.get("email_domains_tried") or []),
        "email_checks_used": credits,
        "queue_position": position,
        "queue_total": total,
        "queue_ahead": ahead,
        "about_usd": price,
        # Keep earlier results visible, with their own search time, during a retry.
        "showing_earlier_results": state in ("queued", "searching") and bool(people),
        "results_searched_at": found_by.get("finished_at"),
        "groups": [{"group": group, "label": label,
                    "people": [p for p in people if p["person_group"] == group]}
                   for group, label in GROUP_LABELS.items()],
        "posts": [_post_view(row, by_id) for row in post_rows],
    }


def people_search_price(conn):
    """The "about $X" beside Run now and Find people: the average recorded cost
    of the latest searches, or the owner's estimate until enough have one."""
    average = read.average_people_search_cost_usd(
        conn, last=config.PEOPLE_SEARCHES_IN_AVERAGE_COST)
    return round(average if average is not None else config.PEOPLE_SEARCH_COST_ESTIMATE_USD, 2)


def person_view(row):
    """A `relevant_personnel` row with the words and states the panel needs."""
    view = dict(row)
    view["lookup_state"] = ("failed" if row.get("failure_reason")
                            else "done" if row.get("looked_up_at") else "not_looked_up")
    view["is_half_name"] = is_half_name(row.get("name"))
    view["provenance_label"] = PROVENANCE_LABELS.get(row.get("provenance"), row.get("provenance"))
    return view


def _post_view(row, people_by_id):
    def people_of(entries):
        return [{**entry,
                 "works_at_company": entry.get("person_id") is not None,
                 "listed_above": bool((people_by_id.get(entry.get("person_id")) or {})
                                      .get("person_group")),
                 "person": people_by_id.get(entry.get("person_id"))}
                for entry in entries or []]

    return {**row, "author": people_by_id.get(row.get("author_id")),
            "reacted_by": people_of(row.get("reacted_by")),
            "commented_by": people_of(row.get("commented_by"))}


# ── command line ────────────────────────────────────────────────────────────

def main(argv=None):
    """Queue eligible saved jobs from the command line and process the people queue."""
    parser = argparse.ArgumentParser(
        description="Find who to contact about starred job listings saved to To Apply.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--starred-to-apply", action="store_true",
                       help="queue every starred To Apply listing that has never been "
                            "searched, then run the queue here")
    which.add_argument("--listing", nargs=3, metavar=("BOARD", "MARKET", "BOARD_JOB_ID"),
                       help="search one To Apply listing by hand, then run the queue here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                        stream=sys.stdout, force=True)
    settings.load_postgres()
    with connect() as conn:
        if args.listing:
            keys, by_hand = [tuple(args.listing)], True
        else:
            keys = [(row["board"], row["market"], row["board_job_id"])
                    for row in read.prioritised_listings(conn, starred_only=True)]
            by_hand = False
        outcomes = {}
        for key in keys:
            word = queue_search(conn, key, by_hand=by_hand)
            outcomes[word] = outcomes.get(word, 0) + 1
    log.info("people search requests: %s",
             ", ".join(f"{word}={count}" for word, count in sorted(outcomes.items())) or "none")
    ran = run_queued_searches()
    if ran is None:
        log.info("another process holds the people-search queue; it will run the queued searches")
    else:
        log.info("people searches run here: %d", ran)
    return 0


if __name__ == "__main__":
    sys.exit(main())
