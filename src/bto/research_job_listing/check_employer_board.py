"""Verify that a candidate is an employer-owned board carrying vacancies.

Check ownership and reject reposting sites before counting vacancies through
the page or its hiring-platform API. Careers brochures and individual job
pages do not count as boards. Vacancy matching belongs to prove_same_job."""

import html
import logging
import re
import urllib.parse

from ..log_errors import log_failure
from .fetch_page import fetch, host, registered_domain, unwrap_redirect
from .hiring_systems import (
    JOB_LISTERS,
    VENDOR_HOSTS,
    is_aggregator,
    is_reposting_address,
    parse_board,
    vendor_of,
)

log = logging.getLogger(__name__)

# The board page for each vendor, worked out from any one job address on it.
_WORKDAY_JOB = re.compile(r"https?://([a-z0-9.-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                          r"(?:([a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_-]+)")
_WORKDAY_SITE_JOB = re.compile(r"https?://(wd\d+)\.myworkdaysite\.com/(?:([a-z]{2}-[A-Z]{2})/)?"
                               r"recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)")
_ORACLE_JOB = re.compile(r"(https?://[a-z0-9.-]+\.oraclecloud\.com)"
                         r"/hcmUI/CandidateExperience/([a-z-]+)/sites/([A-Za-z0-9_]+)")
_SUCCESSFACTORS_JOB = re.compile(r"(https?://[a-z0-9.-]+)/job/")
_JOB_LINK = re.compile(r"(?i)/(job|jobs|career|careers|position|positions|vacanc|opening"
                       r"|requisition|posting)s?/[^/?#\"']{3,}")

# The last path segment of a careers index, never of one vacancy. Without this
# a company homepage that links to /en/career counts as a job board.
_CAREERS_SECTION_TAILS = {"job", "jobs", "career", "careers", "vacancy", "vacancies", "opening",
                          "openings", "position", "positions", "opportunity", "opportunities",
                          "search", "index", "home", "all", "list", "lists", "job-lists", "joblist",
                          "search-jobs", "job-search", "current-openings", "open-positions",
                          "apply", "recruitment", "join-us", "work-with-us"}

# The segment that introduces one vacancy in a link. "career" (singular) is
# not here: an employer can hang /career/why-us and /career/contact-us off its
# careers section, and those are navigation, not jobs.
_VACANCY_LINK_PARENTS = {"job", "jobs", "position", "positions", "posting", "postings",
                         "requisition", "requisitions", "vacancy", "vacancies", "opening",
                         "openings", "careers", "roles"}

# A page whose address does not say "careers" must carry a board's worth of
# vacancies before it counts as one.
MIN_BOARD_LINKS = 8
# The last path segment of a page that lists vacancies, never of one vacancy.
_INDEX_PAGE_TAILS = {"requisitions", "jobs", "careers", "career", "search", "openings", "positions",
                     "vacancies", "opportunities", "roles", "joblist", "job-search", "search-jobs",
                     "all", "list", "index", "index.html", "results", "home", "apply"}
_INDEX_PAGE_PARENT = re.compile(
    r"(?i)(all-?jobs|view-?all|search|category|categories|browse|go|brand|location)")
_ONE_VACANCY_PARENTS = {"job", "jobs", "role", "roles", "position", "positions", "posting",
                        "postings", "requisition", "vacancy", "opening", "careers"}
_CAREERS_ADDRESS = re.compile(r"(?i)(career|job|vacanc|opening|position|recruit|join|opportunit"
                              r"|hiring|work-with-us|search|talent)")

# Words that say nothing about which company a name belongs to.
_COMPANY_NOISE = {"the", "and", "of", "for", "inc", "ltd", "pte", "llc", "group", "technology",
                  "technologies", "limited", "company", "co", "corp", "corporation", "holdings",
                  "solutions", "services", "international", "global", "asia", "pacific", "apac",
                  "singapore", "hong", "kong", "thailand", "sdn", "bhd", "plc", "public", "branch",
                  "operations", "sea", "se", "ag", "gmbh", "sa", "nv", "bv", "kk", "pty",
                  "australia", "nz", "new", "zealand", "hk", "sg", "th"}
_COMPANY_SUFFIXES = {"ltd", "limited", "pte", "plc", "inc", "llc", "corp", "corporation", "co",
                     "company", "gmbh", "bv", "nv", "sa", "pty", "sdn", "bhd", "kk", "group",
                     "holdings", "international"}
_ARTICLES = {"the", "a", "an"}
# Words a company routinely adds to its own name in a domain: "example" and
# "examplegroup" are the same firm, but "example" and "examplefoods" need not be.
_GENERIC_DOMAIN_WORDS = ("", "group", "global", "asia", "apac", "careers", "jobs", "hk", "sg",
                         "th", "au", "nz", "corp", "holdings", "inc", "intl", "international",
                         "co", "bank")
# A company name has to be long enough for a domain built from it to be
# evidence. A five-letter name belongs to several companies; a seventeen-letter
# one belongs to one.
DISTINCTIVE_NAME_CHARS = 8


def company_name_words(name):
    """The words of a company name that say which company it is."""
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(w) > 2 and w not in _COMPANY_NOISE]


def call_counters(calls):
    """The call counters the shared helpers expect, whatever the caller passed."""
    counters = calls if calls is not None else {}
    for name in ("http", "api", "search"):
        counters.setdefault(name, 0)
    return counters


# ── a board page and its vacancy links ────────────────────────────────────

def _is_link_to_one_vacancy(url):
    """Whether a link on a board page goes to one job, not to a careers section
    page. Used only to count a page's vacancy links."""
    segments = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    if len(segments) < 2:
        return False
    tail = segments[-1].lower()
    if (tail in _CAREERS_SECTION_TAILS
            or tail.endswith((".css", ".js", ".pdf", ".png", ".jpg", ".html#"))):
        return False
    if segments[-2].lower() in _VACANCY_LINK_PARENTS:
        return True
    return bool(re.search(r"\d", tail)) or len(tail.split("-")) >= 4


def board_page_from_job_address(url):
    """The page that lists the vacancies a given job address belongs to."""
    if not url or is_aggregator(url):
        return ""
    match = _WORKDAY_JOB.match(url)
    if match:
        tenant, workday_number, locale, site = match.groups()
        return (f"https://{tenant}.{workday_number}.myworkdayjobs.com/"
                + (f"{locale}/" if locale else "") + site)
    match = _WORKDAY_SITE_JOB.match(url)
    if match:
        workday_number, locale, tenant, site = match.groups()
        return (f"https://{workday_number}.myworkdaysite.com/" + (f"{locale}/" if locale else "")
                + f"recruiting/{tenant}/{site}")
    match = _ORACLE_JOB.match(url)
    if match:
        return (f"{match.group(1)}/hcmUI/CandidateExperience/{match.group(2)}"
                f"/sites/{match.group(3)}/requisitions")
    vendor, tenant, _ = parse_board(url)
    if vendor == "greenhouse" and tenant:
        return f"https://job-boards.greenhouse.io/{tenant}"
    if vendor == "lever" and tenant:
        return f"https://jobs.lever.co/{tenant}"
    if vendor == "ashby" and tenant:
        return f"https://jobs.ashbyhq.com/{tenant}"
    if vendor == "smartrecruiters" and tenant:
        return f"https://jobs.smartrecruiters.com/{tenant}"
    parts = urllib.parse.urlsplit(url)
    if "gh_jid=" in (parts.query or ""):                 # an employer page in front of Greenhouse
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    match = _SUCCESSFACTORS_JOB.match(url)
    if match:
        return match.group(1) + "/search/"               # the SuccessFactors careers site search
    # Anything else: the folder the vacancy sits in.
    segments = [s for s in parts.path.split("/") if s]
    return (f"{parts.scheme}://{parts.netloc}/" + "/".join(segments[:-1])
            if len(segments) > 1 else "")


def count_vacancies(url, calls):
    """(how many vacancies, how they were counted, a name to test against the
    employer) for a candidate board.

    The name is the page title, or the tenant name when the vendor's list API
    answered.

    A vendor answers through its own API, which is exact. Anything else is
    counted from the page's own links, because a careers page that lists no
    vacancy is a brochure, not a job board. -1 means a real board whose
    vacancies arrive by script, so they cannot be counted.
    """
    vendor = vendor_of(url)
    if vendor in JOB_LISTERS:
        _, tenant, _ = parse_board(url)
        if tenant:
            calls["api"] += 1
            try:
                _, jobs, _ = JOB_LISTERS[vendor](tenant)
                if jobs:
                    return len(jobs), f"{vendor} list API", tenant
            except Exception as error:
                # A failed API read leaves the page's vacancy links as the fallback.
                log_failure(log, "Board vacancy count failed vendor=%s", vendor,
                            error=error, level=logging.WARNING)
    calls["http"] += 1
    page = fetch(url, timeout=25)
    if page.status != 200 or not page.html:
        return 0, f"http {page.status}", ""
    links = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page.html,
                             re.DOTALL | re.IGNORECASE):
        href = match.group(1)
        text = re.sub(r"<[^>]+>|\s+", " ", html.unescape(match.group(2))).strip()
        if len(text) < 5 or not _JOB_LINK.search(href):
            continue
        full = urllib.parse.urljoin(page.final_url or url, href)
        if not is_aggregator(full) and _is_link_to_one_vacancy(full):
            links.add(full)
    if len(links) >= 3:
        return len(links), "vacancy links on the page", page.title or ""
    # A script-built board can name its vendor without printing vacancy
    # links. Reuse the shared vendor hosts so both checks recognise the same sites.
    for fragment, vendor_name in VENDOR_HOSTS:
        if fragment in page.html:
            return -1, f"JavaScript board ({vendor_name})", page.title or ""
    # The board may be on the vendor's own host and still not print it: a
    # Workday page shell names no host at all.
    if vendor:
        return -1, f"JavaScript board ({vendor}, by host)", page.title or ""
    return 0, "no vacancy links", page.title or ""


