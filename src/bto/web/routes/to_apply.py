"""Show saved jobs and their direct links and people-search results.

Poll active searches and let admins request searches or contact lookups.
Review actions queue automatic searches after the save or star has committed."""

import logging
import threading
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from bto.filter_and_bucket_job_listings import BUCKET_LABELS, BUCKETS
from bto.find_job_contacts import config as find_job_contacts_config
from bto.find_job_contacts import run as find_job_contacts
from bto.log_errors import log_failure
from bto.research_job_listing import run as research_job_listing
from bto.storage.postgres.connection import connect
from bto.storage.postgres.read import (
    average_search_seconds,
    current_review_listing,
    direct_application_link,
    latest_personnel_search,
    relevant_personnel,
    review_group_key,
    saved_review_candidates,
)
from bto.storage.postgres.write import change_review_state
from bto.web import review_cache
from bto.web.auth import admin_required, login_required
from bto.web.review_job_listings import BOARD_LABELS, display_review_state
from bto.web.routes import review as review_routes
from bto.web.saved_job_controls import filter_saved_jobs

blueprint = Blueprint("to_apply", __name__)
log = logging.getLogger(__name__)

# Map each accepted search kind to its owning workflow.
SEARCHES = {"links": research_job_listing, "people": find_job_contacts}
# Poll only searches that are queued or running.
WAITING_OR_RUNNING = ("queued", "searching")


@blueprint.get("/to-apply")
@login_required
def to_apply():
    """Load saved jobs, apply list filters and show the selected job's search results."""
    try:
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            states = review_routes.live_states(conn)
            if states is None:
                return render_template(
                    "to_apply.html",
                    jobs=[],
                    selected=None,
                    review_state_available=False,
                    to_apply_count="—",
                )
            candidates = saved_review_candidates(conn, states)
        jobs = saved_jobs_in_list_order(candidates, states)
        saved_count = len(jobs)
        priority_count = sum(bool(job["review_state"].get("starred_at")) for job in jobs)
        # Run all counts every saved job, whatever the list's filters show.
        lines = read_list_lines(jobs, states)
        run_bar = run_all_bar(with_lines(jobs, lines))
        jobs = filter_saved_jobs(jobs, request.args)
        identifier = int(request.args["id"]) if request.args.get("id") else None
        selected = next((j for j in jobs if j["id"] == identifier), jobs[0] if jobs else None)
        if selected:
            selected = {
                **selected,
                **(
                    review_cache.selected_listing(
                        current_app.config["PG_CONNECT"], selected["id"], review_routes.hot.snapshot
                    )
                    or {}
                ),
            }
        jobs, selected = add_search_results(jobs, selected, states, lines)
        g.review_counts = {"to_apply": saved_count}
        return render_template(
            "to_apply.html",
            jobs=jobs,
            selected=selected,
            board_labels=BOARD_LABELS,
            bucket_labels=BUCKET_LABELS,
            to_apply_count=saved_count,
            saved_count=saved_count,
            priority_count=priority_count,
            review_state_available=True,
            today_sgt=today_in_singapore(),
            run_bar=run_bar,
        )
    except ValueError:
        abort(400)
    except Exception as error:
        log_failure(log, "Saved Review jobs unavailable", error=error)
        return render_template(
            "to_apply.html", jobs=[], selected=None, error="Saved jobs are temporarily unavailable."
        ), 503


@blueprint.post("/to-apply/action")
@admin_required
def action():
    data = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(data, "get"):
        abort(400)
    if data.get("action") != "remove":
        abort(400)
    try:
        with current_app.config["PG_CONNECT"]() as conn:
            if (
                conn.execute("SELECT to_regclass('job_tracking.review_status')").fetchone()[0]
                is None
            ):
                return jsonify(error="Review state is not set up; changes are unavailable."), 409
            change_review_state(conn, int(data["id"]), "saved_to_apply_at", False)
    except (KeyError, ValueError, TypeError):
        abort(400)
    if request.is_json:
        return jsonify(removed=True)
    return redirect(url_for("to_apply.to_apply"))


