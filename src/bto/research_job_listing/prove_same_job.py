"""Verify that a candidate is this vacancy on the employer's own board.

check_vacancy_page reads JD overlap, structured job details and platform
records. accept_vacancy also checks employer ownership, requisition numbers
and title agreement so a neighbouring vacancy cannot pass on boilerplate.
Search steps propose addresses; these checks never search or accept a guess
without evidence.
"""

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import private_adapters
from .check_employer_board import call_counters, company_name_words, employer_controls
from .fetch_page import embedded_state_text, fetch, fetch_json, host, unwrap_redirect
from .hiring_systems import (
    JOB_READERS,
    is_aggregator,
    is_reposting_address,
    parse_board,
    vendor_of,
    workday_job,
)

_TITLE_NOISE = {"senior", "junior", "lead", "principal", "staff", "associate", "assistant",
                "manager", "head", "director", "vp", "avp", "svp", "executive", "specialist",
                "analyst", "engineer", "the", "and", "of", "for", "with", "in", "to", "a",
                "an", "ii", "iii", "iv", "i", "sr", "jr", "role", "job", "position"}

_GENERIC_SEGMENTS = {"careers", "career", "jobs", "job", "en", "us", "sg", "search", "apply",
                     "vacancies", "openings", "listing", "listings", "home", "index",
                     "opportunities", "positions", "role", "roles", "join", "en-us", "en-sg",
                     "en-gb", "global", "external", "details", "detail", "view", "posting",
                     "postings"}

# Words on a page that place a job in each BTO market.
_MARKET_PLACE_WORDS = {"sg": ("singapore",), "hk": ("hong kong", "hongkong"),
                       "th": ("thailand", "bangkok"),
                       "au": ("australia", "sydney", "melbourne", "brisbane", "perth"),
                       "nz": ("new zealand", "auckland", "wellington")}

_NON_JOB_PATH = re.compile(
    r"/(news|blog|press|about|diversity|inclusion|stories|insights|events|media|investor)",
    re.IGNORECASE)
# "Containment" is share_of_jd_on_page: the share of the JD's five-word phrases
# found on the candidate page. Above about 0.3, two texts are the same job far
# beyond title coincidence.
CONTAINMENT_STRONG = 0.30
CONTAINMENT_WEAK = 0.15
# A page with no title is refused below this; above it, it goes on to the
# checks a titled page gets.
NO_TITLE_MIN_CONTAINMENT = 0.50
# Vendors whose job pages are script shells: the vendor's API is the page.
SCRIPT_SHELL_VENDORS = ("workday", "oracle_hcm", "workable", "eightfold", "avature")

# The share of the listing title's distinctive words the candidate title must
# carry; strict compares raw words, both ways.
TITLE_COVERAGE = 0.6
STRICT_TITLE_COVERAGE = 0.7
_RAW_TITLE_NOISE = {"and", "the", "for", "with", "our", "you", "your", "job", "role", "based"}

_REQUISITION_PATTERNS = (
    (r"(?:Job|Requisition|Req|Position|Vacancy)\s*(?:Number|Number:|No\.?|ID|Code)"
     r"\s*[:#]?\s*([A-Z]{0,4}-?\d{5,10})"),
    r"\(([A-Z]?\d{5,10})\)\s*$",
    r"\b([A-Z]{1,3}\d{7,10})\b",
    r"#\s?(\d{5,8})\b",
)


@dataclass
class Verdict:
    """What check_vacancy_page decided about one candidate page, and why."""

    ok: bool
    reason: str                          # short machine label
    url: str = ""
    mechanism: str = ""                  # how the text was read: direct, api or embedded_json
    # which of company, title and location the page names
    facets: list = field(default_factory=list)
    # the page's first JobPosting: title, org, location, valid_through,
    # date_posted, url, identifier
    jsonld: dict = field(default_factory=dict)
    page_title: str = ""
    vendor: str = ""
    fetch_error: str = ""
    note: str = ""


# ── words, places and phrases ─────────────────────────────────────────────

def title_words(title):
    """The words of a job title that name the work, without seniority or generic role nouns."""
    return [w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(w) > 2 and w not in _TITLE_NOISE]


