"""Serve the workflow guide and redirect its alternate address."""

from flask import Blueprint, redirect, render_template, url_for

from bto.web.auth import login_required

blueprint = Blueprint("how_it_works", __name__)


@blueprint.get("/how-it-works")
@login_required
def how_it_works():
    return render_template("how_it_works.html")


@blueprint.get("/taxonomy")
@login_required
def taxonomy():
    """Redirect the alternate guide URL to How it works."""
    return redirect(url_for("how_it_works.how_it_works"), 301)