@blueprint.get("/to-apply/research")
@login_required
def research():
    """One saved job's sections and list line, polled while a search waits or runs."""
    try:
        identifier = int(request.args["id"])
    except (KeyError, ValueError):
        abort(400)
    job, key = find_job_and_key(identifier)
    if not searches_use_this_database():
        return jsonify(render_search_sections(job, None, None))
    start_workers()
    return jsonify(render_search_sections(job, *read_search_status(key)))


@blueprint.post("/to-apply/research/run")
@admin_required
def request_search():
    """Run now, Search again and Retry: queue a new search for one saved job."""
    data = request.get_json(silent=True)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("kind"), str)
        or data["kind"] not in SEARCHES
    ):
        abort(400)
    try:
        identifier = int(data["id"])
    except (KeyError, TypeError, ValueError):
        abort(400)
    job, key = find_job_and_key(identifier)
    if not searches_use_this_database():
        return jsonify(error="Searches are unavailable on this server."), 409
    start_workers()
    kind = data["kind"]
    try:
        outcome = SEARCHES[kind].request(key, by_hand=True)
    except Exception as error:
        log_failure(log, "Search not started kind=%s", kind, error=error)
        return jsonify(error="Could not start the search. Try again."), 503
    log.info(
        "Search requested by hand kind=%s board=%s market=%s board_job_id=%s outcome=%s",
        kind,
        *key,
        outcome,
    )
    sections = {**render_search_sections(job, *read_search_status(key)), "outcome": outcome}
    refusal = refusal_message(kind, outcome)
    if refusal:
        return jsonify(**sections, error=refusal), 409
    return jsonify(**sections)


@blueprint.post("/to-apply/research/look-up")
@admin_required
def look_up():
    """Look up one person's email, phone numbers and accounts while the admin waits."""
    data = request.get_json(silent=True)
    try:
        person_id = int(data["person_id"])
    except (KeyError, TypeError, ValueError):
        abort(400)
    if not searches_use_this_database():
        return jsonify(error="Look up is unavailable on this server."), 409
    start_workers()
    try:
        person = find_job_contacts.look_up(person_id)
    except Exception as error:
        # look_up raises LookupError itself when a newer search replaced the person;
        # its subclasses KeyError and IndexError are real failures.
        if type(error) is LookupError:
            return jsonify(error="A newer search replaced this person. Reload the page."), 404
        log_failure(log, "Look up failed person_id=%s", person_id, error=error)
        return jsonify(error="Look up failed. Try again."), 503
    try:
        people = find_job_contacts.status(
            (person["board"], person["market"], person["board_job_id"])
        )
    except Exception as error:
        log_failure(log, "Contact finding status unavailable", error=error)
        people = None
    page = page_macros()
    return jsonify(
        row=str(page.person_row(person, people, True)),
        email_cell=str(page.email_cell(person, people, admin=True, brief=True)),
    )


def start_searches_after_review_change(action, active, state):
    """Queue links for a newly saved job and people for a saved, starred job.

    The state change has committed. Search failures are logged separately, and
    existing results prevent repeated save or star actions from spending again."""
    if not active or action not in ("star", "save", "save_star", "queue"):
        return
    key = anchor_key(state)
    if key is None or state.get("saved_to_apply_at") is None or not searches_use_this_database():
        return
    if action in ("save", "save_star", "queue"):
        request_automatic_search("job-listing research", research_job_listing, key)
    if state.get("starred_at") is not None:
        request_automatic_search("contact finding", find_job_contacts, key)


def request_automatic_search(name, search, key):
    try:
        outcome = search.request(key)
    except Exception as error:
        log_failure(
            log, "Automatic %s not started board=%s market=%s board_job_id=%s",
            name, *key, error=error,
        )
        return
    log.info(
        "Automatic %s requested board=%s market=%s board_job_id=%s outcome=%s",
        name,
        *key,
        outcome,
    )


def searches_use_this_database():
    """Allow searches only when they use the same database as the page."""
    return current_app.config["PG_CONNECT"] is connect


def anchor_key(state):
    """Return the Review group's first retained board-listing identity."""
    anchors = (state or {}).get("anchors") or []
    if not anchors:
        return None
    return anchors[0]["board"], anchors[0]["market"], anchors[0]["board_job_id"]


def listing_key(job, states):
    """Resolve the job's live Review state and return its retained listing identity."""
    identity = (job["board"], job["market"], job["board_job_id"])
    return anchor_key(states.get(identity, states.get(review_group_key(job))))