def _raw_title_words(title):
    """Every meaningful word of a title, seniority and role nouns included.

    The bracketed tail is dropped first. A board appends the same location and
    relocation wording to every job it carries, and those shared words inflate
    the overlap between two jobs that differ only in seniority.
    """
    core = re.sub(r"[\(\[].*?[\)\]]", " ", title or "")
    return {w for w in re.findall(r"[a-z0-9]+", core.lower())
            if len(w) >= 3 and w not in _RAW_TITLE_NOISE}


def location_words(location, market):
    words = [w for w in [(location or "").lower().strip()] if w]
    words += list(_MARKET_PLACE_WORDS.get((market or "").lower(), ()))
    return words


def could_be_one_vacancy(url):
    """Whether an address could be one vacancy's page rather than a careers
    landing page. Lenient on purpose: any job number, a long slug or two
    meaningful path segments pass, and the page's text decides the rest.
    Compare looks_like_one_vacancy, which is strict because a yes there
    refuses a board candidate."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.strip("/").split("/") if s]
    meaningful = [s for s in segments if s.lower() not in _GENERIC_SEGMENTS]
    if not meaningful and not re.search(r"\d{4,}", parts.query):
        return False
    has_number = (any(re.search(r"\d{4,}", s) for s in segments)
                  or bool(re.search(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}", parts.query)))
    long_slug = any(len(s) > 14 for s in meaningful)
    uuid_like = any(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", s) for s in segments)
    return has_number or long_slug or uuid_like or len(meaningful) >= 2


def company_in_host(url, name):
    candidate_host = host(url)
    return any(w in candidate_host for w in company_name_words(name))


def match_facets(text, *, company, title, location, market):
    """Which of company, title and location the text names."""
    low = (text or "").lower()
    found = []
    company_words = company_name_words(company)
    if company_words and any(w in low for w in company_words):
        found.append("company")
    words = title_words(title)
    if words and sum(w in low for w in words) >= max(1, (len(words) + 1) // 2):
        found.append("title")
    if any(word in low for word in location_words(location, market)):
        found.append("location")
    return found


def jsonld_summary(page):
    """The page's first schema.org JobPosting, cut to the fields the checks
    read; {} when it has none."""
    if not page.jsonld_jobs:
        return {}
    job = page.jsonld_jobs[0]
    organisation = job.get("hiringOrganization") or {}
    place = job.get("jobLocation") or {}
    if isinstance(place, list):
        place = place[0] if place else {}
    address = (place.get("address") or {}) if isinstance(place, dict) else {}
    identifier = job.get("identifier")
    return {"title": str(job.get("title", ""))[:120],
            "org": str(organisation.get("name", "") if isinstance(organisation, dict)
                       else organisation)[:80],
            "location": " ".join(
                str(address.get(k, ""))
                for k in ("addressLocality", "addressRegion", "addressCountry")).strip()[:80],
            "valid_through": str(job.get("validThrough", ""))[:32],
            "date_posted": str(job.get("datePosted", ""))[:32],
            "url": str(job.get("url", ""))[:200],
            "identifier": str((identifier or {}).get("value", "") if isinstance(identifier, dict)
                              else job.get("identifier", ""))[:60]}


def five_word_phrases(text):
    """The text's overlapping five-word phrases."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(words[i:i + 5]) for i in range(max(0, len(words) - 4))}


def share_of_jd_on_page(jd_text, page_text):
    """The share of the JD's five-word phrases that also appear on the page.

    Board copies are reformatted, cut short and wrapped in boilerplate, so the
    share of the JD found on the page is the honest measure, not the overlap of
    the two as a whole.
    """
    jd_phrases, page_phrases = five_word_phrases(jd_text), five_word_phrases(page_text)
    if not jd_phrases or not page_phrases:
        return 0.0
    return len(jd_phrases & page_phrases) / len(jd_phrases)


# ── reading the job's own text ────────────────────────────────────────────

