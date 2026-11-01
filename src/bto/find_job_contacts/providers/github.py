"""Read public GitHub profiles, searches and organisation membership.

Requests use no token. A refusal raises GitHubUnavailable so the caller can
skip GitHub for the remaining people while other lookup steps continue."""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ...log_errors import log_failure

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT_S = 20
HEADERS = {
    "Accept": "application/vnd.github+json",
    # GitHub rejects requests without a User-Agent.
    "User-Agent": "back-to-office",
}


class GitHubUnavailable(RuntimeError):
    """GitHub refused the request or could not be reached."""


def search_users_by_name(name, *, limit=3):
    """Logins of the accounts whose profile name matches `name`, best first."""
    query = urllib.parse.urlencode({"q": f'"{name}" in:name', "per_page": limit})
    answer = _get(f"/search/users?{query}") or {}
    return [item["login"] for item in answer.get("items") or [] if item.get("login")]


def user_profile(login):
    """The public profile: name, company, bio, blog and html_url."""
    return _get(f"/users/{urllib.parse.quote(login)}") or {}


def is_public_member(organisation, login):
    """True when `login` is a public member of the GitHub organisation."""
    path = (f"/orgs/{urllib.parse.quote(organisation)}"
            f"/public_members/{urllib.parse.quote(login)}")
    # GitHub answers 204 for a member and 404 for anyone else.
    return _get(path, member_check=True)


def _get(path, *, member_check=False):
    request = urllib.request.Request(API + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            if member_check:
                return response.status == 204
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False if member_check else None
        raise GitHubUnavailable(f"GitHub answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        log_failure(log, "GitHub public API read failed", error=error, level=logging.WARNING)
        raise GitHubUnavailable(f"GitHub could not be read: {error}") from None