def start_workers():
    """Start both search workers when a page or search request needs them."""
    try:
        research_job_listing.start_worker()
        find_job_contacts.start_worker()
    except Exception as error:
        log_failure(log, "Search workers not started", error=error)


def find_job_and_key(identifier):
    """Load a listing and its retained Review identity, or return a 404 or 503 response.

    The search workflow separately checks whether the job is saved."""
    try:
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            states = review_routes.live_states(conn) or {}
            try:
                job = current_review_listing(conn, identifier)
            except ValueError:
                job = None
    except Exception as error:
        log_failure(log, "Saved job unavailable", error=error)
        abort(503, description="This job is temporarily unavailable.")
    key = listing_key(job, states) if job else None
    if key is None:
        abort(404)
    return {**job, "review_state": display_review_state(job, states)}, key


def read_search_status(key):
    """Read links and people status, retaining either result if the other read fails."""
    results = []
    for name, search in (
        ("Job-listing research", research_job_listing),
        ("Contact finding", find_job_contacts),
    ):
        try:
            results.append(search.status(key))
        except Exception as error:
            log_failure(log, "%s status unavailable", name, error=error)
            results.append(None)
    return tuple(results)


def read_list_line_status(keys):
    """Read the compact search summaries for all saved-job rows on one connection."""
    lines = {}
    try:
        with current_app.config["PG_CONNECT"]() as conn:
            price = find_job_contacts.people_search_price(conn)
            for identifier, key in keys.items():
                if key is None:
                    continue
                link = direct_application_link(conn, key)
                search = latest_personnel_search(conn, key)
                people_state = search["status"] if search else "not_searched"
                # Counted as status() counts it: the people in a People group.
                found = (
                    sum(1 for person in relevant_personnel(conn, key) if person["person_group"])
                    if people_state == "done"
                    else 0
                )
                lines[identifier] = (
                    # The vacancy page tells a full find from "Job board only".
                    {"state": link["status"] if link else "not_searched",
                     "vacancy_url": link.get("vacancy_url") if link else None},
                    {"state": people_state, "found": found, "about_usd": price},
                )
    except Exception as error:
        log_failure(log, "To Apply list lines unavailable", error=error)
        return {}
    return lines


def saved_jobs_in_list_order(candidates, states):
    """Choose one listing per Review group and order saved jobs by star, bucket and save date."""
    grouped = {}
    for job in sorted(
        candidates,
        key=lambda j: (
            j["repeat_of_id"] is not None,
            BUCKETS.index(j["bucket"]) if j["bucket"] in BUCKETS else len(BUCKETS),
            -j["wording_first_seen_at"].timestamp(),
            -j["id"],
        ),
    ):
        grouped.setdefault(review_group_key(job), job)
    jobs = [
        {**job, "review_state": display_review_state(job, states)} for job in grouped.values()
    ]
    jobs.sort(
        key=lambda j: (
            j["review_state"]["starred_at"] is None,
            BUCKETS.index(j["bucket"]) if j["bucket"] in BUCKETS else len(BUCKETS),
            j["review_state"]["saved_to_apply_at"],
        )
    )
    return jobs


def read_list_lines(jobs, states):
    """Read saved-job summaries only when searches use this page's database."""
    if not searches_use_this_database():
        return {}
    return read_list_line_status({job["id"]: listing_key(job, states) for job in jobs})


def with_lines(jobs, lines):
    """Attach compact links and people summaries to each saved-job row."""
    return [
        {**job, **dict(zip(("links", "people"), lines.get(job["id"], (None, None))))}
        for job in jobs
    ]


def add_search_results(jobs, selected, states, lines=None):
    """Attach list summaries and load the selected job's full links and people results."""
    if not searches_use_this_database():
        return jobs, selected
    start_workers()
    lines = dict(read_list_lines(jobs, states) if lines is None else lines)
    if selected:
        key = listing_key(selected, states)
        links, people = read_search_status(key) if key else (None, None)
        selected = {**selected, "links": links, "people": people}
        lines[selected["id"]] = (links, people)
    return with_lines(jobs, lines), selected


