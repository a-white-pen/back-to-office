"""Read public HTML and JSON, and extract the text and job details research checks.

A Page retains redirects, visible text, structured JobPosting data and read
errors. Bot walls are reported rather than bypassed. Ownership and vacancy
matching belong to check_employer_board and prove_same_job.
"""

import gzip
import html
import json
import logging
import re
import typing
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

from ..log_errors import log_failure

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_BYTES = 1_500_000
MAX_JSON_BYTES = 40_000_000          # a vendor's whole job list can be several MB

# Phrases that mean the site answered with a bot wall instead of the page.
BOT_WALL = ("just a moment", "verify you are human", "verifying you are human",
            "enable javascript and cookies", "captcha", "access denied",
            "access to this page has been denied", "checking your browser",
            "request unsuccessful. incapsula", "pardon our interruption",
            "attention required! | cloudflare", "are you a robot",
            "please enable cookies", "bot detection", "ddos protection")

# Query parameters a tracking redirect hides its destination in.
TRACKING_PARAMETERS = ("rx_url", "url", "redirect", "target", "dest", "u", "link")


@dataclass
class Page:
    """One HTTP read, including parsed facts and any access or parsing failure."""

    url: str
    mechanism: str                      # how the text was read: "direct" for a plain fetch
    status: int = 0
    final_url: str = ""
    html: str = ""
    text: str = ""
    title: str = ""
    canonical: str = ""
    jsonld_jobs: list = field(default_factory=list)   # schema.org JobPosting objects
    blocked: bool = False
    error: str = ""

    @property
    def ok(self):
        return self.status == 200 and not self.blocked and not self.error and bool(self.text)


class _VisibleText(HTMLParser):
    """Visible text only: drops script, style, noscript, template and svg, keeps order."""
    SKIP: typing.ClassVar = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.canonical = [], "", ""
        self._skip, self._in_title = 0, False
        self.jsonld = []
        self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        if tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld = True
        if tag == "title":
            self._in_title = True
        if tag == "link" and (attributes.get("rel") or "").lower() == "canonical":
            self.canonical = attributes.get("href") or ""
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        if tag == "script":
            self._in_jsonld = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def _job_postings(blocks):
    """Every schema.org JobPosting inside the page's JSON-LD blocks."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in [str(k) for k in kinds]:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for block in blocks:
        try:
            walk(json.loads(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
    return found


def parse_html(raw):
    """(visible text, title, canonical link, JobPosting blocks) of one HTML page."""
    parser = _VisibleText()
    try:
        parser.feed(raw)
    except Exception:  # noqa: S110 — broken markup keeps what parsed before it
        pass
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, parser.title.strip(), parser.canonical, _job_postings(parser.jsonld)


def is_bot_wall(text, title=""):
    head = (title + "\n" + text[:3000]).lower()
    return any(phrase in head for phrase in BOT_WALL)


def _read_body(response):
    """The response body as text, decompressed however it arrived."""
    try:
        raw = response.read(MAX_BYTES)
    except Exception as error:
        log_failure(log, "Public page body read failed", error=error, level=logging.WARNING)
        return ""
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: S110 — a body that will not decompress is read as it came
            pass
    return raw.decode("utf-8", errors="replace")


def fetch(url, timeout=20, headers=None):
    """One GET of a web page. Always returns a Page; a failure sets its error."""
    page = Page(url=url, mechanism="direct")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page.status, page.final_url = response.status, response.geturl()
            page.html = _read_body(response)
    except urllib.error.HTTPError as error:
        # An error response is compressed the same way a good one is, and is
        # worth reading: a 404 page still says whether it was a bot wall.
        page.status, page.error = error.code, f"HTTP {error.code}"
        page.html = _read_body(error)
    except Exception as error:
        log_failure(log, "Public page read failed", error=error, level=logging.WARNING)
        page.error = f"{error.__class__.__name__}: {str(error)[:120]}"
    if page.html:
        page.text, page.title, page.canonical, page.jsonld_jobs = parse_html(page.html)
        page.blocked = is_bot_wall(page.text, page.title)
        if page.blocked and page.status == 200:
            page.error = "bot wall"
    return page


def fetch_json(url, timeout=20, method="GET", body=None, headers=None):
    """One call to a vendor's public JSON API. Returns (status, object or None, error)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_JSON_BYTES)
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return response.status, json.loads(raw.decode("utf-8", errors="replace")), ""
    except urllib.error.HTTPError as error:
        return error.code, None, f"HTTP {error.code}"
    except json.JSONDecodeError:
        return 200, None, "not JSON"
    except Exception as error:
        log_failure(log, "Public job API read failed", error=error, level=logging.WARNING)
        return 0, None, f"{error.__class__.__name__}: {str(error)[:120]}"


def host(url):
    return (urlsplit(url or "").hostname or "").lower()


def registered_domain(url_or_host):
    """A rough registered domain: the last two labels, or three for the common
    second-level country suffixes (com.sg, com.hk, co.th, com.au, co.nz, govt.nz,
    co.uk, com.my). Enough to compare an apply link with a company website."""
    name = url_or_host if "/" not in url_or_host else host(url_or_host)
    labels = name.split(".")
    if len(labels) >= 3 and labels[-2] in ("com", "co", "org", "net", "gov", "govt", "edu", "ac") \
            and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def unwrap_redirect(url):
    """The destination behind a tracking redirect, which is never the answer
    itself. Follows up to three wrappers and returns the innermost web address."""
    unwrapped = 0
    while unwrapped < 3:
        query = parse_qs(urlsplit(url).query)
        inner = ""
        for name in TRACKING_PARAMETERS:
            for value in query.get(name, []):
                value = unquote(value)
                if value.startswith("http"):
                    inner = value
                    break
            if inner:
                break
        if not inner:
            return url
        url = inner
        unwrapped += 1
    return url


def embedded_state_text(page_html):
    """Text inside the state that script-built careers sites ship in the HTML
    (JSON.parse("…"), __NEXT_DATA__, window.__INITIAL_STATE__). The JD is
    often there when the visible page is an empty shell, so a plain request
    can still read it."""
    parts = []
    for match in re.finditer(r'JSON\.parse\("((?:[^"\\]|\\.)*)"\)', page_html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except ValueError:
            continue
    for match in re.finditer(r'<script[^>]+(?:id="__NEXT_DATA__"|type="application/json")[^>]*>'
                             r"(.*?)</script>", page_html, re.DOTALL):
        parts.append(match.group(1))
    for match in re.finditer(r"window\.__[A-Za-z_]+__?\s*=\s*(\{.*?\})\s*;?\s*</script>",
                             page_html, re.DOTALL):
        parts.append(match.group(1))
    text = " ".join(parts)
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = html.unescape(text.replace("\\n", " ").replace("\\/", "/").replace('\\"', '"'))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)
