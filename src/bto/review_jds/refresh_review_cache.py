"""Confirm an authenticated, persisted Review publication without model work."""

import html
import http.cookiejar
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from bto.log_errors import log_failure
from bto.web.publication_host import configuration_file as host_configuration_file
from bto.web.publication_host import load as host_configuration

REFRESH_TIMEOUT_SECONDS = 3600
log = logging.getLogger(__name__)


class RefreshFailed(RuntimeError):
    """The requested Review publication was not confirmed."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


def configuration():
    """Require an HTTPS origin and private login details for Review publication."""
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
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RefreshFailed("Review refresh must name an HTTPS origin without URL credentials")
    values["url"] = values["url"].rstrip("/")
    return values


class Client:
    """One normal login session; status reads do not load state or queue data."""

    def __init__(self, config, opener=None):
        self.config = config
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect()
        )
        self.logged_in = False

    def request(self, path, fields=None, *, publication=False):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        headers = {"Accept": "application/json"} if publication else {}
        req = urllib.request.Request(
            self.config["url"] + path,
            data=data,
            headers=headers,
            method="POST" if fields is not None else "GET",
        )
        try:
            with self.opener.open(req, timeout=30) as reply:
                return reply.status, reply.read().decode()
        except urllib.error.HTTPError as error:
            if error.code == 302:
                return 302, error.headers.get("Location", "")
            raise RefreshFailed(f"Review request refused (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RefreshFailed("Review request receipt unavailable") from None

    def login(self):
        _, page = self.request("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)
        if not token:
            raise RefreshFailed("Review login has no CSRF token")
        status, location = self.request(
            "/login",
            {
                "username": self.config["username"],
                "password": self.config["password"],
                "csrf_token": html.unescape(token[1]),
            },
        )
        if status != 302 or location != "/review":
            raise RefreshFailed("Review login failed")
        self.logged_in = True

    def status(self):
        if not self.logged_in:
            self.login()
        status, body = self.request("/review/cache/status")
        if status == 302:
            self.login()
            status, body = self.request("/review/cache/status")
        try:
            result = json.loads(body)
            if status != 200 or "completed_request" not in result:
                raise ValueError
            return result
        except (TypeError, ValueError):
            raise RefreshFailed("Review did not expose publication status") from None


def publish_on_host(request_id, *, recover=False):
    """Run the privately configured serving-host helper as a synchronous nightly child."""
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise RefreshFailed("Invalid Review publication identifier")
    try:
        host = host_configuration(privileged=True)
    except ValueError as error:
        raise RefreshFailed("Private Review publication host settings are unavailable") from error
    helper = [
        "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        "STATE_DIRECTORY=" + host["state_directory"], "HOME=" + host["state_directory"],
        "BTO_REVIEW_PUBLICATION_CONFIG=" + str(host_configuration_file()),
        "PYTHONDONTWRITEBYTECODE=1", host["python"],
        "-m", "bto.web.build_review_snapshot",
        "--recover" if recover else "--publish", "--request-id", request_id,
    ]
    command = ["/usr/bin/sudo", "-n"]
    if not recover:
        # A scope limits this child and its builder; it installs no service or trigger.
        command += [
            "/usr/bin/systemd-run", "--scope", "--quiet", "--collect",
            "--unit=bto-review-publication-" + request_id,
            "--property=MemoryHigh=256M", "--property=MemoryMax=320M",
            "--property=MemorySwapMax=64M", "--property=RuntimeMaxSec=55min",
            "--property=TimeoutStopSec=120", "--property=OOMPolicy=stop",
        ]
    log.info("Review publication helper started request_id=%s recovery=%s", request_id, recover)
    try:
        result = subprocess.run(command + helper, cwd=host["working_directory"], check=False,
                                timeout=120 if recover else 3430)
    except (OSError, subprocess.SubprocessError) as error:
        raise RefreshFailed("Review publication helper could not finish; see the nightly journal") from error
    log.info("Review publication helper finished request_id=%s recovery=%s exit_code=%s",
             request_id, recover, result.returncode)
    if result.returncode not in (0, 75):
        raise RefreshFailed("Review publication helper failed; see the nightly journal")


def refresh(
    config=None,
    *,
    timeout=REFRESH_TIMEOUT_SECONDS,
    wait=time.sleep,
    monotonic=time.monotonic,
    opener=None,
):
    """Request once, then confirm that exact build persisted and became serving."""
    client = Client(config or configuration(), opener)
    started = monotonic()
    deadline = started + timeout
    before = client.status()
    external = "publication_phase" in before
    if external and before["building"]:
        # Recover an interrupted child only when its ownership lock is free.
        publish_on_host(before["request_id"], recover=True)
        before = client.status()
        if before.get("publication_phase") == "pending":
            publish_on_host(before["request_id"])
            before = client.status()
    if before["building"]:
        log.info("Review publication waiting for an existing build timeout_seconds=%s", timeout)
    while before["building"]:
        if monotonic() >= deadline:
            raise RefreshFailed("Existing Review build did not finish before publication deadline")
        wait(5)
        before = client.status()
    request_id = uuid4().hex
    log.info("Review publication requested request_id=%s previous_snapshot=%s timeout_seconds=%s",
             request_id, before["built_at"], timeout)
    try:
        code, body = client.request(
            "/review/cache/rebuild",
            {"csrf_token": before["csrf_token"], "request_id": request_id},
            publication=True,
        )
        if code != 202 or json.loads(body).get("request_id") != request_id:
            raise RefreshFailed("Review publication request was not accepted")
    except RefreshFailed as error:
        if str(error) != "Review request receipt unavailable":
            raise
        log.warning("Review publication receipt missing request_id=%s; checking status without resubmission",
                    request_id)
        # Never blindly resend a publication after losing its acknowledgement.
    if external:
        publish_on_host(request_id)
    status_unavailable = False
    while monotonic() < deadline:
        wait(5)
        try:
            status = client.status()
        except RefreshFailed as error:
            if not status_unavailable:
                log_failure(log, "Review publication status unavailable request_id=%s; retrying",
                            request_id, error=error, level=logging.WARNING)
                status_unavailable = True
            continue
        if status_unavailable:
            log.info("Review publication status recovered request_id=%s", request_id)
            status_unavailable = False
        if status["failed_request"] == request_id:
            log.error("Review publication failed request_id=%s seconds=%.1f",
                      request_id, monotonic() - started)
            raise RefreshFailed(
                "Review publication failed: " + (status.get("error") or "unknown error")
            )
        if status["completed_request"] == request_id and not status["building"]:
            log.info("Review publication confirmed request_id=%s snapshot=%s seconds=%.1f",
                     request_id, status["built_at"], monotonic() - started)
            return {
                "status": "complete",
                "request_id": request_id,
                "previous_snapshot": before["built_at"],
                "confirmed_snapshot": status["built_at"],
                "persisted": True,
            }
    log.error("Review publication deadline reached request_id=%s timeout_seconds=%s",
              request_id, timeout)
    raise RefreshFailed("Review publication was not confirmed before its deadline")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        print(json.dumps(refresh()))
    except Exception as error:
        log_failure(log, "Review publication command failed", error=error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
