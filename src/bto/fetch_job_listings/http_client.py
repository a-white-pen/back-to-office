"""Shared direct-HTTP plumbing for the boards we call ourselves: MCF and SEEK.

Called by:
    fetch_job_listings.mcf.fetch, fetch_job_listings.seek.fetch

One function, `call()`, wraps urllib with the behaviour both boards proved in
production: a polite pause after every successful request, and exponential
backoff on transient failures (429/5xx and network errors). Anything else —
URLs, bodies, response validation — belongs to the board's own fetch module.

Indeed and LinkedIn go through apify_client.py instead, never through here.
"""

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 45
TRANSIENT_HTTP = (429, 500, 502, 503, 504)


def call(url, body=None, *, headers, pause=0.0, attempts=4, timeout=DEFAULT_TIMEOUT_S):
    """One HTTP call. POST when `body` is given (JSON-encoded), else GET.

    Returns the raw response bytes. Retries transient HTTP statuses and
    network errors with exponential backoff; the final failure propagates as
    urllib.error.HTTPError / URLError for the caller's per-job accounting.
    `pause` sleeps after a successful response so a sweep stays polite.
    """
    data = json.dumps(body).encode() if body is not None else None
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            if pause:
                time.sleep(pause)
            return payload
        except urllib.error.HTTPError as error:
            if error.code in TRANSIENT_HTTP and attempt < attempts:
                log.warning("HTTP %s from %s; retry %d/%d in %.0fs",
                            error.code, url.split("?")[0], attempt, attempts - 1, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except urllib.error.URLError as error:
            if attempt < attempts:
                log.warning("network error on %s (%s); retry %d/%d in %.0fs",
                            url.split("?")[0], error.reason, attempt, attempts - 1, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