def render_search_sections(job, links, people):
    """Render the search panels and list row replaced by browser polling."""
    admin = g.user["role"] == "admin"
    board_label = BOARD_LABELS.get(job["board"], job["board"])
    page = page_macros()
    return {
        "strip": str(page.information_strip(job, links, people, admin, board_label)),
        "links": str(page.links_section(job, links, admin, board_label)),
        "people": str(page.people_section(job, people, admin)),
        "line": str(page.list_line(job, links, people, admin)),
        "running": any(
            (status or {}).get("state") in WAITING_OR_RUNNING for status in (links, people)
        ),
    }


def page_macros():
    """Load the search-panel template macros without consuming flashed messages."""
    context = {
        "get_flashed_messages": lambda *args, **kwargs: [],
        "today_sgt": today_in_singapore(),
    }
    current_app.update_template_context(context)
    return current_app.jinja_env.get_template("to_apply.html").make_module(context)


def today_in_singapore():
    """Format today's Singapore date the same way as the page's time labels."""
    return current_app.jinja_env.filters["sgt"](datetime.now(timezone.utc)).split(" · ")[0]


def refusal_message(kind, outcome):
    """Explain why a manual search request queued no work."""
    # Contact finding's request() returns the same two words as these constants.
    if outcome == research_job_listing.NOT_ELIGIBLE:
        return "Only jobs saved to To Apply are searched."
    if outcome == research_job_listing.DAILY_LIMIT_REACHED:
        limit = (
            research_job_listing.daily_limit()
            if kind == "links"
            else find_job_contacts_config.PEOPLE_SEARCHES_PER_DAY
        )
        return f"Today's limit of {limit} searches is reached. Try again tomorrow."
    return None


# Run all tracks the requests made by this process for its progress bar.
# The two workflows own their durable queues and each runs one search at a time.
RUN_ALL_WAITING = ("not_searched", "failed")
RUN_ALL_LINK_USD = 0.005  # one paid web search; most link searches cost less
RUN_ALL_SECONDS = {"links": 30, "people": 140}  # fallback estimates without completed runs
RUN_ALL = {"started_at": None, "total": 0, "items": None, "asking": False, "last": None}
_run_all_lock = threading.Lock()


@blueprint.post("/to-apply/run-all")
@admin_required
def run_all():
    """Queue the eligible searches and let their own workers apply spending guards."""
    if not searches_use_this_database():
        return jsonify(error="Searches are unavailable on this server."), 409
    jobs, states = load_saved_jobs()
    if states is None:
        return jsonify(error="Review state is unavailable, so nothing was started."), 503
    steps = run_all_steps(with_lines(jobs, read_list_lines(jobs, states)))
    with _run_all_lock:
        if RUN_ALL["items"] is not None:
            return jsonify(error="Run all is already running."), 409
        if not steps:
            return jsonify(error="Nothing to run."), 409
        RUN_ALL.update(started_at=datetime.now(timezone.utc), total=len(steps), items=[], asking=True)
    start_workers()
    try:
        for kind, job in steps:
            key = listing_key(job, states)
            try:
                outcome = SEARCHES[kind].request(key, by_hand=True)
            except Exception as error:
                log_failure(log, "Run all search not started kind=%s", kind, error=error)
                outcome = None
            with _run_all_lock:
                RUN_ALL["items"].append({"kind": kind, "key": key, "label": run_all_label(kind, job),
                                         "asked": outcome in WAITING_OR_RUNNING})
    finally:
        with _run_all_lock:
            RUN_ALL["asking"] = False
    asked = sum(item["asked"] for item in RUN_ALL["items"])
    log.info("Run all asked for searches=%d of %d", asked, len(steps))
    return jsonify(asked=asked, total=len(steps))


@blueprint.get("/to-apply/run-all")
@login_required
def run_all_status():
    """Return the latest Run all controls for browser polling."""
    bar = run_all_bar(None)
    if bar is not None and not bar["running"]:
        jobs, states = load_saved_jobs()
        bar = run_all_bar(with_lines(jobs, read_list_lines(jobs, states))) if states else None
    page = page_macros()
    return jsonify(bar=str(page.run_all_controls(bar, g.user["role"] == "admin")),
                   running=bool(bar and bar["running"]))


def load_saved_jobs():
    """Read saved jobs and live Review state in one read-only transaction."""
    with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        states = review_routes.live_states(conn)
        if states is None:
            return [], None
        candidates = saved_review_candidates(conn, states)
    return saved_jobs_in_list_order(candidates, states), states


