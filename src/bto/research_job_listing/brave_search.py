"""Read Brave search results, cache them for 30 days and record paid requests.

Research and contact finding use separate keys and cost tables. Missing keys
skip the web search; refusals let other search steps continue. Search results
are candidates that callers must verify. Cost-write failures are logged and
do not fail the search."""

import gzip
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import settings
from ..log_errors import log_failure
from ..storage.postgres import write as pg_write

log = logging.getLogger(__name__)

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
PRICE_PER_REQUEST_USD = 0.005
CACHE_DAYS = 30
MIN_INTERVAL_S = 1.1                  # the plan allows about one request a second per key
REQUEST_TIMEOUT_S = 30

# One key per use, and each key's spend goes to its own cost table.
COST_TABLES = {
    "BRAVE_FIND_DIRECT_LINK_API_KEY": "brave_find_direct_link",
    "BRAVE_FIND_EMPLOYER_API_KEY": "brave_find_employer",
    "BRAVE_FIND_PEOPLE_API_KEY": "brave_find_people",
    "BRAVE_LOOK_UP_PERSON_API_KEY": "brave_look_up_person",
}

_key_locks_guard = threading.Lock()
_key_locks = {}                       # key name → lock that paces that key's requests
_last_request_at = {}                 # key name → time its last request ended


def _configured_value(name):
    """A setting from the environment, or from the repository's .env file on a laptop."""
    return os.environ.get(name) or settings._dotenv().get(name)


def is_configured(key_name):
    """Whether this use's Brave key is set, so its search steps can run."""
    return bool(_configured_value(key_name))


def _cache_path(parameters):
    folder = Path(_configured_value("BTO_SEARCH_CACHE")
                  or (Path.home() / ".cache" / "bto" / "search"))
    digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:24]
    return folder / f"{digest}.json"


def _cached_answer(path):
    """The cached Brave answer for these parameters, or None when there is none
    or it is older than CACHE_DAYS."""
    try:
        if time.time() - path.stat().st_mtime > CACHE_DAYS * 86400:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _save_answer(path, answer):
    """Write the cache file whole or not at all, so a reader never sees half of it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(answer))
        temporary.replace(path)
    except OSError as error:
        log_failure(log, "Brave answer not cached cache_file=%s", path.name,
                    error=error, level=logging.WARNING)


def _key_lock(key_name):
    with _key_locks_guard:
        return _key_locks.setdefault(key_name, threading.Lock())


def _record_cost(key_name):
    """One cost row for a paid request. Fail soft: a lost row is logged, never raised."""
    table = COST_TABLES[key_name]
    try:
        pg_write.record_brave_cost(table, PRICE_PER_REQUEST_USD)
    except Exception as error:
        log_failure(log, "Brave cost row not written table=%s cost_usd=%s",
                    table, PRICE_PER_REQUEST_USD, error=error, level=logging.WARNING)


def search(query, *, key_name, count=20, refusals=None):
    """One web search on the named key. Returns a list of {url, title, description}.

    A refusal returns an empty list rather than raising: a search that does
    not answer is a step that found nothing, not a broken run. When the caller
    passes a `refusals` list, the refusal's description is appended to it, so
    the caller can tell "nothing on the web" from "Brave would not look".
    """
    if key_name not in COST_TABLES:
        raise ValueError(f"unknown Brave key name {key_name!r}; "
                         f"expected one of {sorted(COST_TABLES)}")
    parameters = {"q": query, "count": count, "result_filter": "web"}
    path = _cache_path(parameters)
    cached = _cached_answer(path)
    if cached is not None:
        return _web_results(cached)
    key = _configured_value(key_name)
    if not key:
        return []
    with _key_lock(key_name):
        wait = MIN_INTERVAL_S - (time.time() - _last_request_at.get(key_name, 0.0))
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(
            ENDPOINT + "?" + urllib.parse.urlencode(parameters),
            headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                     "X-Subscription-Token": key, "User-Agent": "bto/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            answer = json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            _note_refusal(_describe_refusal(error), key_name, refusals, error=error)
            return []
        except Exception as error:
            _note_refusal(f"{type(error).__name__}: {str(error)[:120]}",
                          key_name, refusals, error=error)
            return []
        finally:
            _last_request_at[key_name] = time.time()
    # Brave answered, so the request is paid for whether or not it found anything.
    _record_cost(key_name)
    _save_answer(path, answer)
    return _web_results(answer)


def _note_refusal(description, key_name, refusals, *, error):
    # Keep provider detail in the private result, not in operational logs.
    log_failure(log, "Brave search refused key_name=%s http_status=%s",
                key_name, getattr(error, "code", None), error=error, level=logging.WARNING)
    if refusals is not None:
        refusals.append(description)


def _describe_refusal(error):
    """HTTP status and reason, plus Brave's own error code and detail when its body carries them."""
    detail = ""
    try:
        raw = error.read()
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: S110 — an uncompressed body is read as it is
            pass
        body = json.loads(raw.decode("utf-8", "replace"))
        problem = body.get("error") or {}
        detail = " ".join(part for part in (problem.get("code"), problem.get("detail")) if part)
    except Exception:  # noqa: S110 — the status line alone still says enough
        pass
    return f"HTTP {error.code} {error.reason}" + (f" · {detail}" if detail else "")


def _web_results(answer):
    """The answer's web results as {url, title, description}, each description
    cut to 300 characters."""
    return [{"url": r.get("url", ""), "title": r.get("title", ""),
             "description": (r.get("description") or "")[:300]}
            for r in ((answer.get("web") or {}).get("results") or [])]
