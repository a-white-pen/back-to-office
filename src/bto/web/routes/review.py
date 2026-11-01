"""Serve the Review page, listing details and wording comparisons.

Use review_job_listings.py to prepare data, then render templates or return
JSON. Require an admin account for actions that change Review."""

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from bto.filter_and_bucket_job_listings import BUCKET_LABELS, SINGAPORE, night
from bto.filter_and_bucket_job_listings.evaluate_job_requirements import normalize_name
from bto.log_errors import log_failure
from bto.storage.postgres.read import (
    REVIEW_TIMESTAMPS,
    latest_personnel_search,
    review_group_key,
    review_states,
    saved_review_candidates,
)
from bto.storage.postgres.write import change_review_state
from bto.web import review_cache, review_table
from bto.web.auth import admin_required, login_required
from bto.web.review_empty import alternatives
from bto.web.review_job_listings import (
    BOARD_LABELS,
    MARKETS,
    display_review_state,
    resolved_review_states,
    review_filter_state,
    review_state_page,
    wording_diff,
)

blueprint = Blueprint("review", __name__)
log = logging.getLogger(__name__)


@blueprint.app_template_filter("requirement_groups")
def requirement_groups(lines):
    """Group matching display labels while keeping every distinct supporting requirement."""
    groups, seen = {}, set()
    for line in lines:
        name = normalize_name(line["detail"])
        evidence = (name, line["quote"], line["type"], line["status"], line.get("note"))
        if evidence in seen:
            continue
        seen.add(evidence)
        groups.setdefault(name, {"detail": line["detail"], "lines": []})["lines"].append(line)
    for group in groups.values():
        notes = {line.get("note") for line in group["lines"]}
        group["note"] = next(iter(notes)) if len(notes) == 1 else None
        statuses = list(dict.fromkeys(line["status"] for line in group["lines"]))
        if len(statuses) > 1:
            group["note"] = " / ".join(
                "not clear if required" if status == "unknown" else status
                for status in statuses
            )
    return list(groups.values())


@blueprint.app_template_filter("requirement_rows")
def requirement_rows(groups, section, assessment=None):
    """Combine asks that share one quote and note, while retaining separate evidence."""
    rows, phrases = [], {}
    for group in groups:
        if len(group["lines"]) > 1:
            notes = {requirement_note(line, section, assessment) for line in group["lines"]}
            rows.append({**group, "note": notes.pop()} if len(notes) == 1 else group)
            continue
        line = group["lines"][0]
        key = (line["quote"], requirement_note(line, section, assessment))
        if key in phrases:
            phrases[key]["detail"] += ", " + group["detail"]
            continue
        phrases[key] = {**group, "note": key[1]}
        rows.append(phrases[key])
    return rows


YEARS_NOTE = re.compile(
    r"asks [\d.]+ years, (?:your (comfortable|stretch) is ([\d.]+)|past your limit of ([\d.]+))"
)


def requirement_note(line, section, assessment=None):
    """Write the panel note for a requirement without changing its assessment."""
    note, kind = line.get("note"), line["type"]
    years = YEARS_NOTE.fullmatch(note or "") if kind == "years" else None
    if kind == "years" and note == "years met":
        return "met"
    if years:
        zone, number, limit = years.groups()
        if limit:
            return f"past limit, your limit is {limit}"
        if zone == "comfortable":
            return f"a little over, your comfortable is {number}"
        return f"long shot, your stretch is {number}"
    held = {"tool": "you have", "language": "you meet it", "work_rights": "you meet it"}
    if section == "for" and kind in held:
        return held[kind] + (" · " + note if note else "")
    walls = {
        normalize_name(name)
        for names in ((assessment or {}).get("walls") or {}).values()
        for name in names
    }
    if section == "against" and kind == "tool" and normalize_name(line["detail"]) not in walls:
        return "not on your list" + (" · " + note if note else "")
    if line["status"] == "preferred" and not note:
        return "preferred"
    return note