def run_all_steps(jobs):
    """Choose unsearched or failed links, then people searches for starred jobs."""
    links = [("links", job) for job in jobs
             if (job.get("links") or {}).get("state") in RUN_ALL_WAITING]
    people = [("people", job) for job in jobs
              if job["review_state"].get("starred_at")
              and (job.get("people") or {}).get("state") in RUN_ALL_WAITING]
    return links + people


def run_all_label(kind, job):
    company = job.get("advertiser_name") or "this job"
    return f"Finding direct link for {company}" if kind == "links" else f"Searching people for {company}"


def run_all_bar(jobs):
    """Show running search progress, or estimate the eligible work and its cost."""
    if not searches_use_this_database():
        return None
    try:
        with current_app.config["PG_CONNECT"]() as conn:
            seconds = {
                kind: average_search_seconds(conn, package.TABLE) or RUN_ALL_SECONDS[kind]
                for kind, package in SEARCHES.items()
            }
            progress = run_all_progress(conn, seconds)
            price = None if progress else find_job_contacts.people_search_price(conn)
    except Exception as error:
        log_failure(log, "Run all bar unavailable", error=error)
        return None
    if progress:
        return progress
    steps = run_all_steps(jobs or [])
    count = {kind: sum(step_kind == kind for step_kind, _ in steps) for kind in SEARCHES}
    cost = count["links"] * RUN_ALL_LINK_USD + count["people"] * price
    with _run_all_lock:
        last = RUN_ALL["last"]
    return {"running": False, "nothing": not steps, "summary": run_all_summary(count, cost, seconds),
            "last": last}


def run_all_progress(conn, seconds):
    """Read the queued searches and retain a summary once they have all finished."""
    with _run_all_lock:
        if RUN_ALL["items"] is None:
            return None
        started_at, total, asking = RUN_ALL["started_at"], RUN_ALL["total"], RUN_ALL["asking"]
        items = list(RUN_ALL["items"])
    read_row = {"links": direct_application_link, "people": latest_personnel_search}
    rows = [read_row[item["kind"]](conn, item["key"]) or {} if item["asked"] else {} for item in items]
    waiting = [(item, row) for item, row in zip(items, rows)
               if item["asked"] and row.get("status") in WAITING_OR_RUNNING]
    ended = [row for item, row in zip(items, rows)
             if row.get("finished_at") and row["finished_at"] >= started_at]
    failed = sum(not item["asked"] for item in items) + sum(
        row.get("status") == "failed" for row in ended)
    done = len(items) - len(waiting)
    if not waiting and not asking:
        with _run_all_lock:
            RUN_ALL.update(started_at=None, total=0, items=None, last={
                "at": started_at, "total": total, "failed": failed,
                "end": max((row["finished_at"] for row in ended), default=datetime.now(timezone.utc)),
            })
        return None
    searching = [item["label"] for item, row in waiting if row.get("status") == "searching"]
    left = {kind: sum(item["kind"] == kind for item, _ in waiting) * seconds[kind] for kind in SEARCHES}
    left["links"] += (total - len(items)) * seconds["links"]
    return {
        "running": True, "started_at": started_at, "done": done, "total": total,
        "pct": round(done * 100 / total) if total else 0, "left": about_minutes(max(left.values())),
        "now": ("Now: " + " · ".join(searching)) if searching
        else ("Next: " + waiting[0][0]["label"]) if waiting else "Asking for the searches…",
    }


def run_all_summary(count, cost, seconds):
    """Describe the estimated work, cost and duration of the two search queues."""
    if not any(count.values()):
        return "Every direct link and ★ people search has run."
    parts = [
        f"{count['links']} direct link{'' if count['links'] == 1 else 's'}" if count["links"] else "",
        f"{count['people']} people search{'' if count['people'] == 1 else 'es'}" if count["people"] else "",
    ]
    money = "under $0.01" if cost < 0.01 else f"about ${cost:.2f}"
    time = max(count[kind] * seconds[kind] for kind in SEARCHES)
    return "To run: " + " · ".join(part for part in parts if part) + f" · {money} · {about_minutes(time)}"


def about_minutes(seconds):
    minutes = round(seconds / 60)
    return "under a minute" if minutes < 1 else f"about {minutes} min"
