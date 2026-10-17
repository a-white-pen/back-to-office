"""Present the Taxonomy shell while its definitions await replacement."""

from flask import Blueprint, render_template

from bto.web.auth import login_required

blueprint = Blueprint("taxonomy", __name__)


@blueprint.get("/taxonomy")
@login_required
def taxonomy():
    return render_template("taxonomy.html")
