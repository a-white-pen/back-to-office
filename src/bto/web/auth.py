"""Private-account authentication and server-side permissions."""

import secrets
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

blueprint = Blueprint("auth", __name__)


def login_required(view):
    @wraps(view)
    def guarded(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return guarded


def admin_required(view):
    @wraps(view)
    @login_required
    def guarded(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return guarded


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@blueprint.before_app_request
def authenticate_request():
    username = session.get("username")
    account = current_app.config["BTO_USERS"].get(username)
    g.user = {"username": username, "role": account["role"]} if account else None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # Viewer business actions fail even when posted directly rather than through the UI.
        if (
            g.user
            and g.user["role"] != "admin"
            and request.endpoint not in {"auth.login", "auth.logout"}
        ):
            abort(403)
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
            abort(400)


@blueprint.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        account = current_app.config["BTO_USERS"].get(username)
        # The dummy hash keeps the unknown-user path from having a trivial timing difference.
        password_hash = (
            account["password_hash"] if account else current_app.config["BTO_DUMMY_HASH"]
        )
        if check_password_hash(password_hash, request.form.get("password", "")) and account:
            session.clear()
            session["username"] = username
            csrf_token()
            return redirect(url_for("review.review"))
        error = "That username or password isn't right. Try again."
    return render_template("login.html", error=error)


@blueprint.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
