"""Find relevant public-sector officers in the Singapore Government Directory.

The public search endpoint needs an empty POST body so Content-Length is
sent. Its HTML lists agency, designation, name and work contact details.
Keep only officers relevant to this job. Uppercase surnames are moved last
to agree with the other sources and with email-address construction.
"""

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ...log_errors import log_failure
from ..find_phones import phone_channel
from .classify_found_persons import (
    RECRUITING_TITLE,
    FoundPerson,
    plain_letters,
    same_name,
    same_name_in_any_order,
    shared_words,
)

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.sgdi.gov.sg/api/v1/sgdi/GetSearchResult"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
           "Accept": "application/json"}
TIMEOUT_S = 20
EVIDENCE = "the Singapore Government Directory"
PERSON_ITEM = re.compile(r'<li class="type-d">(.*?)</li>', re.DOTALL)
AGENCY_NAME = re.compile(r'<p class="col-md-12 title"[^>]*><a [^>]*>(.*?)</a></p>', re.DOTALL)
DESIGNATION = re.compile(r'<p class="col-md-6 title">(.*?)</p>', re.DOTALL)
PHONE = re.compile(r'fa-phone"></span>([^<]*)<')
NAME = re.compile(r'fa-user"></span><a [^>]*>(.*?)</a>', re.DOTALL)
EMAIL = re.compile(r'fa-envelope"></span>(?:<span[^>]*>)?([^<]*@[^<]*)<')
TITLE_WORDS = re.compile(r"^(?:Ms|Mr|Mrs|Mdm|Dr|Prof|Er)\.?\s+")
SURNAME = re.compile(r"[A-Z][A-Z'-]+")


@dataclass
class Officer:
    """One person in an SGDI answer."""

    name: str
    designation: str
    department: str
    agencies: list
    phone: str | None
    email: str | None


def find_agency_officers(company_name, job_words):
    """The company's officers whose designation or department is recruiting
    or shares a team word with the role, as FoundPerson records."""
    people = []
    terms = [f"{company_name} human resource"]
    if job_words:
        terms.insert(0, f"{company_name} {job_words[0]}")
    for term in terms:
        for officer in search_directory(term):
            title = ", ".join(part for part in (officer.designation, officer.department) if part)
            relevant = RECRUITING_TITLE.search(title) or shared_words(title, job_words)
            if relevant and at_company(officer, company_name) and not any(
                    same_name(officer.name, p.name) for p in people):
                people.append(officer_as_person(officer))
    return people


def find_officer(person_name, company_name):
    """The directory entry for one person at the company, or None. The name
    may be written surname first or last."""
    for officer in search_directory(person_name):
        written_alike = (same_name(officer.name, person_name)
                         or same_name_in_any_order(officer.name, person_name))
        if written_alike and at_company(officer, company_name):
            return officer
    return None


def officer_as_person(officer):
    """The officer as a FoundPerson: publicly observed, with their printed
    email and direct line."""
    title = ", ".join(part for part in (officer.designation, officer.department) if part)
    channels = ([phone_channel(officer.phone, "sg", evidence=EVIDENCE)]
                if officer.phone else [])
    return FoundPerson(
        name=officer.name, found_by="sgdi", provenance="publicly_observed",
        title=title or None, location="Singapore", printed_email=officer.email,
        email_address=officer.email, channels=channels, in_market=True)


def at_company(officer, company_name):
    """True when one of the officer's agency names contains the company's
    name, compared as letters only."""
    wanted = _comparable(company_name)
    return bool(wanted) and any(wanted in _comparable(agency) for agency in officer.agencies)


def search_directory(term):
    """The people on the first page of SGDI's search for `term`. A directory
    that cannot be read gives no one; it never fails the search."""
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'term': term, 'page': 1})}"
    request = urllib.request.Request(url, data=b"", headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            answer = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        log_failure(log, "SGDI search unavailable", error=error, level=logging.WARNING)
        return []
    return parse_officers(answer.get("htmlModels") or "")


def parse_officers(markup):
    """Officer records from the `htmlModels` HTML of one SGDI answer."""
    officers = []
    for item in PERSON_ITEM.findall(markup):
        name = _text(NAME.search(item))
        if not name:
            continue
        agencies = [html.unescape(a).strip() for a in AGENCY_NAME.findall(item)]
        officers.append(Officer(
            # Awards follow the name after a comma; keep only the name.
            name=surname_last(TITLE_WORDS.sub("", name.split(",")[0]).strip()),
            designation=_text(DESIGNATION.search(item)) or "",
            # The last agency name is the most specific: the department.
            department=agencies[-1] if len(agencies) > 1 else "",
            agencies=agencies,
            phone=_text(PHONE.search(item)) or None,
            email=_text(EMAIL.search(item)) or None,
        ))
    return officers


def surname_last(name):
    """Normalize surname position and casing when the source distinguishes it.

    Names with all words uppercase, or none, stay as the source writes them."""
    words = name.split()
    capitals = [SURNAME.fullmatch(word) is not None for word in words]
    if all(capitals) or not any(capitals):
        return name
    leading = capitals.index(False)
    words = words[leading:] + words[:leading]
    return " ".join(word.title() if SURNAME.fullmatch(word) else word for word in words)


def _text(match):
    return " ".join(html.unescape(match.group(1)).split()) if match else ""


def _comparable(name):
    """Letters only, without a bracketed short form such as "(EXA)" and
    without "and" or "&", which the directory and the boards write differently."""
    words = plain_letters(re.sub(r"\(.*?\)", " ", name or "")).replace("&", " ")
    return "".join(w for w in re.findall(r"[a-z]+", words) if w != "and")
