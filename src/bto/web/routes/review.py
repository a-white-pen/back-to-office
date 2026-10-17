"""Thin Review HTTP routes; queries and presentation decisions live in the domain."""

import logging

from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for

from bto.filter_and_bucket_job_listings import BUCKET_LABELS
from bto.review_jds.review_job_listings import (
    BOARD_LABELS,
    MARKETS,
    review_filter_state,
    review_page,
    review_selection,
    wording_diff,
)
from bto.web.auth import admin_required, login_required

blueprint = Blueprint("review", __name__)
log = logging.getLogger(__name__)


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
    controls = page.get("filters")
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
        {"market": market, "boards": [
            {"key": market + ":" + board, "label": label,
             "selected": market + ":" + board not in controls.get("boards_off", []),
             "count": controls.get("board_counts", {}).get(market + ":" + board, 0)}
            for board, label in BOARD_LABELS.items() if market + ":" + board in pairs
        ]} for market in controls["markets"]
    ]
    historical_markets = [m for m in controls.get("available_markets", []) if m not in MARKETS]
    oos = ("title_filter", "non_tech", "tech_not_for_me")
    selected = controls["buckets"]
    selected = (
        [b for b in selected if b not in oos]
        if any(b in selected for b in oos)
        else [*selected, *oos]
    )
    return render_template(
        "review.html",
        **{**page, "filters": controls},
        bucket_labels=BUCKET_LABELS,
        page_url=page_url,
        toggle_filter=toggle_filter,
        toggle_board=toggle_board,
        board_groups=board_groups,
        board_off_count=sum(pair.split(":")[0] in controls["markets"] for pair in controls.get("boards_off", [])),
        board_labels=BOARD_LABELS,
        historical_markets=historical_markets,
        show_historical_markets=controls["window"] == "any" or any(
            m in controls["markets"] for m in historical_markets
        ),
        out_of_scope_url=page_url(buckets=",".join(selected) or "none", page=1, id=None),
    )


@blueprint.get("/review")
@login_required
def review():
    try:
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # Broad history reads retain bounded headroom beyond warm-cache timings.
            conn.execute("SET LOCAL statement_timeout = '90s'")
            conn.execute("SET LOCAL work_mem = '32MB'")
            conn.execute("SET LOCAL jit = off")
            page = review_page(conn, request.args)
        return render_review(page)
    except ValueError as error:
        return render_review({"jobs": [], "selected": None, "total": 0, "error": str(error)}), 400
    except Exception as error:
        log.error("Review unavailable error_type=%s", type(error).__name__)
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
    try:
        with current_app.config["PG_CONNECT"]() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '10s'")
            context = review_selection(conn, request.args.get("id"), request.args)
        if not context["selected"]:
            abort(404)
        context.update(
            board_labels=BOARD_LABELS, bucket_labels=BUCKET_LABELS,
            page_url=lambda **changes: review_url(None, **changes),
        )
        return jsonify(
            id=context["selected"]["id"],
            history=render_template("_review_history.html", **context),
            listing=render_template("_listing.html", **context),
            role=render_template("_review_role.html", **context),
        )
    except ValueError:
        abort(400)
    except Exception as error:
        if getattr(error, "code", None) in {404, 400}:
            raise
        log.error("Review listing unavailable error_type=%s", type(error).__name__)
        return jsonify(error="The selected listing is temporarily unavailable."), 503


@blueprint.post("/review/action")
@admin_required
def action():
    abort(501, description="Review workflow actions are not connected yet.")


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


@blueprint.get("/review/progress")
@login_required
def progress():
    # Let already-open pages retire their old polling loop without a history scan.
    return jsonify(refresh="manual")
