"""Read contacts printed on the listing or its same-market LinkedIn copy.

Use the poster, board contacts and JD in that order. These contacts are
publicly observed and belong in named_on_listing. A contact with no printed
name is identified by its address or phone number."""

import re

from ..find_phones import phone_channel, phone_numbers_in
from .classify_found_persons import FoundPerson, plain_letters

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# A name is two to four capitalised words on one line.
NAME = r"((?:Ms|Mr|Mrs|Mdm|Dr)\.?[ \t]+)?([A-Z][a-zA-Z'-]+(?:[ \t]+[A-Z][a-zA-Z'-]+){1,3})"
# Match a name after a contact or application instruction.
NAME_AFTER_CONTACT_PHRASE = re.compile(
    r"(?i:\b(?:contact(?:\s+person)?|reach\s+out\s+to|speak\s+to|write\s+to|"
    r"(?:send|email)\s+(?:your\s+)?(?:cv|resume|application)s?\s+to|"
    r"personnel\s+name|consultant|recruiter))\s*:?\s+" + NAME)
# Singapore agency listings print the consultant beside an EA registration number.
NAME_BEFORE_REGISTRATION = re.compile(
    NAME + r"\s*[|(,\-–]?\s*(?i:(?:EA\s*)?(?:Personnel\s*)?"
    r"(?:Reg(?:istration)?\.?\s*(?:No\.?|Number)?|Registration)\s*[:#.]?\s*R\d{6,7}[A-Z]?)")
# Role and contact words can precede a name without being part of it.
WORDS_BEFORE_NAMES = {"senior", "consultant", "recruiter", "recruitment", "manager", "executive",
                      "partner", "director", "lead", "specialist", "associate", "principal", "hr",
                      "contact", "please", "email", "call"}
POSTER_NOTE = "Posted this job on LinkedIn"
# Phrases after "contact" that are not names.
NOT_NAMES = {"us", "our", "the", "human", "resources", "team", "talent"}


def find_people_in_listing(listing, linkedin_copy=None):
    """Everyone the listing names, as FoundPerson records, poster first."""
    facts = listing.get("board_facts") or {}
    copy_facts = (linkedin_copy or {}).get("board_facts") or {}
    market = listing.get("market")
    people = []

    poster = facts.get("poster_name") or copy_facts.get("poster_name")
    if isinstance(poster, str) and poster.strip():
        profile = facts.get("poster_profile_url") or copy_facts.get("poster_profile_url")
        # No title: the poster's own title comes from LinkedIn, when found there.
        people.append(FoundPerson(
            name=poster.strip(), found_by="listing", provenance="publicly_observed",
            linkedin_url=profile or None, note=POSTER_NOTE))

    for contact in facts.get("contacts") or []:
        kind, value = _contact_parts(contact)
        if value:
            _add_printed_contact(people, kind, value, market)

    description = listing.get("description_text") or ""
    for name in names_in_text(description):
        if not any(plain_letters(name) == plain_letters(p.name) for p in people):
            people.append(FoundPerson(
                name=name, found_by="listing", provenance="publicly_observed",
                note="Named on the listing as the contact"))
    for address in EMAIL.findall(description):
        _add_printed_contact(people, "email", address, market)
    for number in phone_numbers_in(description, market):
        _add_printed_contact(people, "phone", number, market)
    return people


def names_in_text(text):
    """Names the JD writes next to a contact phrase or a registration number."""
    names = []
    for pattern in (NAME_AFTER_CONTACT_PHRASE, NAME_BEFORE_REGISTRATION):
        for match in pattern.finditer(text or ""):
            words = match.group(2).split()
            while words and words[0].lower() in WORDS_BEFORE_NAMES:
                words.pop(0)
            name = " ".join(words)
            if (len(words) >= 2 and not {w.lower() for w in words} & NOT_NAMES
                    and name not in names):
                names.append(name)
    return names


def _contact_parts(contact):
    """(kind, value) from a board contact. SEEK gives {type, value} pairs;
    `type` may be missing, so the value decides when it is."""
    if isinstance(contact, dict):
        value = str(contact.get("value") or "").strip()
        kind = str(contact.get("type") or "").strip().lower()
    elif isinstance(contact, str):
        value, kind = contact.strip(), ""
    else:
        return None, None
    if kind not in ("email", "phone"):
        kind = "email" if EMAIL.fullmatch(value) else "phone"
    return kind, value


def _add_printed_contact(people, kind, value, market):
    """Attach a printed address or number to the one named person whose name
    it spells, or else add it as its own contact named by the value."""
    if any(value.lower() == (p.email_address or "").lower()
           or value in [c["value"] for c in p.channels] for p in people):
        return
    if kind == "email":
        local = plain_letters(value.split("@")[0])
        owner = next((p for p in people if p.found_by == "listing" and not p.printed_email
                      and _local_part_spells(local, p.name)), None)
        if owner is not None:
            owner.printed_email = owner.email_address = value
            return
        people.append(FoundPerson(
            name=value, found_by="listing", provenance="publicly_observed",
            title="printed on the listing", printed_email=value, email_address=value))
        return
    channel = phone_channel(value, market, evidence="printed on the listing")
    named = [p for p in people if p.found_by == "listing" and not re.search(r"[@\d]", p.name)
             and p.note != POSTER_NOTE]
    if len(named) == 1:
        # With one named contact, a number printed on the listing is theirs.
        named[0].channels.append(channel)
        return
    people.append(FoundPerson(
        name=value, found_by="listing", provenance="publicly_observed",
        title="printed on the listing", channels=[channel]))


def _local_part_spells(local, name):
    """Match email-name forms built from given names, surname or their initials."""
    words = re.findall(r"[a-z]+", plain_letters(name))
    if len(words) < 2:
        return False
    letters = re.sub(r"[^a-z]", "", local)
    first, last = words[0], words[-1]
    return letters in {first + last, last + first, first[0] + last, first, first + last[0]}
