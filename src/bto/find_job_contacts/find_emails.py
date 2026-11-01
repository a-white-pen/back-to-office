"""Find a trusted employer domain, then look up and check work email addresses.

Try LinkedIn's employer website, employer-owned research links, then Hunter's
matching company domain. Hunter's free Email Count must find addresses there.
For each person, check a printed address first, then Hunter's result, then a
generated pattern. Mailbox checks and address lookups use Hunter credits;
the caller decides when to hold them back.

"Seen online" requires a real source page. Hunter's Google search links alone
do not prove that an address was printed anywhere.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .find_relevant_personnel.classify_found_persons import is_half_name, plain_letters
from .identify_company import (
    domain_names_company,
    is_company_domain,
    registrable_domain,
    without_legal_ending,
)

# The words after "emails at example.com (...)" on the People panel's job line.
DOMAIN_SOURCE_WORDS = {
    "linkedin_website": "employer website on its LinkedIn listing",
    "company_job_page": "the company's own job page",
    "hunter": "the email service's domain for the company name",
}
GOOGLE_SEARCH_SOURCE = "https://www.google.com/search"
DEFAULT_PATTERN = "{first}.{last}"


@dataclass
class EmailDomain:
    """The company's trusted email domain and where it came from. `tried`
    lists the domains rejected before it because Hunter knows no addresses
    there; with no trusted domain, `domain` and `source` are None."""

    domain: str | None
    source: str | None
    tried: list = field(default_factory=list)


def find_email_domain(company, direct_link, hunter):
    """The company's EmailDomain, from Hunter's free reads only. Raises
    HunterUnavailable when Hunter fails."""
    tried = []
    for domain, source in _domain_candidates(company, direct_link, hunter):
        if domain in tried:
            continue
        if hunter.email_count(domain) > 0:
            return EmailDomain(domain, source, tried)
        tried.append(domain)
    return EmailDomain(None, None, tried)


def look_up_email(person, domain, hunter, patterns):
    """Fill `person.email_address`, `email_kind` and `email_confidence`.

    `patterns` remembers each domain's address pattern for the search, so it
    is bought once. A person without a full name and without a printed
    address is left as not looked up. Raises HunterUnavailable when Hunter
    fails, and then leaves the three fields as they were: a failed Look up
    never loses an address an earlier lookup found.
    """
    if person.printed_email:
        check = hunter.verify_email(person.printed_email)
        kind = {"valid": "valid_mailbox_found", "invalid": "no_mailbox"}.get(
            check.get("status"), "unchecked_seen_online")
        _set_email(person, person.printed_email, kind, _confidence(check.get("score")))
        return
    if not domain:
        return
    # An earlier lookup's address is looked for again, not trusted as printed.
    names = _first_and_last_name(person.name)
    if names is None:
        _set_email(person, None, "not_found" if is_half_name(person.name) else None, None)
        return
    first, last = names
    found = hunter.find_email(domain, first, last)
    if found.get("email"):
        check = hunter.verify_email(found["email"])
        status = check.get("status") or (found.get("verification") or {}).get("status")
        if status == "valid":
            kind = "valid_mailbox_found"
        elif status == "invalid":
            kind = "no_mailbox"
        elif _seen_online(found):
            kind = "unchecked_seen_online"
        else:
            kind = "pattern_guess"
        _set_email(person, found["email"], kind,
                   _confidence(check.get("score"), found.get("score")))
        return

    if domain not in patterns:
        patterns[domain] = hunter.domain_pattern(domain) or DEFAULT_PATTERN
    address = build_address(patterns[domain], first, last, domain)
    if address is None:
        _set_email(person, None, "not_found", None)
        return
    check = hunter.verify_email(address)
    kind = {"valid": "valid_mailbox_built", "invalid": "no_mailbox"}.get(
        check.get("status"), "unchecked_our_guess")
    _set_email(person, address, kind, _confidence(check.get("score")))


def build_address(pattern, first, last, domain):
    """The address a pattern such as "{first}.{last}" or "{f}{last}" gives for
    a name, in plain lower-case letters; None when a part is empty."""
    first_letters = re.sub(r"[^a-z]", "", plain_letters(first))
    last_letters = re.sub(r"[^a-z]", "", plain_letters(last))
    if not first_letters or not last_letters:
        return None
    local = (pattern.replace("{first}", first_letters).replace("{last}", last_letters)
             .replace("{f}", first_letters[0]).replace("{l}", last_letters[0]))
    if "{" in local or not local:
        return None
    return f"{local}@{domain}"


def same_company_name(hunter_name, company_name):
    """True when Hunter's company name is the company's name, legal endings
    aside, or the start of it: Hunter's "Example" matches "Example Bank
    Singapore", but Hunter's "Example Shop" is another company than "Example"."""
    ours = " ".join(re.findall(r"[a-z0-9]+", plain_letters(without_legal_ending(company_name) or "")))
    theirs = " ".join(re.findall(r"[a-z0-9]+", plain_letters(without_legal_ending(hunter_name) or "")))
    return bool(ours and theirs) and (ours == theirs or ours.startswith(theirs + " "))


