"""To apply presentation; downstream application workflows remain disconnected."""

from flask import Blueprint, abort, render_template

from bto.web.auth import admin_required, login_required

blueprint = Blueprint("to_apply", __name__)


@blueprint.get("/to-apply")
@login_required
def to_apply():
    return render_template("to_apply.html")


@blueprint.post("/to-apply/action")
@admin_required
def action():
    abort(501, description="Application workflow is not connected.")
