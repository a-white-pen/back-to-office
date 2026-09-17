"""Convert source job descriptions to deterministic plain text.

HTML-bearing boards use ``description_text_from_html``; LinkedIn uses the
separate, deliberately narrow ``description_text_from_linkedin`` rule.

Unsupported prospective HTML raises ``UnsupportedConstruct`` so the caller can
quarantine the observation instead of using a lossy fallback. Nothing here
logs JD prose or contact values.
"""

import re
import unicodedata
from html import unescape as _unescape_html
from html.parser import HTMLParser

# Raw-token protection

# Literal visible tokens shaped like <R123456> would otherwise be eaten as an
# unknown start tag. They are swapped for placeholders before the parse and
# restored verbatim into visible output afterwards. The two placeholder marks
# are chosen per input from the private-use code points that occur neither in
# the raw HTML nor in anything its character references decode to — exactly
# what the parser can emit as text — so input cannot be mistaken for a
# placeholder. An input without a raw token is never touched.
_RAW_TOKEN = re.compile(r"<R[0-9]+>")
_PLACEHOLDER_MARKS = (range(0xE000, 0xF900),           # BMP private use
                      range(0xF0000, 0xFFFFE),         # plane 15 private use
                      range(0x100000, 0x10FFFE))       # plane 16 private use


def _placeholder_marks(html):
    """Two code points absent from `html` and from its decoded character
    references; UnsupportedConstruct when the input holds every candidate
    (≥137,468 distinct private-use code points — not a description)."""
    present = set(html) | set(_unescape_html(html))
    chosen = []
    for block in _PLACEHOLDER_MARKS:
        for code in block:
            mark = chr(code)
            if mark not in present:
                chosen.append(mark)
                if len(chosen) == 2:
                    return chosen[0], chosen[1]
    raise UnsupportedConstruct("raw-token protection")

# Ignored content
_IGNORED_CONTENT = {"head", "script", "style", "template"}

# Structural tags
_BLOCK = {
    "address", "article", "aside", "blockquote", "body", "details", "dialog",
    "div", "dl", "dt", "dd", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "html",
    "main", "nav", "p", "section", "summary",
}

# Unsupported prospective constructs
_UNSUPPORTED = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "img", "pre"}
_UNSUPPORTED_OL_ATTRS = {"start", "reversed", "type"}

# Invisible-character normalization
# ZWSP, ZWNJ, ZWJ, WORD JOINER, FUNCTION APPLICATION, INVISIBLE SEPARATOR,
# NOMINAL DIGIT SHAPES and BOM form the token-local repair set.
_REPAIR_INVISIBLES = "\u200b‌‍⁠⁡⁣⁯﻿"
# SOFT HYPHEN plus the always-removed controls; ZWNJ and ZWJ are preserved.
_REMOVED_INVISIBLES = set("­⁠⁡⁣⁯﻿")
# A repair token is a maximal run of this ASCII set plus the invisibles.
_REPAIR_RUN = re.compile(r"[A-Za-z0-9@._:/?&=%+~#\-" + _REPAIR_INVISIBLES + r"]+")
_STRIP_INVISIBLES = re.compile("[" + _REPAIR_INVISIBLES + "]")

_SPACE_RUN = re.compile(r" {2,}")


class UnsupportedConstruct(Exception):
    """Typed refusal to normalize, such as ``table`` or ``ol[start]``."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Line:
    """One logical-line candidate: raw text parts plus its list context.

    item_key identifies the innermost open list item the line belongs to
    (None outside items); the marker/indent decision is deferred to
    finalization because ordered numbering is lazy and emptiness is known only
    after whitespace cleanup.
    """

    __slots__ = ("depth", "item_key", "list_id", "ordered", "parts")

    def __init__(self, item_key, depth, list_id, ordered):
        self.parts = []
        self.item_key = item_key
        self.depth = depth
        self.list_id = list_id
        self.ordered = ordered


class _Extractor(HTMLParser):
    """Collect visible text and logical-boundary events in one parser pass."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_stack = []
        self._lines = []
        self._current = None
        self._stack = []
        self._next_list_id = 0
        self._next_item_id = 0

    # -- list / line state ---------------------------------------------------

    def _boundary(self):
        if self._current is not None:
            self._lines.append(self._current)
            self._current = None

    def _innermost_item(self):
        for entry in reversed(self._stack):
            if entry["item"] is not None:
                return entry
        return None

    def _open_line(self):
        entry = self._innermost_item()
        if entry is None:
            self._current = _Line(None, 0, None, False)
        else:
            self._current = _Line(entry["item"], entry["depth"],
                                  entry["list_id"], entry["ordered"])

    def _open_list(self, ordered):
        self._boundary()
        self._stack.append({"ordered": ordered, "item": None,
                            "depth": len(self._stack) + 1,
                            "list_id": self._next_list_id})
        self._next_list_id += 1

    def _open_item(self):
        if not self._stack:
            self._open_list(ordered=False)        # Orphan li recovery.
        top = self._stack[-1]
        if top["item"] is not None:               # A sibling li closes the prior item.
            self._boundary()
        top["item"] = self._next_item_id
        self._next_item_id += 1
        self._boundary()

    def _close_item(self):
        if self._stack and self._stack[-1]["item"] is not None:
            self._stack[-1]["item"] = None
        self._boundary()

    def _close_list(self, tag):
        if not self._stack or self._stack[-1]["ordered"] != (tag == "ol"):
            return  # Ignore mismatches rather than guessing browser DOM repair.
        self._close_item()
        self._stack.pop()
        self._boundary()

    # -- parser events -------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORED_CONTENT:
            self._ignored_stack.append(tag)
            return
        if self._ignored_stack:
            return
        if tag in _UNSUPPORTED:
            raise UnsupportedConstruct(tag)
        if tag == "ol":
            for name, _ in attrs:
                if name in _UNSUPPORTED_OL_ATTRS:
                    raise UnsupportedConstruct(f"ol[{name}]")
            self._open_list(ordered=True)
        elif tag == "ul":
            self._open_list(ordered=False)
        elif tag == "li":
            for name, _ in attrs:
                if name == "value":
                    raise UnsupportedConstruct("li[value]")
            self._open_item()
        elif tag in ("br", "hr"):
            self._boundary()                      # hr emits no marker.
        elif tag in _BLOCK:
            self._boundary()
        # Inline and unknown tags are transparent; do not invent glue spaces.

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _IGNORED_CONTENT:
            # Only the innermost matching ignored tag can end its region.
            # Mismatched or crossed closes stay fail-closed instead of exposing
            # hidden content or attempting browser-style tree repair.
            if self._ignored_stack and self._ignored_stack[-1] == tag:
                self._ignored_stack.pop()
            return
        if self._ignored_stack:
            return
        if tag in _UNSUPPORTED:
            return  # Only unsupported starts quarantine; stray ends are ignored.
        if tag == "li":
            self._close_item()
        elif tag in ("ul", "ol"):
            self._close_list(tag)
        elif tag in _BLOCK:
            self._boundary()

    def handle_data(self, data):
        if self._ignored_stack or not data:
            return
        if self._current is None:
            self._open_line()
        self._current.parts.append(data)          # No inline glue heuristic.

    # Comments, declarations and processing instructions are not visible text.
    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def lines(self):
        self._boundary()                          # EOF finalizes visible open state.
        return self._lines