def email_domain_line(email_status, domain, source, tried):
    """The job line's words about emails, such as "emails at example.com
    (employer website on its LinkedIn listing)"; None before the email step."""
    if email_status == "checked" and domain:
        return f"emails at {domain} ({DOMAIN_SOURCE_WORDS.get(source, source)})"
    if email_status == "no_trusted_domain":
        return ("No trusted email domain" + (f" (tried {', '.join(tried)})" if tried else ""))
    if email_status == "held_back":
        return "email checks held back"
    if email_status == "unavailable":
        return "email check unavailable"
    return None


def _domain_candidates(company, direct_link, hunter):
    """(domain, source) candidates in the order of the module docstring.
    Hunter's Domain Finder is asked only when the earlier candidates carry no
    addresses."""
    if company.website and company.website_source == "linkedin_website":
        domain = registrable_domain(company.website)
        if is_company_domain(domain):
            yield domain, "linkedin_website"
    for url in ((direct_link or {}).get("company_job_board_url"),
                (direct_link or {}).get("vacancy_url")):
        domain = registrable_domain(url)
        if is_company_domain(domain) and domain_names_company(domain, company.name):
            yield domain, "company_job_page"
    if company.name:
        for answer in hunter.find_domains(company.name):
            domain = registrable_domain(answer.get("domain"))
            if is_company_domain(domain) and same_company_name(answer.get("company_name"),
                                                               company.name):
                yield domain, "hunter"


def _first_and_last_name(name):
    """Split given names from the surname for a complete personal name.

    Return None for an abbreviated surname, a single word or contact details."""
    words = (name or "").split()
    if len(words) < 2 or is_half_name(name) or "@" in name or re.search(r"\d", name):
        return None
    return " ".join(words[:-1]), words[-1]


def _set_email(person, address, kind, confidence):
    """Set the three email fields together, once Hunter has answered."""
    person.email_address, person.email_kind, person.email_confidence = address, kind, confidence


def _seen_online(found):
    """True when Hunter found the address printed on a real page."""
    if found.get("source_type") != "found":
        return False
    for source in found.get("sources") or []:
        uri = source.get("uri") if isinstance(source, dict) else None
        if not isinstance(uri, str) or uri.startswith(GOOGLE_SEARCH_SOURCE):
            continue
        try:
            page = urlsplit(uri)
            if page.scheme in ("http", "https") and page.hostname:
                return True
        except ValueError:
            continue
    return False


def _confidence(*scores):
    """The first score given, as a whole number from 0 to 100."""
    for score in scores:
        if isinstance(score, (int, float)):
            return max(0, min(100, round(score)))
    return None
