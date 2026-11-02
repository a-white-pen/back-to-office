"""Merge people, group their titles and choose who gets a full lookup.

The groups describe printed titles, not who actually hires. People found
only through post reactions or comments remain under that post. A hidden
surname is completed only when the public profile address spells it out.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

GROUP_ORDER = ("named_on_listing", "recruiter", "hiring_manager_or_team", "also_at_company")
PROVENANCE_ORDER = ("publicly_observed", "provider_observed", "provider_inferred")
# Recruiting words. "HR" counts only as a word of its own, so "HRIS Analyst" stays out.
RECRUITING_TITLE = re.compile(
    r"\b(talent|recruit\w*|sourcer|sourcing|hr|human resources?|people partner)\b", re.IGNORECASE)
LEADING_TITLE = re.compile(
    r"\b(head|director|vice president|vp|chief|manager|lead|principal)\b", re.IGNORECASE)
SENIOR_TITLE = re.compile(r"\b(senior|sr|staff|principal|lead)\b", re.IGNORECASE)
JUNIOR_TITLE = re.compile(
    r"\b(intern|internship|trainee|graduate|student|apprentice)\b", re.IGNORECASE)
# Titles that run the company. With the other leading titles they lead everyone
# else at the company, and they stand in for a hiring team when none is found.
EXECUTIVE_TITLE = re.compile(
    r"\b(ceo|coo|cfo|cto|cio|chief|president|founder|managing director|general manager"
    r"|executive director)\b", re.IGNORECASE)
# A board seat or an adviser sits on the company, not in it: never a leader to contact.
BOARD_TITLE = re.compile(
    r"\b(board|non[- ]executive|chair\w*|advis[eo]r\w*|trustee)\b", re.IGNORECASE)

# Words in a job title that say how senior, not what the team does.
SENIORITY_WORDS = {
    "senior", "sr", "junior", "jr", "lead", "manager", "mgr", "vp", "avp", "svp", "head",
    "director", "principal", "staff", "associate", "assistant", "executive", "chief",
    "intern", "i", "ii", "iii", "iv", "group",
}
# Words in a job title that say where or how the job is done, or join its other words.
PLACE_AND_JOINING_WORDS = {
    "and", "of", "the", "for", "in", "apac", "emea", "asia", "sea", "global", "regional",
    "singapore", "hong", "kong", "thailand", "australia", "new", "zealand", "sg", "hk", "th",
    "au", "nz", "based", "remote", "hybrid", "contract", "permanent",
}
TITLE_NOISE = SENIORITY_WORDS | PLACE_AND_JOINING_WORDS
# Job nouns shared by many teams: "Analytics Engineer" is about analytics, not engineering.
GENERIC_JOB_NOUNS = {
    "engineer", "specialist", "analyst", "consultant", "officer", "architect", "developer",
    "scientist", "partner", "advisor", "expert",
}
# Words that name the same team in titles. LinkedIn's title filter matches
# words, so a team called "Data" is also found under "Analytics".
SYNONYMS = {
    "analytics": ("data", "insights"),
    "data": ("analytics",),
    "insights": ("analytics", "data"),
    "intelligence": ("analytics", "data"),
    "software": ("engineering",),
    "engineering": ("software",),
    "machine": ("ml", "ai"),
    "learning": ("ml",),
}


@dataclass
class FoundPerson:
    """One person's source evidence, classification and lookup results.

    Keep printed contact details separate from lookup results. Post-only
    people stay under their posts; is_key selects people for a full lookup.
    Classification fills group and reason without claiming who actually hires.
    """

    name: str
    found_by: str
    provenance: str
    title: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    note: str | None = None
    printed_email: str | None = None
    email_address: str | None = None
    email_kind: str | None = None
    email_confidence: int | None = None
    channels: list = field(default_factory=list)
    only_in_posts: bool = False
    in_market: bool = False
    group: str | None = None
    reason: str | None = None
    is_key: bool = False
    looked_up_at: datetime | None = None
    failure_reason: str | None = None

    def relevant_personnel_row(self):
        """The `job_tracking.relevant_personnel` columns this person fills."""
        return {
            "person_group": self.group,
            "name": self.name,
            "title": self.title,
            "location": self.location,
            "reason": self.reason,
            "provenance": self.provenance,
            "linkedin_url": self.linkedin_url,
            "email_address": self.email_address,
            "email_kind": self.email_kind,
            "email_confidence": self.email_confidence,
            "channels": self.channels,
            "looked_up_at": self.looked_up_at,
            "failure_reason": self.failure_reason,
        }


def plain_letters(text):
    """Lower case without accents, so "Zoë Groß" and "Zoe Gross" compare equal."""
    folded = unicodedata.normalize("NFKD", (text or "").replace("ß", "ss"))
    return "".join(c for c in folded if not unicodedata.combining(c)).lower()


def is_half_name(name):
    """Return True when only the surname's initial is printed."""
    return bool(re.search(r"\s[A-Za-z]\.$", (name or "").strip()))


