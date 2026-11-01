"""Parse printed phone numbers and associate web results with a nearby name.

Listing and SGDI readers reuse the number parser. Web lookup reuses existing
search results and requires the person's name nearby, so a switchboard
elsewhere on a page does not count. Channels are direct_line or mobile;
the page dials from value, so url is null.
"""

import re
import urllib.parse

# Candidate numbers: digits with spaces, dots, dashes or brackets between them.
NUMBER = re.compile(r"(?<![\w+])(\+?\d[\d ().-]{6,18}\d)(?!\w)")
# A year range in a career history is not a phone number.
YEAR_RANGE = re.compile(r"^(?:19|20)\d\d\s*[-–]\s*(?:19|20)\d\d$")
# Dialling codes of the five markets, for numbers written with a leading +.
COUNTRY_CODES = {"65": "sg", "852": "hk", "66": "th", "61": "au", "64": "nz"}
# How close to the name a number must be printed, in characters.
CHARACTERS_AFTER_NAME = 80
CHARACTERS_BEFORE_NAME = 40


def phone_numbers_in(text, market):
    """Phone numbers in `text` that are valid for the market, in order, each once."""
    found, seen = [], set()
    for match in NUMBER.finditer(text or ""):
        number = " ".join(match.group(1).split())
        digits = re.sub(r"\D", "", number)
        if digits in seen or YEAR_RANGE.match(number) or not _is_phone_number(number, market):
            continue
        seen.add(digits)
        found.append(number)
    return found


def phone_channel(number, market, *, evidence):
    """One `channels` entry for a phone number."""
    return {
        "kind": "mobile" if _is_mobile(number, market) else "direct_line",
        "value": number,
        "url": None,
        "attributed": True,
        "evidence": evidence,
    }


def phone_numbers_near_name(name, pages, market):
    """Numbers printed close to `name` in web-search answers, as channels whose
    evidence is the page they came from."""
    channels, seen = [], set()
    wanted = (name or "").casefold()
    if not wanted:
        return channels
    for page in pages:
        text = f"{page.get('title') or ''} · {page.get('description') or ''}"
        folded = text.casefold()
        start = folded.find(wanted)
        while start >= 0:
            window_start = max(0, start - CHARACTERS_BEFORE_NAME)
            window = text[window_start:start + len(wanted) + CHARACTERS_AFTER_NAME]
            for number in phone_numbers_in(window, market):
                digits = re.sub(r"\D", "", number)
                if digits not in seen:
                    seen.add(digits)
                    channels.append(phone_channel(number, market, evidence=_page_label(page)))
            start = folded.find(wanted, start + 1)
    return channels


def _is_phone_number(number, market):
    """True when the digits fit the market's numbering plan; a number written
    with + needs only a plausible length."""
    digits = re.sub(r"\D", "", number)
    if number.startswith("+"):
        return 8 <= len(digits) <= 15
    market = (market or "").lower()
    if market == "sg":
        return len(digits) == 8 and digits[0] in "689"
    if market == "hk":
        return len(digits) == 8 and digits[0] in "235679"
    if market == "th":
        return len(digits) in (9, 10) and digits[0] == "0"
    if market in ("au", "nz"):
        return 9 <= len(digits) <= 11 and digits[0] == "0"
    return False


def _is_mobile(number, market):
    """Mobile by the numbering plan of the number's country; anything else is
    shown as a direct line."""
    digits = re.sub(r"\D", "", number)
    # Numbers in Singapore and Hong Kong have no leading 0, and a number written
    # with + loses its country code. A 0 is added so that, in every market, the
    # digit after the 0 decides.
    if number.startswith("+"):
        code = next((c for c in COUNTRY_CODES if digits.startswith(c)), None)
        if code is None:
            return False
        market, digits = COUNTRY_CODES[code], "0" + digits[len(code):]
    elif (market or "").lower() in ("sg", "hk"):
        digits = "0" + digits
    first = digits[1:2]
    return {
        "sg": first in "89",
        "hk": first in "569",
        "th": first in "689",
        "au": first == "4",
        "nz": first == "2",
    }.get((market or "").lower(), False)


def _page_label(page):
    """The page a number came from, in words: its title, or else its host."""
    title = " ".join((page.get("title") or "").split())
    if title:
        return title if len(title) <= 60 else title[:57] + "…"
    return urllib.parse.urlsplit(page.get("url") or "").hostname or "a web page"