@blueprint.app_template_filter("reason_quote")
def reason_quote(assessment, reason):
    """Find the JD quote named by the bucket reason, when one exists."""
    if not assessment or not reason:
        return None
    walls = {
        normalize_name(name) for names in (assessment.get("walls") or {}).values() for name in names
    }
    lacks = {normalize_name(name) for name in assessment.get("lacks") or []}
    types = {"internship": "internship", "students or new graduates only": "students_only"}

    def named(line):
        name = normalize_name(line["detail"])
        if reason == assessment.get("years_reason"):
            return line["type"] == "years" and line.get("note") == reason
        if reason in types:
            return line["type"] == types[reason]
        if reason.startswith("lacks "):
            return name in lacks and line["detail"] in reason
        if reason == "requires a doctorate":
            return line["type"] == "education"
        if reason.startswith(("requires ", "work rights: ")):
            return name in walls and line["detail"] in reason
        if re.fullmatch(r"[\d.]+-month contract in [A-Z]{2}", reason):
            return line["type"] == "contract"
        return False

    return next((line["quote"] for line in assessment.get("against") or [] if named(line)), None)


@blueprint.app_template_filter("board_date")
def board_date(value):
    """Display the board's supplied date without reinterpreting it as a collection night."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%-d %b")
    except (ValueError, TypeError):
        return value


@blueprint.app_template_filter("seen_age")
def seen_age(value):
    if not value:
        return ""
    age = (datetime.now(SINGAPORE).date() - night(value)).days
    return age_words(age) if age >= 0 else ""


@blueprint.app_template_filter("night_date")
def night_date(value):
    """The collection night a timestamp belongs to, to compare with the latest one."""
    return night(value) if value else None


@blueprint.app_template_filter("seen_words")
def seen_words(value):
    """Describe first seen as today, yesterday or a number of collection nights ago."""
    if not value:
        return "—"
    days = (datetime.now(SINGAPORE).date() - night(value)).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


@blueprint.app_template_filter("new_jobs")
def new_jobs(count):
    """' · 182 new jobs' for a night's First seen hover, or nothing without a count."""
    return f" · {count:,} new job{'' if count == 1 else 's'}" if count else ""


# First-seen hover counts come from the snapshot, without a database read.
# Retain its identity and counts only, so replaced snapshots can be released.
_night_counts = {"key": None, "counts": {}}
_night_counts_lock = threading.Lock()


def new_jobs_by_night(snapshot):
    """Count first-seen jobs for each night covered by the snapshot."""
    if snapshot is None:
        return {}
    key = (id(snapshot), snapshot.built_at)
    with _night_counts_lock:
        if _night_counts["key"] == key:
            return _night_counts["counts"]
    counts = {}
    for row in snapshot.rows:
        day = night(row["listing_first_seen_at"]).isoformat()
        counts[day] = counts.get(day, 0) + 1
    with _night_counts_lock:
        _night_counts.update(key=key, counts=counts)
    return counts


def review_url(controls=None, **changes):
    values = {key: values for key, values in request.args.lists() if any(values)}
    if controls:
        for legacy in ("market", "bucket", "days", "board"):
            values.pop(legacy, None)
        values.update(controls["query"])
    if "id" in changes and str(changes["id"]) != request.args.get("id"):
        values.pop("advertiser_scope", None)
        values.pop("advertiser_days", None)
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return url_for("review.review", **values)