def same_name(first, second):
    """Match identical names or a full surname against its printed initial."""
    a = " ".join(plain_letters(first).split())
    b = " ".join(plain_letters(second).split())
    if not a or not b:
        return False
    if a == b:
        return True
    for half, full in ((a, b), (b, a)):
        if is_half_name(half):
            # Split the abbreviated surname's initial from the given names.
            given, initial = half[:-3], half[-2]
            if full.startswith(given + " ") and full[len(given) + 1:].startswith(initial):
                return True
    return False


def same_name_in_any_order(first, second):
    """Match name words regardless of where a source places the surname."""
    words = sorted(plain_letters(first).split())
    return bool(words) and words == sorted(plain_letters(second).split())


def team_words(title, description=""):
    """What the team does, from the job title and any team the JD names.

    "Senior Analytics Engineer" gives ["Analytics Engineer", "Analytics"];
    "... join the Product Management team" adds "Product Management".
    Seniority, place and generic job nouns are dropped. At most six phrases.
    """
    title = re.sub(r"\(.*?\)", " ", title or "")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&+]*", title) if w.lower() not in TITLE_NOISE]
    found = [" ".join(words)] if words else []
    found += [w for w in words if len(w) >= 4 and w.lower() not in GENERIC_JOB_NOUNS]
    team_pattern = r"\b(?:the|our) ((?:[A-Z][A-Za-z&]+ ){0,2}[A-Z][A-Za-z&]+) [Tt]eam\b"
    for team in re.findall(team_pattern, description or "")[:3]:
        team = " ".join(w for w in team.split() if w.lower() not in TITLE_NOISE)
        if team:
            found.append(team)
    return list(dict.fromkeys(found))[:6]


def matching_words(job_words):
    """The single words, with their synonyms, that a person's title can share."""
    single = set()
    for phrase in job_words:
        for word in phrase.lower().split():
            if len(word) >= 4 and word not in GENERIC_JOB_NOUNS:
                single.add(word)
                single.update(SYNONYMS.get(word, ()))
    return single


def shared_words(title, job_words):
    """The job's team words (and synonyms) that appear in `title`, sorted."""
    title_words = set(re.findall(r"[a-z]+", plain_letters(title)))
    return sorted(matching_words(job_words) & title_words)


def group_of(person, job_words, place):
    """(group, reason) for one person, from their title only."""
    if person.found_by == "listing":
        return "named_on_listing", person.note
    if person.only_in_posts:
        return None, None
    title = person.title or ""
    where = f" in {place}" if place and person.in_market else ""
    if JUNIOR_TITLE.search(title):
        return "also_at_company", None
    if RECRUITING_TITLE.search(title):
        named = shared_words(title, job_words)
        if named and not person.in_market:
            return "recruiter", f"Recruiting title naming '{named[0]}'"
        return "recruiter", f"Recruiting title{where}"
    named = shared_words(title, job_words)
    if not named:
        return "also_at_company", None
    leading = LEADING_TITLE.search(title)
    if leading:
        word = leading.group(1)
        level = "VP" if word.lower() == "vp" else word.title()
        return "hiring_manager_or_team", f"{level}-level {named[0]} title{where}"
    if SENIOR_TITLE.search(title):
        return "hiring_manager_or_team", f"Senior title sharing '{named[0]}' with the role"
    core = " ".join(job_words[:1]).lower()
    if core and core in plain_letters(title):
        return "hiring_manager_or_team", f"Same title as the role{',' if where else ''}{where}"
    return "hiring_manager_or_team", f"Title sharing '{named[0]}' with the role"


def rank_in_group(person):
    """Sort key inside a group: leaders first, then senior titles, each in the
    job's market first. Lower sorts first. Everyone else at the company is led
    by the people who run it."""
    title = person.title or ""
    if person.group == "hiring_manager_or_team":
        tier = 0 if LEADING_TITLE.search(title) else 1 if SENIOR_TITLE.search(title) else 2
    elif person.group == "also_at_company":
        tier = leader_tier(title)
    else:
        tier = 0
    return (tier, not person.in_market)


