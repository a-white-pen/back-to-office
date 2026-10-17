"""Refresh the existing authenticated Review snapshot and await its completion."""

import html
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class RefreshFailed(RuntimeError):
    """The serving snapshot could not be confirmed fresh within bounded recovery."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


def configuration():
    import os

    from bto.settings import _dotenv

    private = _dotenv()
    values = {
        key: os.environ.get("BTO_REVIEW_REFRESH_" + key.upper())
        or private.get("BTO_REVIEW_REFRESH_" + key.upper())
        for key in ("url", "username", "password")
    }
    if not all(values.values()):
        raise RefreshFailed("Review refresh URL and private admin credentials are required")
    parsed = urllib.parse.urlsplit(values["url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RefreshFailed("Review refresh must use an HTTPS origin without URL credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RefreshFailed("Review refresh URL must name the serving origin")
    values["url"] = values["url"].rstrip("/")
    return values


def snapshot(markup, now):
    """The existing Review status exposes a Singapore minute and build-in-progress flag."""
    block = re.search(r'<div class="review-status">(.*?)</div>', markup, re.DOTALL)
    # Nested filter summaries end before the snapshot; use the status through its form.
    end = markup.find('<form id="review-rebuild"')
    start = markup.find('<div class="review-status">')
    text = html.unescape(re.sub(r"<[^>]*>", " ", markup[start:end] if end > start >= 0 else block[1] if block else ""))
    match = re.search(r"Snapshot\s+(\d{1,2}\s+[A-Z][a-z]{2})\s*·\s*(\d{1,2}:\d{2}\s*[AP]M)\s+SGT", text)
    if not match:
        raise RefreshFailed("Review did not expose a usable snapshot timestamp")
    stamp = datetime.strptime(match[1] + " " + str(now.year) + " " + match[2], "%d %b %Y %I:%M %p").replace(tzinfo=now.tzinfo)
    if stamp > now + timedelta(days=1):
        stamp = stamp.replace(year=stamp.year - 1)
    return stamp, "refreshing" in text.casefold()


# The serving builder permits 900 seconds. Allow an existing build, the
# requested fresh build, the minute boundary and bounded read recovery.
REFRESH_TIMEOUT_SECONDS = 2100


def refresh(config=None, *, timeout=REFRESH_TIMEOUT_SECONDS, wait=time.sleep, clock=None, monotonic=time.monotonic, opener=None):
    """Use normal login/CSRF and the existing admin POST; never starts model work."""
    config = config or configuration()
    clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Singapore")))
    opener = opener or urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect()
    )
    deadline = monotonic() + timeout
    failures = []

    def request(path, fields=None):
        method = "POST" if fields is not None else "GET"
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        req = urllib.request.Request(config["url"] + path, data=data, method=method)
        for attempt in range(3):
            try:
                with opener.open(req, timeout=30) as reply:
                    return reply.status, reply.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                if error.code == 302:
                    return 302, error.headers.get("Location", "")
                if method == "POST" or error.code not in {408, 429, 500, 502, 503, 504}:
                    raise RefreshFailed(f"Review {method} was refused (HTTP {error.code})") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == "POST":
                    raise RefreshFailed("Review POST receipt unavailable; inspect snapshot before retrying") from None
            failures.append({"method": method, "attempt": attempt + 1})
            if attempt < 2:
                wait(2 ** attempt)
        raise RefreshFailed("Review read failed after three bounded attempts")

    _, page = request("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if not token:
        raise RefreshFailed("Review login did not expose its CSRF token")
    status, location = request("/login", {"username": config["username"], "password": config["password"], "csrf_token": html.unescape(token[1])})
    if status != 302 or location != "/review":
        raise RefreshFailed("Review admin login failed")
    _, page = request("/review?view=table")
    if '<form id="review-rebuild"' not in page:
        raise RefreshFailed("Review account cannot request a snapshot rebuild")
    before, building = snapshot(page, clock())
    while building:
        if monotonic() >= deadline:
            raise RefreshFailed("Existing Review build did not finish before refresh deadline")
        wait(5)
        _, page = request("/review?view=table")
        _, building = snapshot(page, clock())
    # The existing UI timestamp has minute precision. Cross a minute before
    # requesting so an old, same-minute snapshot cannot acknowledge this run.
    requested_minute = clock().replace(second=0, microsecond=0) + timedelta(minutes=1)
    wait(max(0, (requested_minute - clock()).total_seconds()))
    token = re.search(r'<form id="review-rebuild".*?name="csrf_token" value="([^"]+)"', page, re.DOTALL)
    if not token:
        raise RefreshFailed("Review rebuild CSRF token unavailable")
    receipt_uncertain = False
    try:
        status, _ = request("/review/cache/rebuild", {"csrf_token": html.unescape(token[1])})
        if status != 302:
            raise RefreshFailed("Review rebuild did not acknowledge the admin request")
    except RefreshFailed as error:
        if "receipt unavailable" not in str(error):
            raise
        # The builder may have accepted the POST. Confirm by reads, never by
        # blindly sending another rebuild request after a lost receipt.
        receipt_uncertain = True
    while monotonic() < deadline:
        wait(5)
        _, page = request("/review?view=table")
        after, building = snapshot(page, clock())
        if after >= requested_minute and not building:
            return {"status": "complete", "previous_snapshot": before.isoformat(), "requested_after": requested_minute.isoformat(), "confirmed_snapshot": after.isoformat(), "confirmed_at": clock().isoformat(), "read_recovery": failures, "post_receipt_uncertain": receipt_uncertain}
    raise RefreshFailed("Review cache did not confirm a completed fresh snapshot within the bounded serving-cache recovery window")


def main():
    import json

    print(json.dumps(refresh()))


if __name__ == "__main__":
    main()
