"""Applied presentation; tracking data is connected by its owning workflow later."""

from flask import Blueprint, abort, render_template

from bto.web.auth import admin_required, login_required

blueprint = Blueprint("applied", __name__)


@blueprint.get("/applied")
@login_required
def applied():
    return render_template("applied.html")


@blueprint.post("/applied/action")
@admin_required
def action():
    abort(501, description="Application data is not connected.")