def _text_from_vendor_api(url):
    """The job's text through the vendor's own JSON when the page is a script
    shell. Returns (text, title, location, vendor) or None."""
    vendor, tenant, extra = parse_board(url)
    if vendor == "workday" and extra.get("site"):
        path = urlsplit(url).path
        i = path.find("/job/")
        if i >= 0:
            _, job, _ = workday_job(extra["tenant"], extra["site"], extra["host"], path[i:])
            if job:
                return job.get("text", ""), job.get("title", ""), job.get("location", ""), "workday"
    if vendor in JOB_READERS and tenant and extra.get("job_id"):
        _, job, _ = JOB_READERS[vendor](tenant, extra["job_id"])
        if job:
            return job.get("text", ""), job.get("title", ""), job.get("location", ""), vendor
    candidate_host = host(url)
    if vendor == "workable":
        match = re.search(r"apply\.workable\.com/([^/]+)/j/([A-Za-z0-9]+)", url)
        if match:
            _, answer, _ = fetch_json("https://apply.workable.com/api/v2/accounts/"
                                      f"{match.group(1)}/jobs/{match.group(2)}")
            if isinstance(answer, dict) and answer.get("title"):
                text = re.sub(r"<[^>]+>", " ", (answer.get("description") or "") + " "
                              + (answer.get("requirements") or "") + " "
                              + (answer.get("benefits") or ""))
                return (text, answer.get("title", ""),
                        str((answer.get("location") or {}).get("city", "")), "workable")
    if vendor == "oracle_hcm":
        match = re.search(r"/sites/(CX_\d+)/(?:job|requisitions/preview)/(\d+)", url)
        if match:
            _, answer, _ = fetch_json(
                f"https://{candidate_host}/hcmRestApi/resources/latest"
                "/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                f"&finder=ById;Id=%22{match.group(2)}%22,siteNumber={match.group(1)}")
            item = ((answer or {}).get("items") or [{}])[0]
            if item.get("Title"):
                text = re.sub(r"<[^>]+>", " ",
                              html.unescape(str(item.get("ExternalDescriptionStr", ""))))
                return text, item.get("Title", ""), item.get("PrimaryLocation", ""), "oracle_hcm"
    if vendor == "eightfold":
        match = re.search(r"/careers/job/(\d+)|[?&]pid=(\d+)", url)
        job_id = (match.group(1) or match.group(2)) if match else ""
        if job_id:
            _, answer, _ = fetch_json(
                f"https://{candidate_host}/api/apply/v2/jobs/{job_id}"
                f"?domain={candidate_host.split('.')[0]}.com",
                headers={"Referer": f"https://{candidate_host}/careers",
                         "Accept": "application/json, text/plain, */*"})
            if isinstance(answer, dict) and answer.get("name"):
                text = re.sub(r"<[^>]+>", " ", answer.get("job_description", "") or "")
                return text, answer.get("name", ""), answer.get("location", ""), "eightfold"
    return private_adapters.read_vacancy(url)


def check_vacancy_page(url, listing, page=None):
    """Accept a candidate only on evidence a person would accept: the page's own
    text shares the JD's five-word phrases, or a schema.org JobPosting or the
    vendor's record names the same title with some shared text. A match on
    company and title words alone is reported as `weak_facets_only` and never
    counts as found."""
    if not url:
        return Verdict(False, "empty")
    url = unwrap_redirect(url)
    if is_aggregator(url):
        return Verdict(False, "aggregator", url=url)
    if not could_be_one_vacancy(url):
        return Verdict(False, "not_specific", url=url)
    vendor = vendor_of(url)
    if _NON_JOB_PATH.search(urlsplit(url).path) and not vendor:
        return Verdict(False, "non_job_path", url=url)
    if page is None:
        page = fetch(url)
    text = page.text if page.ok else ""
    title, location, mechanism = page.title, "", page.mechanism
    jsonld = jsonld_summary(page) if page.html else {}
    # The bot-wall test is a guess from phrases. A page that carries JobPosting
    # data is the real page, whatever words it also prints.
    if page.blocked and jsonld:
        page.blocked = False
        text = page.text
    # Vendors whose pages are script shells: the API is the page. The address
    # after redirects is used, because a short apply link only reveals its
    # tenant after the redirect.
    if (vendor in SCRIPT_SHELL_VENDORS or private_adapters.script_shell(url)
            or len(text) < 300 or page.blocked):
        from_api = _text_from_vendor_api(page.final_url if page.final_url else url)
        if from_api is None and page.final_url and page.final_url != url:
            from_api = _text_from_vendor_api(url)
        if from_api:
            text, title, location, mechanism = from_api[0], from_api[1], from_api[2], "api"
            vendor = vendor or from_api[3]
    # A script shell that ships its state in the HTML.
    if page.html and len(text) < 300:
        embedded = embedded_state_text(page.html)
        if len(embedded) >= 300:
            text, mechanism = embedded, "embedded_json"
    if page.final_url and is_aggregator(page.final_url):
        return Verdict(False, "redirected_to_aggregator", url=url, note=page.final_url)
    if not text:
        if vendor:
            reason = "unverified_ats_unreadable"
        elif company_in_host(url, listing.advertiser_name):
            reason = "unverified_company_domain_unreadable"
        else:
            reason = "unreadable_untrusted"
        return Verdict(False, reason, url=url, mechanism=mechanism, vendor=vendor,
                       fetch_error=page.error)
    # Answer with the employer's page, never a tracking host that redirected to it.
    answer = url
    if page.final_url and host(page.final_url) != host(url) and not is_aggregator(page.final_url):
        answer = page.final_url.split("?utm_", 1)[0]
    containment = share_of_jd_on_page(listing.jd_text, text)
    # A script shell whose visible text is navigation while the JD sits in
    # the state it ships in the HTML.
    if containment < CONTAINMENT_WEAK and page.html:
        embedded = embedded_state_text(page.html)
        if len(embedded) >= 300:
            embedded_containment = share_of_jd_on_page(listing.jd_text, embedded)
            if embedded_containment > containment:
                text, containment, mechanism = embedded, embedded_containment, "embedded_json"
    facets = match_facets(f"{title} {location} {text}", company=listing.advertiser_name,
                          title=listing.title, location=listing.job_location,
                          market=listing.market)
    title_facets = match_facets(f"{title} {jsonld.get('title', '')}", company="",
                                title=listing.title, location="", market="")
    note = f"containment={containment:.2f}"
    found = {"mechanism": mechanism, "facets": facets, "jsonld": jsonld, "page_title": title[:120],
             "vendor": vendor, "note": note}
    if containment >= CONTAINMENT_STRONG:
        return Verdict(True, "content_strong", url=answer, **found)
    jsonld_title_agrees = bool(jsonld) and "title" in match_facets(
        jsonld.get("title", ""), company="", title=listing.title, location="", market="")
    if containment >= CONTAINMENT_WEAK and ("title" in title_facets or jsonld_title_agrees):
        return Verdict(True, "content_weak_title", url=answer, **found)
    if "company" in facets and "title" in facets:
        return Verdict(False, "weak_facets_only", url=url, **found)
    return Verdict(False, "read_clean_no_match", url=url, **found)


# ── requisition numbers, the one token unique to a vacancy ────────────────

def requisition_ids(listing):
    """The employer's own requisition number, where the listing prints one.

    It is unique to this vacancy across the whole employer, so it separates a
    job from its neighbours where the title cannot. Employers print it in the
    title, under "Job Number", or inside brackets.
    """
    found, seen = [], set()
    for text in (listing.title or "", (listing.jd_text or "")[:1500]):
        for pattern in _REQUISITION_PATTERNS:
            for match in re.finditer(pattern, text):
                value = match.group(1).strip()
                if value and value not in seen and not value.startswith("0000"):
                    seen.add(value)
                    found.append(value)
    return found[:2]


def requisition_matches(listing, url, page_text=""):
    """Whether a requisition number the listing prints also appears in the
    candidate. None when the listing prints none: no signal either way."""
    numbers = requisition_ids(listing)
    if not numbers:
        return None
    haystack = (url + " " + (page_text or "")).lower()
    return any(number.lower() in haystack for number in numbers)


# ── the title check ───────────────────────────────────────────────────────

def title_agrees(listing_title, other_title, strict=False):
    """The candidate must be this job, not another job at the same employer.

    One employer's jobs share boilerplate, so shared text alone accepts a
    neighbouring vacancy: two jobs one title word apart can share almost all
    their text. Coverage is one-way, because an employer adds a department,
    a brand or a requisition number but rarely drops the words that name the
    role. title_words() has already removed seniority and the generic role
    nouns every job in a department shares.
    """
    if not (other_title or "").strip():
        return True                                   # nothing to disagree with
    core = re.sub(r"[\(\[].*?[\)\]]", " ", listing_title or "")
    core = re.split(r"\s+[|–—]\s+", core)[0]
    mine = set(title_words(core)) or set(title_words(listing_title or ""))
    theirs = set(title_words(other_title))
    if not mine or not theirs:
        return True
    shared = mine & theirs
    if len(shared) / len(mine) < TITLE_COVERAGE:
        return False
    if not strict:
        return True
    # Weak JD evidence needs raw title words to match both ways. Keep
    # seniority and role nouns so a neighbouring vacancy cannot pass.
    mine_raw, theirs_raw = _raw_title_words(listing_title), _raw_title_words(other_title)
    if not mine_raw or not theirs_raw:
        return True
    common = mine_raw & theirs_raw
    return min(len(common) / len(mine_raw), len(common) / len(theirs_raw)) >= STRICT_TITLE_COVERAGE


# ── the check ─────────────────────────────────────────────────────────────

def accept_vacancy(url, listing, calls=None, board=""):
    """Is this the listing's own vacancy, on a page the employer controls?

    Returns (accepted, evidence). Accepts on one of two footings:
      requisition  the employer's own requisition number appears on the page
      content      the page's text shares the JD's phrases and the title agrees
    """
    calls = call_counters(calls)
    employer = listing.employer
    if not url:
        return False, {"why": "no address"}
    if is_reposting_address(url) or is_aggregator(url):
        return False, {"why": "re-posting board"}
    calls["http"] = calls.get("http", 0) + 1
    verdict = check_vacancy_page(url, listing)
    if not verdict.ok:
        return False, {"why": f"unverified: {verdict.reason}"}
    answer = verdict.url or url
    page = fetch(answer, timeout=20)
    text = page.text or ""
    containment = round(share_of_jd_on_page(listing.jd_text, text), 3) if text else 0.0
    page_title = (verdict.page_title or page.title or "")
    requisition_found = requisition_matches(listing, answer, text)

    on_employer_host = employer_controls(answer, employer, listing.company_website or "")
    if board and host(answer) == host(board):
        on_employer_host = True
    if not on_employer_host and not vendor_of(answer):
        return False, {"why": "not on a host the employer controls", "containment": containment}

    if requisition_found is True:
        return True, {"footing": "requisition", "containment": containment,
                      "requisition": requisition_ids(listing), "page_title": page_title[:90]}
    # Without a page title to compare, require stronger JD overlap to
    # distinguish this vacancy from another job at the same employer.
    if not (page_title or "").strip():
        head = " ".join((text or "")[:300].split())
        if title_agrees(listing.title, head) and containment >= CONTAINMENT_STRONG:
            return True, {"footing": "content, title read from the page body",
                          "containment": containment, "page_head": head[:90]}
        if containment < NO_TITLE_MIN_CONTAINMENT:
            return False, {"why": f"no title on the page and text overlap only {containment}",
                           "containment": containment}
    elif not title_agrees(listing.title, page_title):
        return False, {"why": "title names a different vacancy", "containment": containment,
                       "page_title": page_title[:90]}
    facets = match_facets(f"{page_title} {text[:4000]}", company=employer,
                          title=listing.title, location=listing.job_location, market=listing.market)
    # A vendor page is a script shell: this second read returns navigation and
    # scores 0, even though check_vacancy_page already proved the overlap through
    # the vendor's own record. Where that happened, its verdict is the
    # evidence, not this re-read.
    strong = verdict.reason == "content_strong" or containment >= CONTAINMENT_STRONG
    weak_but_placed = (containment >= CONTAINMENT_WEAK and "location" in facets
                       and title_agrees(listing.title, page_title, strict=True))
    # When the re-read has no JD text, the vendor record remains the
    # evidence. Require a title match both ways to exclude neighbouring jobs.
    vendor_record = (verdict.reason == "content_weak_title" and containment < CONTAINMENT_WEAK
                     and title_agrees(listing.title, page_title, strict=True))
    if strong or weak_but_placed or vendor_record:
        footing = ("content" if strong
                   else "content+location" if weak_but_placed
                   else "vendor record + title")
        return True, {"footing": footing, "containment": containment, "reason": verdict.reason,
                      "facets": facets, "page_title": page_title[:90]}
    return False, {"why": f"text does not match ({containment})", "reason": verdict.reason,
                   "page_title": page_title[:90]}