def _repair_tokens(text):
    """Delete invisibles inside qualifying email- or URL-shaped runs."""

    def fix(match):
        run = match.group(0)
        stripped = _STRIP_INVISIBLES.sub("", run)
        if stripped == run:
            return run
        at = stripped.find("@")
        if (0 < at < len(stripped) - 1
                or stripped.startswith(("http://", "https://", "www."))):
            return stripped
        return run

    return _REPAIR_RUN.sub(fix, text)


def _normalize_unicode(text):
    """Apply general invisible handling and then Unicode NFC."""
    out = []
    for ch in text:
        if ch == "\u200b" or ch.isspace():
            out.append(" ")
        elif ch in _REMOVED_INVISIBLES:
            continue
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def description_text_from_html(html):
    """HTML → description_text per the locked spec; None when nothing remains.

    Unsupported prospective constructs raise ``UnsupportedConstruct`` so the
    caller can quarantine the observation without a lossy fallback.
    """
    if html is None:
        return None
    html = str(html)

    tokens, restore = [], None
    if _RAW_TOKEN.search(html):
        open_mark, close_mark = _placeholder_marks(html)

        def protect(match):
            tokens.append(match.group(0))
            return f"{open_mark}{len(tokens) - 1}{close_mark}"

        html = _RAW_TOKEN.sub(protect, html)
        restore = re.compile(re.escape(open_mark) + "([0-9]+)" + re.escape(close_mark))

    extractor = _Extractor()
    extractor.feed(html)                          # One parser instance, whole input.
    extractor.close()

    counters = {}                                 # list_id -> last assigned number
    numbered = {}                                 # item_key -> marker string
    out = []
    for line in extractor.lines():
        text = "".join(line.parts)
        if restore is not None:                   # marks occur nowhere in the input
            text = restore.sub(lambda m: tokens[int(m.group(1))], text)
        # Repair contact tokens before general handling can turn ZWSP into a
        # space and split a recoverable email address or URL.
        text = _repair_tokens(text)
        text = _normalize_unicode(text)
        text = _SPACE_RUN.sub(" ", text).strip()
        if not text:
            continue
        if line.item_key is None:
            out.append(text)
            continue
        first_of_item = line.item_key not in numbered
        if first_of_item:
            marker = ""
            if line.ordered:                      # Number only nonempty items.
                counters[line.list_id] = counters.get(line.list_id, 0) + 1
                marker = f"{counters[line.list_id]}. "
            numbered[line.item_key] = marker
            indent = "  " * (line.depth - 1)
            out.append(indent + numbered[line.item_key] + text)
        else:
            indent = "  " * (line.depth - 1) + "  "
            out.append(indent + text)
    return "\n".join(out) or None


# LinkedIn plaintext cleanup

_LINKEDIN_CHROME = re.compile(r"Show more\s+Show less\s*$")


def description_text_from_linkedin(job_description):
    """Apply LinkedIn's intentionally narrow plaintext cleanup.

    ``Show more`` + whitespace + ``Show less`` at the end is appended UI chrome
    around complete listing text; occurrences mid-description stay. No other
    LinkedIn-specific cleanup exists.
    """
    if job_description is None:
        return None
    text = _LINKEDIN_CHROME.sub("", str(job_description), count=1)
    text = text.strip()
    return text or None