def render_review(page):
    """Render the selected job, filters, counts and table controls."""
    g.review_counts = {"to_apply": page.get("to_apply_count")}
    controls = page.get("filters")
    if controls:
        defaults = review_filter_state({})
        same_filters = all(
            controls.get(key) == defaults[key]
            for key in ("markets", "boards_off", "buckets", "default_scope", "seen")
        )
        if (
            same_filters
            and request.args.get("show", "unread") == "unread"
            and not any(request.args.get(key) for key in ("all", "advertiser", "description", "q"))
        ):
            g.review_counts["review"] = page.get("total")
    page.setdefault("review_state_available", False)
    if controls is None:
        try:
            controls = review_filter_state(request.args)
        except ValueError:
            controls = review_filter_state({})
        if page.get("total") is None:
            controls["seen_label"] = "Unavailable"

    def page_url(**changes):
        return review_url(controls, **changes)

    def toggle_filter(dimension, value):
        selected = controls[dimension]
        selected = [v for v in selected if v != value] if value in selected else [*selected, value]
        return page_url(**{dimension: ",".join(selected) or "none"}, page=1, id=None)

    def toggle_board(pair):
        excluded = controls.get("boards_off", [])
        excluded = [p for p in excluded if p != pair] if pair in excluded else [*excluded, pair]
        return page_url(boards_off=",".join(excluded) or None, board=None, page=1, id=None)

    pairs = controls.get("board_pairs", [])
    board_groups = [
        {
            "market": market,
            "boards": [
                {
                    "key": market + ":" + board,
                    "label": label,
                    "selected": market + ":" + board not in controls.get("boards_off", []),
                    "count": controls.get("board_counts", {}).get(market + ":" + board, 0),
                }
                for board, label in BOARD_LABELS.items()
                if market + ":" + board in pairs
            ],
        }
        for market in controls["markets"]
    ]
    historical_markets = [m for m in controls.get("available_markets", []) if m not in MARKETS]
    oos = ("title_filter", "non_tech", "tech_not_for_me")
    selected = controls["buckets"]
    selected = (
        [b for b in selected if b not in oos]
        if any(b in selected for b in oos)
        else [*selected, *oos]
    )
    try:
        table = review_table.presentation(request.args, page.get("page", 1), page.get("total") or 0)
    except ValueError:
        table = review_table.presentation({"view": request.args.get("view")}, 1, 0)
    return render_template(
        "review.html",
        **{**page, "filters": controls},
        review_navigation=controls.get("review"),
        bucket_labels=BUCKET_LABELS,
        page_url=page_url,
        toggle_filter=toggle_filter,
        toggle_board=toggle_board,
        board_groups=board_groups,
        board_off_count=sum(
            pair.split(":")[0] in controls["markets"] for pair in controls.get("boards_off", [])
        ),
        board_labels=BOARD_LABELS,
        historical_markets=historical_markets,
        show_historical_markets=controls["window"] == "any"
        or any(m in controls["markets"] for m in historical_markets),
        out_of_scope_url=page_url(buckets=",".join(selected) or "none", page=1, id=None),
        latest_night=controls.get("latest_night"),
        night_counts=new_jobs_by_night(getattr(g, "review_snapshot", None)),
        table=table,
        relaxations=alternatives(
            request.args, getattr(g, "review_snapshot", None), getattr(g, "review_states", None)
        )
        if page.get("total") == 0 and not page.get("error") and request.args.get("view") != "table"
        else [],
    )


STATEMENT_TIMEOUT = os.environ.get("BTO_REVIEW_STATEMENT_TIMEOUT", "90s")
CACHE_SECONDS = int(os.environ.get("BTO_REVIEW_CACHE_SECONDS", "0"))
WAIT_SECONDS = int(os.environ.get("BTO_REVIEW_WAIT_SECONDS", "60"))
HOT_CACHE = os.environ.get("BTO_REVIEW_HOT_CACHE") == "1"
_cache = review_cache.fallback_cache
_reading = threading.BoundedSemaphore(1)
hot = review_cache.HotCache(gate=_reading)


class ReviewBusy(Exception):
    """Another Review read is still running."""