def looks_like_one_vacancy(url):
    """Whether an address is one job rather than the page that lists them.

    The tail decides: an index word is a list, a bare number under a vacancy
    word is a job, and a category page keeps its number but says "view all
    jobs" on the way there. Strict, because a yes refuses a board candidate.
    """
    segments = [s for s in urllib.parse.urlsplit(url).path.strip("/").split("/") if s]
    if not segments:
        return False
    # A page suffix is not part of the name: /en/careers/jobs.html is the jobs
    # index, not one job under /careers.
    tail = re.sub(r"\.(html?|aspx?|php|jsp)$", "", segments[-1].lower())
    if tail in _INDEX_PAGE_TAILS:
        return False
    parent = segments[-2].lower() if len(segments) >= 2 else ""
    if _INDEX_PAGE_PARENT.search(parent):
        return False
    if re.fullmatch(r"\d{4,}", tail) or re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", tail):
        return True
    if parent in _ONE_VACANCY_PARENTS and len(tail) > 3:
        return True
    return bool(re.search(r"\d{5,}", tail) and len(tail) > 10)


# ── who owns a host ───────────────────────────────────────────────────────

def acronyms(employer):
    """The initials a company may trade under. A domain that is an acronym is
    invisible to a word test: "Example Consulting Group" can be ecg.example.

    Two forms are built, because the company suffix is sometimes part of the
    acronym and sometimes not: one keeps "Group", the other drops it. A leading
    "The" is always dropped.
    """
    words = list(re.findall(r"[A-Za-z]+", employer or ""))

    def build(chosen):
        return "".join(w.lower() if len(w) <= 2 else w[0].lower() for w in chosen)

    full = build([w for w in words if w.lower() not in _ARTICLES])
    trimmed = build([w for w in words
                     if w.lower() not in _COMPANY_SUFFIXES and w.lower() not in _ARTICLES])
    return {a for a in (full, trimmed) if len(a) >= 3}