def leader_tier(title):
    """0 for a title that runs the company, 1 for another leading title, 2 for
    the rest. A board seat or an adviser is never a leader."""
    if BOARD_TITLE.search(title):
        return 2
    if EXECUTIVE_TITLE.search(title):
        return 0
    return 1 if LEADING_TITLE.search(title) else 2


def merge_same_people(people):
    """One record per person. `people` comes in the steps' order (listing,
    LinkedIn, SGDI, web, posts), so the earliest record keeps its name and
    title and later records only fill gaps."""
    merged = []
    for person in people:
        match = next((kept for kept in merged if _same_person(kept, person)), None)
        if match is None:
            merged.append(person)
            continue
        for name in ("title", "location", "linkedin_url", "printed_email", "email_address"):
            if not getattr(match, name) and getattr(person, name):
                setattr(match, name, getattr(person, name))
        if is_half_name(match.name) and not is_half_name(person.name):
            match.name = person.name
        match.channels += [c for c in person.channels if c not in match.channels]
        match.only_in_posts = match.only_in_posts and person.only_in_posts
        match.in_market = match.in_market or person.in_market
        if PROVENANCE_ORDER.index(person.provenance) < PROVENANCE_ORDER.index(match.provenance):
            match.provenance = person.provenance
        if person.found_by == "listing" and match.found_by != "listing":
            match.found_by, match.note = "listing", person.note
    return merged


def classify(people, job_words, place):
    """Merge, group and order everyone found. Returns the ordered list the
    People panel shows: the four groups in order, then the post-only people."""
    merged = merge_same_people(people)
    for person in merged:
        person.group, person.reason = group_of(person, job_words, place)

    def position(person):
        group = GROUP_ORDER.index(person.group) if person.group else len(GROUP_ORDER)
        return (group, rank_in_group(person))

    return sorted(merged, key=position)


def pick_key_personnel(people, *, recruiters, hiring_team):
    """Mark who gets the full lookup inside the search: everyone named on the
    listing, then the first `recruiters` recruiters and the first
    `hiring_team` hiring managers and team members, in panel order. When no
    title shares a team word, as at a small company, the company's leaders
    among everyone else take the hiring team's places."""
    taken = {"recruiter": 0, "hiring_manager_or_team": 0}
    limits = {"recruiter": recruiters, "hiring_manager_or_team": hiring_team}
    no_team = not any(person.group == "hiring_manager_or_team" for person in people)
    for person in people:
        group = person.group
        if no_team and group == "also_at_company" and leader_tier(person.title or "") < 2:
            group = "hiring_manager_or_team"
        if group == "named_on_listing":
            person.is_key = True
        elif group in taken and taken[group] < limits[group]:
            person.is_key = True
            taken[group] += 1
    return [person for person in people if person.is_key]


def complete_half_name(name, public_identifier):
    """Complete an abbreviated surname only when the profile address supports it.

    The address must start with the given names, followed by a surname
    beginning with the printed initial."""
    match = re.fullmatch(r"(.+?)\s+([A-Za-z])\.", (name or "").strip())
    if not match or not public_identifier:
        return name
    given, initial = match.groups()
    # Ignore numeric profile suffixes when reconstructing a person's name.
    parts = [part for part in re.split(r"[-_]", public_identifier.lower())
             if part and not re.search(r"\d", part)]
    letters = "".join(parts)
    given_letters = re.sub(r"[^a-z]", "", plain_letters(given))
    surname = letters[len(given_letters):]
    if (not letters.startswith(given_letters) or len(surname) < 2
            or not surname.isalpha() or surname[0] != initial.lower()):
        return name
    return f"{given} {surname.capitalize()}"


def _same_person(first, second):
    first_url, second_url = linkedin_key(first.linkedin_url), linkedin_key(second.linkedin_url)
    if first_url and second_url and first_url == second_url:
        return True
    if same_name(first.name, second.name):
        return True
    # SGDI writes an officer's name surname last, but LinkedIn and the web may
    # write the same person's name surname first.
    return "sgdi" in (first.found_by, second.found_by) and same_name_in_any_order(
        first.name, second.name)


def linkedin_key(url):
    """A comparable form of a LinkedIn profile address, or None for the
    internal-id form ("/in/ACwAA..."): each Actor writes those differently for
    the same person, so they cannot be compared."""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url or "")
    if not match or re.fullmatch(r"AC[a-zA-Z0-9_-]{20,}", match.group(1)):
        return None
    return match.group(1).lower()