def _read_page(args, states, *, fresh=False):
    """Read a bounded database page, with an optional cache keyed by live owner state."""
    version = hashlib.sha256(
        json.dumps(
            {
                key: {
                    **{f: value.get(f) for f in REVIEW_TIMESTAMPS},
                    "members": sorted(value.get("members", [])),
                }
                for key, value in states.items()
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    key = (tuple(sorted(args.items(multi=True))), version)
    now = time.monotonic()
    hit = _cache.get(key)
    if CACHE_SECONDS and hit and not fresh and now - hit[0] < CACHE_SECONDS:
        return copy.deepcopy(hit[1])
    try:
        if not _reading.acquire(timeout=WAIT_SECONDS):
            raise ReviewBusy
        try:
            with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute(
                    f"SET LOCAL statement_timeout = '{int(STATEMENT_TIMEOUT.rstrip('s'))}s'"
                )
                conn.execute("SET LOCAL work_mem = '8MB'")
                conn.execute("SET LOCAL jit = off")
                with review_cache.foreground_read():
                    page = review_state_page(conn, args, states)
        finally:
            _reading.release()
    except ValueError:
        raise
    except Exception as error:
        if CACHE_SECONDS and hit:
            log_failure(
                log, "Review database read failed; serving cached page",
                error=error, level=logging.WARNING,
            )
            return {**copy.deepcopy(hit[1]), "stale_since": hit[2]}
        raise
    if CACHE_SECONDS:
        _cache.put(key, (now, copy.deepcopy(page), datetime.now(timezone.utc)))
    return page


def warm(app):
    """Use the same builder locally; local startup follows completed live publications."""
    if HOT_CACHE:
        check = None
        if os.environ.get("BTO_WEB_HTTPS") != "1":
            from bto.review_jds.refresh_review_cache import (
                Client,
                RefreshFailed,
                configuration,
            )

            try:
                client = Client(configuration())
                check = client.status
            except RefreshFailed:
                log.warning("Local Review catch-up needs the private BTO_REVIEW_REFRESH settings")
        hot.start(app.config["PG_CONNECT"], check)


def _wants_recent_queue(args):
    return not (
        args.get("all") == "1"
        or args.get("advertiser")
        or args.get("description")
        or args.get("seen") in {"any", "all", "14d", "28d"}
    )


def _hot_page(args, states):
    snapshot = hot.snapshot
    g.review_snapshot = snapshot
    if snapshot is None:
        return {
            "jobs": [],
            "selected": None,
            "total": None,
            "loaded": None,
            "warming": True,
            "warming_since": hot.building_since,
        }
    return review_cache.page(
        current_app.config["PG_CONNECT"],
        args,
        snapshot,
        states,
        cached_details_only=getattr(g, "review_state_unavailable", False),
    )


def live_states(conn=None):
    """Resolve owner state once per request; never keep it between requests."""
    if not hasattr(g, "review_states"):
        hot.check_for_update()
        if review_cache.memory_pressure():
            review_cache.trim_optional()
        with review_cache.foreground_read():
            if conn is not None:
                g.review_states = review_states(conn, allow_missing=True)
            else:
                with current_app.config["PG_CONNECT"]() as connection, connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    g.review_states = review_states(connection, allow_missing=True)
    return g.review_states


def states_for_review():
    """A lost live connection must not hide an already loaded metadata snapshot."""
    from psycopg import OperationalError

    try:
        return live_states()
    except OperationalError as error:
        if not HOT_CACHE or hot.snapshot is None:
            raise
        g.review_states = None
        g.review_state_unavailable = True
        log_failure(
            log, "Review state unavailable; serving saved metadata",
            error=error, level=logging.WARNING,
        )
        return None


def mark_state_unavailable(controls):
    # Without PostgreSQL, read membership is unknown: show metadata without that filter.
    controls["review"]["counts"].update(read="—", unread="—")


def state_json(value):
    return {
        field: value[field].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if value.get(field)
        else None
        for field in REVIEW_TIMESTAMPS
    }


@blueprint.get("/review")
@login_required
def review():
    """Serve Review from its snapshot or a bounded read-only database query."""
    try:
        args = request.args.copy()
        fresh = args.pop("fresh", None) is not None
        states = states_for_review()
        if getattr(g, "review_state_unavailable", False):
            args["show"] = "all"
        state_available = states is not None
        states = states or {}
        page = (
            _hot_page(args, states)
            if HOT_CACHE and (hot.snapshot is not None or _wants_recent_queue(args))
            else None
        )
        if page is None:
            page = _read_page(args, states, fresh=fresh)
        page["review_state_available"] = state_available
        page["your_history"] = your_history(
            states, page.get("selected"), page.get("advertiser_names")
        )
        page["advertiser_you"] = advertiser_you(page.get("advertiser_rows"), states)
        if getattr(g, "review_state_unavailable", False):
            mark_state_unavailable(page["filters"])
            page["state_error"] = (
                "PostgreSQL is temporarily unavailable. Showing saved listing metadata, "
                "including read and unread jobs. Read/star/save actions are disabled."
            )
        page["to_apply_count"] = (
            sum(value["saved_to_apply_at"] is not None for value in states.values())
            if state_available
            else "—"
        )
        snapshot = getattr(g, "review_snapshot", None)
        if HOT_CACHE:
            page["hot"] = {
                "built_at": snapshot.built_at if snapshot else None,
                "building": hot.building_since is not None,
            }
        if (
            snapshot
            and page.get("selected")
            and page.get("snapshot_built_at") == snapshot.built_at
            and not getattr(g, "review_state_unavailable", False)
        ):
            review_cache.prefetch(
                current_app.config["PG_CONNECT"],
                page["selected"]["id"],
                args,
                snapshot,
                page["filters"].get("_prefetch_ids", []),
                context=g.user["username"],
            )
        return render_review(page)
    except ValueError as error:
        return render_review({"jobs": [], "selected": None, "total": 0, "error": str(error)}), 400
    except Exception as error:
        log_failure(log, "Review unavailable", error=error)
        return render_review(
            {
                "jobs": [],
                "selected": None,
                "total": None,
                "error": "Job-listing data is temporarily unavailable.",
            }
        ), 503


@blueprint.get("/review/listing")
@login_required
def listing():
    """Return one listing and its panels without replacing the whole Review page."""
    try:
        args = request.args.copy()
        states = states_for_review()
        if getattr(g, "review_state_unavailable", False):
            args["show"] = "all"
        state_available = states is not None
        states = states or {}
        snapshot = hot.snapshot if HOT_CACHE else None
        controls = review_filter_state(
            args,
            latest_night=snapshot.latest if snapshot else None,
            older_nights=snapshot.older if snapshot else (),
            available_markets=snapshot.available_markets if snapshot else MARKETS,
            board_pairs=snapshot.board_pairs if snapshot else (),
        )
        use_hot = snapshot is not None and review_cache.cacheable(args, controls, snapshot)
        if use_hot:
            identifier = int(args.get("id"))
            selected, context = review_cache.details(
                current_app.config["PG_CONNECT"],
                identifier,
                args,
                snapshot,
                cached_only=getattr(g, "review_state_unavailable", False),
            )
            selected = (
                {
                    **selected,
                    "review_state": display_review_state(selected, resolved_review_states(states)),
                }
                if selected
                else None
            )
            review_cache.queue(args, controls, snapshot, states)
            if getattr(g, "review_state_unavailable", False):
                mark_state_unavailable(controls)
            navigation = controls.get("review")
        else:
            page = _read_page(args, states)
            selected = page["selected"]
            context = {
                key: value
                for key, value in page.items()
                if key
                not in {"selected", "filters", "jobs", "total", "page", "pages", "loaded", "error"}
            }
            controls = page["filters"]
            navigation = controls["review"]
        if not selected:
            abort(404)
        context = {
            **context,
            "selected": selected,
            "your_history": your_history(states, selected, context.get("advertiser_names")),
            "advertiser_you": advertiser_you(context.get("advertiser_rows"), states),
            "latest_night": controls.get("latest_night"),
            "review_navigation": navigation,
            "review_state_available": state_available,
            "board_labels": BOARD_LABELS,
            "bucket_labels": BUCKET_LABELS,
            "page_url": lambda **changes: review_url(None, **changes),
        }
        response = jsonify(
            id=selected["id"],
            review_state_available=state_available,
            review_state=state_json(selected["review_state"]),
            navigation=navigation,
            facets={
                key: controls[key]
                for key in ("market_counts", "bucket_counts", "board_counts", "out_of_scope_count")
            },
            history=render_template("_review_history.html", **context),
            listing=render_template("_listing.html", **context),
            role=render_template("_review_role.html", **context),
        )
        if use_hot and navigation and not getattr(g, "review_state_unavailable", False):
            review_cache.prefetch(
                current_app.config["PG_CONNECT"],
                selected["id"],
                args,
                snapshot,
                controls.get("_prefetch_ids", []),
                context=g.user["username"],
            )
        return response
    except (TypeError, ValueError):
        abort(400)
    except Exception as error:
        if getattr(error, "code", None) in {400, 404}:
            raise
        log_failure(log, "Review listing unavailable", error=error)
        return jsonify(error="The selected listing is temporarily unavailable."), 503


@blueprint.get("/review/cache/status")
@login_required
def cache_status():
    status = hot.status()
    from bto.web.auth import csrf_token

    status["csrf_token"] = csrf_token()
    return jsonify(status)


@blueprint.post("/review/cache/rebuild")
@admin_required
def rebuild_cache():
    """Admin-only: ask the background builder for a fresh snapshot."""
    if not HOT_CACHE:
        abort(404)
    request_id = hot.request_rebuild(request.form.get("request_id"))
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify(request_id=request_id), 202 if request_id else 409
    if request_id:
        flash(
            "Reading a fresh Review snapshot in the background; the current one stays in "
            "use until it is ready."
        )
    else:
        flash("A Review snapshot is already being read.")
    return redirect(url_for("review.review"))


@blueprint.post("/review/action")
@admin_required
def action():
    """Commit an admin's read, star or save change, then request eligible searches."""
    data = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(data, "get"):
        abort(400)
    field = {
        "read": "reviewed_at",
        "star": "starred_at",
        "save": "saved_to_apply_at",
        "save_star": "saved_to_apply_at",
        "queue": "saved_to_apply_at",
    }.get(data.get("action"))
    active = data.get("active")
    if isinstance(active, str):
        active = {"true": True, "false": False}.get(active)
    if not field or not isinstance(active, bool):
        abort(400)
    restore = data.get("restore")
    if restore is not None and (
        data.get("action") != "queue"
        or active
        or not isinstance(restore, dict)
        or set(restore) != {"read", "star"}
        or any(not isinstance(value, bool) for value in restore.values())
    ):
        abort(400)
    try:
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            if data.get("action") == "queue":
                if active:
                    change_review_state(
                        conn, int(data["id"]), "reviewed_at", True, return_states=True
                    )
                    if data.get("star") is True:
                        change_review_state(
                            conn, int(data["id"]), "starred_at", True, return_states=True
                        )
                elif restore:
                    for name, column in (("read", "reviewed_at"), ("star", "starred_at")):
                        change_review_state(
                            conn, int(data["id"]), column, restore[name], return_states=True
                        )
            if data.get("action") == "save_star" and active:
                change_review_state(conn, int(data["id"]), "starred_at", True)
            state, states = change_review_state(
                conn, int(data["id"]), field, active, return_states=True
            )
        g.review_states = states
        # Search failures must not fail the committed state change.
        # Import here because the To Apply routes also use this module.
        try:
            from bto.web.routes.to_apply import start_searches_after_review_change

            start_searches_after_review_change(data.get("action"), active, state)
        except Exception as error:
            log_failure(log, "Automatic searches not started", error=error)
        return jsonify(
            review_state=state_json(state),
            saved_count=sum(value["saved_to_apply_at"] is not None for value in states.values()),
            people_search=people_search_status(data.get("action"), state),
        )
    except (KeyError, TypeError, ValueError):
        abort(400)
    except Exception as error:
        from psycopg.errors import UndefinedTable

        if (
            isinstance(error, UndefinedTable)
            and error.diag.message_primary == 'relation "job_tracking.review_status" does not exist'
        ):
            return jsonify(error="Review state is not set up; changes are unavailable."), 409
        log_failure(log, "Review state write failed", error=error)
        return jsonify(error="Could not save Review state. Try again."), 503


def people_search_status(action, state):
    """Read the latest people-search status after a saved job's star change."""
    if action != "star" or not state.get("saved_to_apply_at"):
        return None
    try:
        from bto.web.routes.to_apply import anchor_key, searches_use_this_database

        key = anchor_key(state)
        if key is None or not searches_use_this_database():
            return None
        with current_app.config["PG_CONNECT"]() as conn:
            search = latest_personnel_search(conn, key)
        return search["status"] if search else None
    except Exception as error:
        log_failure(log, "People search status unavailable", error=error)
        return None


def advertiser_you(rows, states):
    """'in queue' for each of the advertiser's other jobs saved to To Apply, by id."""
    resolved = resolved_review_states(states or {})
    return {
        row["id"]: "in queue"
        for row in rows or []
        if display_review_state(row, resolved).get("saved_to_apply_at")
    }


@blueprint.get("/review/diff")
@login_required
def diff():
    try:
        first, second = int(request.args["first"]), int(request.args["second"])
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            result = wording_diff(conn, first, second)
        return jsonify(html=result)
    except (KeyError, TypeError, ValueError):
        abort(400)


# ── Your history ──────────────────────────────────────────────────────────────
# Your history reads current saves, separately from cached listing context.
# Re-read the saved listings only when the set of saves changes.
_saved_jobs = {"key": None, "jobs": []}
_saved_jobs_lock = threading.Lock()


def your_history(states, selected, names):
    """Show the selected advertiser's saved jobs, or no history when it cannot be read."""
    if not selected or not states:
        return []
    names = set(names or ([selected["advertiser_name"]] if selected.get("advertiser_name") else []))
    if not names:
        return []
    try:
        group = review_group_key(selected)
        rows = [
            {
                "title": job.get("title"),
                "this_job": review_group_key(job) == group,
                "stage": "In queue",
                "tone": "queued",
                "meta": "Not applied yet · queued "
                + ago_words(job["review_state"]["saved_to_apply_at"]),
                "href": url_for("to_apply.to_apply", id=job["id"]),
            }
            for job in saved_jobs(states)
            if job.get("advertiser_name") in names
        ]
    except Exception as error:
        log_failure(log, "Your history unavailable", error=error)
        return []
    return rows


def saved_jobs(states):
    """The jobs saved to To Apply, one per Review group, in the To Apply list's order."""
    from bto.web.routes.to_apply import saved_jobs_in_list_order  # it imports this module

    key = tuple(
        sorted(
            (str(group), str(state["saved_to_apply_at"]))
            for group, state in states.items()
            if state.get("saved_to_apply_at")
        )
    )
    with _saved_jobs_lock:
        if _saved_jobs["key"] == key:
            return _saved_jobs["jobs"]
    with current_app.config["PG_CONNECT"]() as conn:
        jobs = saved_jobs_in_list_order(saved_review_candidates(conn, states), states)
    with _saved_jobs_lock:
        _saved_jobs.update(key=key, jobs=jobs)
    return jobs


def ago_words(value):
    """How long ago `value` was, in Singapore calendar days, as age_words words it."""
    return age_words((datetime.now(SINGAPORE).date() - value.astimezone(SINGAPORE).date()).days)


def age_words(days):
    """Describe elapsed days using days, weeks or months."""
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{round(days / 7)} weeks ago"
    return f"{round(days / 30)} months ago"