def _domain_label_is(label, name):
    """Whether the domain's first part (the "example" of example.com.sg) is
    this name, give or take a word a company adds to its own domain. A
    substring is not enough: "examplehealthcare" is not "Example Health"."""
    return (label == name
            or any(label == name + word for word in _GENERIC_DOMAIN_WORDS)
            or any(name == label + word for word in _GENERIC_DOMAIN_WORDS))


def contradicts_website(url, website, employer=""):
    """The listing names the employer's domain and this address is on another one.

    Employers do own second domains: a listing can name a short domain while
    the employer publishes its vacancies on a domain spelled out from its full
    name. So a different domain is only a contradiction when the candidate's
    domain is not plainly built from the employer's own name.

    Require a distinctive name before accepting a second domain built
    from it. Short names can belong to several unrelated employers. Vendor
    hosts are exempt because their domains belong to the hiring platform.
    """
    if not website or not url or vendor_of(url):
        return False
    website_host = host(website if website.startswith("http") else "https://" + website)
    website_domain = registered_domain(website_host) or ""
    candidate_domain = registered_domain(host(url)) or ""
    if not website_domain or candidate_domain == website_domain:
        return False
    words = [w for w in company_name_words(employer) if len(w) >= 3]
    joined = "".join(words)
    if len(joined) >= DISTINCTIVE_NAME_CHARS:
        label = re.sub(r"[^a-z0-9]", "", candidate_domain.split(".")[0].lower())
        if _domain_label_is(label, joined):
            return False                          # a second domain this employer owns
    return True


