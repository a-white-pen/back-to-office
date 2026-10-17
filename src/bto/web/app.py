"""Create the shared Flask application; run development HTTP on loopback only."""

import argparse
import logging
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, redirect, request, url_for
from werkzeug.security import generate_password_hash

from bto.filter_and_bucket_job_listings import SINGAPORE, night
from bto.filter_and_bucket_job_listings.config import read_private_data
from bto.settings import load_postgres
from bto.storage.postgres.connection import connect

from . import auth
from .routes import applied, review, taxonomy, to_apply


def create_app(test_config=None):
    app = Flask(__name__)
    if test_config and "BTO_USERS" in test_config:
        settings = test_config
    else:
        private = read_private_data(Path(__file__).with_name("auth_config.py"))
        settings = {"SECRET_KEY": private.get("SECRET_KEY"), "BTO_USERS": private.get("USERS")}
        load_postgres()
    users = settings.get("BTO_USERS")
    if not settings.get("SECRET_KEY") or not isinstance(users, dict) or not users:
        raise ValueError("Private web authentication is unconfigured")
    if any(
        a.get("role") not in {"admin", "viewer"} or not a.get("password_hash")
        for a in users.values()
    ):
        raise ValueError("Private web accounts are invalid")
    app.config.update(
        SECRET_KEY=settings["SECRET_KEY"],
        BTO_USERS=users,
        BTO_DUMMY_HASH=generate_password_hash("unusable-example-password"),
        PG_CONNECT=connect,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=64_000,
    )
    if test_config:
        app.config.update(test_config)
    app.register_blueprint(auth.blueprint)
    for module in (review, to_apply, applied, taxonomy):
        app.register_blueprint(module.blueprint)

    @app.context_processor
    def shared_context():
        def page_url(**changes):
            values = request.args.to_dict()
            for key, value in changes.items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
            return url_for("review.review", **values)

        return {"user": g.user, "csrf_token": auth.csrf_token, "page_url": page_url}

    @app.template_filter("sgt")
    def singapore_time(value):
        return value.astimezone(SINGAPORE).strftime("%-d %b · %-I:%M %p SGT") if value else "—"

    @app.template_filter("salary_label")
    def salary_label(row, missing="Not supplied"):
        if row.get("salary_raw"):
            return row["salary_raw"]
        lower, upper = row.get("salary_min"), row.get("salary_max")
        if lower is None and upper is None:
            return missing
        amount = lambda value: f"{value:,.2f}".rstrip("0").rstrip(".")
        band = (
            amount(lower) + (" – " + amount(upper) if upper is not None and upper != lower else "")
            if lower is not None else "Up to " + amount(upper)
        )
        qualifier = "From" if upper is None else "Up to" if lower is None else ""
        band = band.removeprefix("Up to ")
        return " ".join(value for value in [
            qualifier, row.get("salary_currency") or "Currency not supplied", band,
            row.get("salary_period") or "",
        ] if value)

    @app.template_filter("collection_night")
    def collection_night(value):
        return night(value).strftime("%-d %b") if value else "—"

    @app.get("/")
    def front_door():
        return redirect(url_for("review.review" if g.user else "auth.login"))

    @app.after_request
    def protect_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-src 'none'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    create_app().run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