def employer_controls(url, employer, website=""):
    """A host this employer controls: its own domain, its initials, or a vendor
    tenant whose host or path carries its name.

    A re-posting site that puts the employer's name in its path is not the
    employer, so for a host that is not a vendor the name must be in the host.
    """
    if not url or is_reposting_address(url) or is_aggregator(url):
        return False
    candidate_host = host(url)
    flat_host = re.sub(r"[^a-z0-9]", "", candidate_host.lower())
    flat_path = re.sub(r"[^a-z0-9]", "", urllib.parse.urlsplit(url).path.lower())
    words = [w for w in company_name_words(employer) if len(w) >= 3]
    initials = acronyms(employer)
    if website:
        website_host = host(website if website.startswith("http") else "https://" + website)
        website_domain = registered_domain(website_host) or ""
        if website_domain and registered_domain(candidate_host) == website_domain:
            return True
        # A candidate on another domain is judged by contradicts_website,
        # which accept_board calls. The rule lives in one place, because only
        # that function knows when a different domain is a different company.
    if vendor_of(url):
        # A vendor tenant is named for its client somewhere, in the host or
        # in the path. One word is enough here, because the vendor's own
        # domain carries no other client.
        return (any(w in flat_host for w in words) or any(w in flat_path for w in words)
                or any(a in flat_host + flat_path for a in initials))
    # The employer's own domain. A substring is not enough when only one
    # distinctive word survives: "The Example Solutions Limited" reduces to
    # "example", which is also the start of examplefoods.com, another company.
    # So a single word must match the registered domain's own label, give or
    # take a generic company word.
    label = re.sub(r"[^a-z0-9]", "",
                   (registered_domain(candidate_host) or candidate_host).split(".")[0].lower())
    if words and (_domain_label_is(label, "".join(words)) or _domain_label_is(label, words[0])):
        return True
    return any(a in flat_host for a in initials)


def names_employer(text, employer):
    """Whether a page title or text names the employer.

    A board page usually titles itself with the employer's name, so every
    distinctive word being present is good evidence. A vendor host can name
    nobody while its page title names the employer.
    """
    flat = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    short = re.sub(r"[^a-z0-9]", "", (employer or "").lower())[:18]
    words = [w for w in company_name_words(employer) if len(w) >= 3]
    return (bool(short and short in flat) or any(a in flat for a in acronyms(employer))
            or (bool(words) and all(w in flat for w in words)))


# ── the check ─────────────────────────────────────────────────────────────

def accept_board(url, listing, calls=None):
    """Is this the employer's own job board? Returns (accepted, evidence)."""
    calls = call_counters(calls)
    employer = listing.employer
    if not url:
        return False, {"why": "no address"}
    if is_reposting_address(url) or is_aggregator(url):
        return False, {"why": "re-posting board"}
    # One vacancy is not the board it sits on. A job page on the employer's
    # board host links to other jobs, which is enough to fool a link count. A
    # shortener hides the same thing behind a redirect, so the unwrapped
    # address is what gets tested.
    target = unwrap_redirect(url) or url
    if looks_like_one_vacancy(url) or looks_like_one_vacancy(target):
        return False, {"why": "this is one vacancy, not the board that lists them"}
    if host(target) != host(url) or len(urllib.parse.urlsplit(target).path.strip("/")) <= 24:
        # A shortener hides its destination until it is followed: a short
        # vendor link can be one vacancy, and nothing in the address says so.
        calls["http"] = calls.get("http", 0) + 1
        landed = fetch(url, timeout=20).final_url or target
        if looks_like_one_vacancy(landed):
            return False, {"why": "this address redirects to one vacancy, not to a board"}
        target = landed
    count, how, title = count_vacancies(url, calls)
    if count == 0:
        return False, {"why": f"no vacancies readable ({how})", "page_title": title[:90]}
    # A corporate homepage that teases three jobs is not a job board. Either the
    # address says it is a careers page, or it must list a board's worth of them.
    # A board whose vacancies arrive by script (count -1) cannot be counted, so
    # it is not held to this.
    parts = urllib.parse.urlsplit(target)
    if (count > 0 and not _CAREERS_ADDRESS.search(f"{parts.hostname or ''}{parts.path or '/'}")
            and count < MIN_BOARD_LINKS):
        return False, {"why": f"not a careers address and only {count} vacancies on it",
                       "page_title": title[:90]}
    # A website named on the listing is the employer's own domain. A careers
    # page on another domain that is not a vendor is a different company, and
    # the page titling itself with the employer's name proves nothing: a
    # same-name company titles itself the same way. So this refusal comes
    # before the name test.
    if contradicts_website(url, listing.company_website or "", employer):
        return False, {"why": "on a different domain from the company website this listing names",
                       "page_title": (title or "")[:90], "website": listing.company_website}
    owned = employer_controls(url, employer, listing.company_website or "")
    named = names_employer(title, employer)
    if not (owned or named):
        return False, {"why": "host is not this employer's", "page_title": title[:90]}
    return True, {"vacancies": count, "how": how, "page_title": (title or "")[:90],
                  "ownership": "host" if owned else "page names the employer"}
