"""Recognise hiring platforms and read their public job APIs.

A tenant is the employer's account on a platform, such as `example` in
jobs.lever.co/example. parse_board reads it from an address; search_the_board
discovers platforms embedded in an employer's own careers page. Reposting
sites are kept separate because their URLs cannot prove employer ownership.
"""

import html
import re
from urllib.parse import quote, urlsplit

from .fetch_page import fetch_json, host

# Host fragment → vendor. Order matters only for readability.
VENDOR_HOSTS = (
    ("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"),
    ("boards.greenhouse.io", "greenhouse"), ("job-boards.greenhouse.io", "greenhouse"),
    ("boards.eu.greenhouse.io", "greenhouse"), ("job-boards.eu.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"), ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"), ("ashbyhq.com", "ashby"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("icims.com", "icims"), ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"), ("successfactors.eu", "successfactors"),
    ("jobs.sap.com", "successfactors"),
    ("eightfold.ai", "eightfold"), ("phenompeople.com", "phenom"), ("phenom.com", "phenom"),
    ("oraclecloud.com", "oracle_hcm"), ("brassring.com", "brassring"),
    ("workable.com", "workable"), ("bamboohr.com", "bamboohr"),
    ("teamtailor.com", "teamtailor"), ("recruitee.com", "recruitee"),
    ("jobvite.com", "jobvite"), ("avature.net", "avature"), ("gr8people.com", "gr8people"),
    ("ats.rippling.com", "rippling"), ("rippling-ats.com", "rippling"),
    ("applytojob.com", "jazzhr"), ("breezy.hr", "breezy"),
    ("comeet.com", "comeet"), ("personio.de", "personio"), ("jobs.personio.com", "personio"),
    ("freshteam.com", "freshteam"), ("zohorecruit.com", "zoho"), ("manatal.com", "manatal"),
    ("jobadder.com", "jobadder"), ("pinpointhq.com", "pinpoint"), ("dover.com", "dover"),
    ("careers.hibob.com", "hibob"),
    ("csod.com", "cornerstone"), ("ultipro.com", "ukg"), ("ukg.com", "ukg"),
    ("paylocity.com", "paylocity"), ("adp.com", "adp"), ("hirebridge.com", "hirebridge"),
    ("recruiterbox.com", "recruiterbox"), ("jobscore.com", "jobscore"),
    ("polymer.co", "polymer"), ("wellfound.com", "wellfound"), ("homerun.co", "homerun"),
    ("talentlyft.com", "talentlyft"), ("careers-page.com", "manatal"),
    ("bullhornstaffing.com", "bullhorn"), ("vincere.io", "vincere"),
)

# Job boards and aggregators: they carry other employers' jobs, so no address
# on them is ever an employer's own board or vacancy page.
REPOSTING_HOSTS = ("linkedin.", "indeed.", "jobstreet.", "jobsdb.", "seek.com",
                   "mycareersfuture.", "glassdoor.", "jora.", "foundit.", "ziprecruiter.",
                   "monster.", "adzuna.", "jobsora", "trovit", "neuvoo", "talent.com",
                   "careerjet", "jobrapido", "whatjobs", "lensa", "simplyhired", "jobleads",
                   "glints.", "efinancialcareers", "grabjobs", "jobscentral", "fastjobs",
                   "jobsbank", "nodeflair", "myjobs", "wantedly", "kalibrr", "cake.me",
                   "jobtome", "workipedia", "jobscout", "recruit.net", "jooble", "himalayas.app",
                   "welcometothejungle", "remoteok", "weworkremotely", "builtin.", "dice.com",
                   "levels.fyi",
                   # Regional boards: an apply link on one board often points
                   # at another board, not at the employer.
                   "ctgoodjobs.", "recruit.com.hk", "cpjobs.com", "classifiedpost.",
                   "jobmarket.com.hk", "moovup.", "jobable.", "hongkongjobs.", "jobup.",
                   "jobsacademy", "jobstoday", "jobtopgun", "jobbkk", "jobthai", "jobnisit",
                   "thaijob", "workventure", "prosple", "gradsingapore", "jobsgopublic",
                   "tech-in-asia", "techinasia.com", "startup.jobs", "getlinks.", "hirist",
                   "bebee.", "seek.co.nz", "trademe.", "careerone.")

# Re-posting boards, directories and profile pages, matched on the host and
# path. None of them is the employer, however prominently it prints the
# employer's name.
REPOSTING_ADDRESS = re.compile(
    r"(?i)(linkedin|indeed|glassdoor|jobstreet|jobsdb|seek\.|mycareersfuture|jobsbank|"
    r"facebook|instagram|twitter|x\.com|youtube|wikipedia|crunchbase|bloomberg|zhipin|"
    r"jora|adzuna|neuvoo|talent\.com|simplyhired|ziprecruiter|careerjet|glints|foundit|"
    r"trueup|levels\.fyi|wellfound|builtin|himalayas|remoteok|weworkremotely|"
    r"freehire|workopia|dreamworkhq|techjobasia|grabjobs|jobstore|expatjobboard|workana|"
    r"jobboard|job-board|jobsboard|joblist|jobsite|dejobs\.org|cpjobs|ctgoodjobs|"
    r"jobsora|whatjobs|jobrapido|learn4good|jobleads|jobgether|startup\.jobs|jobvertise|"
    r"expatriates|salary\.com|hirist|instahyre|jobted|mitula|vacancies?\.|hunt\.jobs|"
    r"vertexaisearch\.cloud\.google\.com|grounding-api-redirect)")


def vendor_of(url):
    """The hiring-software vendor behind an address, or '' when the host is not a known one."""
    name = host(url)
    for fragment, vendor in VENDOR_HOSTS:
        if fragment in name:
            return vendor
    return ""


def is_aggregator(url):
    """Whether the host is a job board or aggregator that re-posts other employers' jobs."""
    name = host(url)
    return any(fragment.rstrip(".") in name for fragment in REPOSTING_HOSTS)


def is_reposting_address(url):
    """Whether the host and path are a re-posting board, directory or profile
    site, unless a vendor hosts it.

    Two traps this avoids: an employer's own board on a vendor host can carry
    the words "job board" in its host (job-boards.greenhouse.io), and a
    tracking parameter names whichever board sent the visitor
    (?utm_medium=jobboard), which says nothing about the host. Only the host
    and the path are tested.
    """
    parts = urlsplit(url)
    return (bool(REPOSTING_ADDRESS.search(f"{parts.hostname or ''}{parts.path}"))
            and not vendor_of(url))


# ── the tenant name inside a vendor address ──────────────────────────────

def parse_board(url):
    """(vendor, tenant, extra) from a vendor address, when the address carries
    the tenant. Workday's extra is (tenant, site, host) because its API needs
    all three. Returns (vendor, '', {}) when the vendor is known but the tenant
    cannot be read from the address."""
    vendor = vendor_of(url)
    parts = urlsplit(url)
    name, path = (parts.hostname or "").lower(), parts.path
    segments = [s for s in path.split("/") if s]
    if vendor == "workday":
        # Two shapes:
        #   https://{tenant}.wd{n}.myworkdayjobs.com/{locale?}/{site}/job/{location}/{slug}_{req}
        #   https://wd{n}.myworkdaysite.com/{locale?}/recruiting/{tenant}/{site}/job/...
        # Both answer the search at https://{host}/wday/cxs/{tenant}/{site}/jobs,
        # and on the myworkdayjobs shape that host is the tenant's own host.
        if "myworkdaysite.com" in name and "recruiting" in segments:
            i = segments.index("recruiting")
            tenant = segments[i + 1] if len(segments) > i + 1 else ""
            site = segments[i + 2] if len(segments) > i + 2 else ""
        else:
            tenant = name.split(".")[0]
            site = ""
            for i, segment in enumerate(segments):
                if segment in ("job", "details") and i > 0:
                    site = segments[i - 1]
                    break
            if not site and segments:
                site = (segments[1]
                        if re.match(r"^[a-z]{2}-[A-Z]{2}$", segments[0]) and len(segments) > 1
                        else segments[0])
        return vendor, tenant, {"tenant": tenant, "site": site, "host": name}
    if vendor == "greenhouse":
        # boards.greenhouse.io/{tenant}/jobs/{id} · job-boards.greenhouse.io/{tenant}/jobs/{id}
        if segments and segments[0] != "embed":
            job_id = segments[2] if len(segments) > 2 and segments[1] == "jobs" else ""
            return vendor, segments[0], {"job_id": job_id}
        return vendor, "", {}
    if vendor == "lever":
        # jobs.lever.co/{tenant}/{uuid}
        return (vendor, (segments[0] if segments else ""),
                {"job_id": segments[1] if len(segments) > 1 else ""})
    if vendor == "ashby":
        # jobs.ashbyhq.com/{tenant}/{uuid}
        return (vendor, (segments[0] if segments else ""),
                {"job_id": segments[1] if len(segments) > 1 else ""})
    if vendor == "smartrecruiters":
        # jobs.smartrecruiters.com/{Company}/{id}-{slug} · careers.smartrecruiters.com/{Company}/...
        if segments and segments[0] not in ("oneclick-ui",):
            return (vendor, segments[0],
                    {"job_id": (segments[1].split("-")[0] if len(segments) > 1 else "")})
        return vendor, "", {}
    return vendor, "", {}


# ── the vendors' public job lists (no key) ───────────────────────────────

def list_greenhouse(tenant):
    """Read Greenhouse's public vacancy list for one employer."""
    status, answer, error = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs")
    if not answer:
        return status, [], error
    jobs = [{"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""), "id": str(j.get("id", "")),
             "updated_at": j.get("updated_at", "")} for j in answer.get("jobs", [])]
    return status, jobs, ""


def list_lever(tenant):
    """Read Lever's public vacancy list for one employer."""
    status, answer, error = fetch_json(
        f"https://api.lever.co/v0/postings/{tenant}?mode=json&limit=500")
    if not isinstance(answer, list):
        return status, [], error or "unexpected shape"
    jobs = [{"title": j.get("text", ""),
             "location": ((j.get("categories") or {}).get("location") or ""),
             "url": j.get("hostedUrl", ""), "id": j.get("id", ""),
             "updated_at": str(j.get("createdAt", ""))} for j in answer]
    return status, jobs, ""


def list_ashby(tenant):
    """Read Ashby's public vacancy list for one employer."""
    status, answer, error = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{tenant}")
    if not answer:
        return status, [], error
    jobs = [{"title": j.get("title", ""), "location": j.get("location", ""),
             "url": j.get("jobUrl", ""), "id": j.get("id", ""),
             "updated_at": j.get("publishedAt", "")} for j in answer.get("jobs", [])]
    return status, jobs, ""


def list_smartrecruiters(tenant):
    """Read SmartRecruiters postings in bounded pages and retain completed pages."""
    jobs, offset, status, error = [], 0, 0, ""
    while True:
        status, answer, error = fetch_json(
            f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings"
            f"?limit=100&offset={offset}")
        if not answer:
            break
        for j in answer.get("content", []):
            location = j.get("location") or {}
            jobs.append({"title": j.get("name", ""),
                         "location": ", ".join(
                             x for x in (location.get("city"), location.get("country")) if x),
                         "url": f"https://jobs.smartrecruiters.com/{tenant}/{j.get('id')}",
                         "id": str(j.get("id", "")), "updated_at": j.get("releasedDate", "")})
        offset += 100
        if offset >= int(answer.get("totalFound", 0)) or offset > 2000:
            break
    return status, jobs, error if not jobs else ""


def search_workday(tenant, site, workday_host, text, limit=20):
    """Workday's own job search: the same call the careers site makes."""
    url = f"https://{workday_host}/wday/cxs/{tenant}/{site}/jobs"
    status, answer, error = fetch_json(url, method="POST",
                                       body={"appliedFacets": {}, "limit": limit, "offset": 0,
                                             "searchText": text})
    if not answer:
        return status, [], error
    jobs = []
    for j in answer.get("jobPostings", []):
        path = j.get("externalPath", "")
        jobs.append({"title": j.get("title", ""), "location": j.get("locationsText", ""),
                     "url": f"https://{workday_host}/{site}{path}" if path else "",
                     "id": (j.get("bulletFields") or [""])[0],
                     "updated_at": j.get("postedOn", "")})
    return status, jobs, ""


JOB_LISTERS = {"greenhouse": list_greenhouse, "lever": list_lever, "ashby": list_ashby,
               "smartrecruiters": list_smartrecruiters}


# ── one job's own text, from the vendor's API ─────────────────────────────

def _strip_tags(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


def greenhouse_job(tenant, job_id):
    """Read one Greenhouse vacancy and its plain-text description."""
    status, answer, error = fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{job_id}")
    if not answer:
        return status, {}, error
    return status, {"title": answer.get("title", ""),
                    "location": (answer.get("location") or {}).get("name", ""),
                    "text": _strip_tags(answer.get("content", "")),
                    "url": answer.get("absolute_url", ""),
                    "updated_at": answer.get("updated_at", "")}, ""


def lever_job(tenant, job_id):
    """Combine the description and section text for one Lever vacancy."""
    status, answer, error = fetch_json(f"https://api.lever.co/v0/postings/{tenant}/{job_id}")
    if not answer:
        return status, {}, error
    text = " ".join([answer.get("descriptionPlain", "")]
                    + [f"{item.get('text', '')} {item.get('content', '')}"
                       for item in answer.get("lists", [])]
                    + [answer.get("additionalPlain", "")])
    return status, {"title": answer.get("text", ""),
                    "location": ((answer.get("categories") or {}).get("location") or ""),
                    "text": _strip_tags(text), "url": answer.get("hostedUrl", ""),
                    "updated_at": str(answer.get("createdAt", ""))}, ""


def ashby_job(tenant, job_id):
    """Find one vacancy in the employer's Ashby job list."""
    status, answer, error = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true")
    if not answer:
        return status, {}, error
    for j in answer.get("jobs", []):
        if j.get("id") == job_id:
            return status, {"title": j.get("title", ""), "location": j.get("location", ""),
                            "text": _strip_tags(j.get("descriptionPlain")
                                                or j.get("descriptionHtml") or ""),
                            "url": j.get("jobUrl", ""), "updated_at": j.get("publishedAt", "")}, ""
    return status, {}, "job id not on board"


def smartrecruiters_job(tenant, job_id):
    """Combine the description sections for one SmartRecruiters vacancy."""
    status, answer, error = fetch_json(
        f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings/{job_id}")
    if not answer:
        return status, {}, error
    sections = (answer.get("jobAd") or {}).get("sections") or {}
    text = " ".join(_strip_tags((sections.get(name) or {}).get("text", ""))
                    for name in ("companyDescription", "jobDescription", "qualifications",
                                 "additionalInformation"))
    location = answer.get("location") or {}
    return status, {"title": answer.get("name", ""),
                    "location": ", ".join(
                        x for x in (location.get("city"), location.get("country")) if x),
                    "text": text, "url": f"https://jobs.smartrecruiters.com/{tenant}/{job_id}",
                    "updated_at": answer.get("releasedDate", "")}, ""


def workday_job(tenant, site, workday_host, external_path):
    """One Workday job in full, from the same call its careers page makes."""
    status, answer, error = fetch_json(
        f"https://{workday_host}/wday/cxs/{tenant}/{site}{external_path}")
    if not answer:
        return status, {}, error
    info = answer.get("jobPostingInfo") or {}
    return status, {"title": info.get("title", ""), "location": info.get("location", ""),
                    "text": _strip_tags(info.get("jobDescription", "")),
                    "url": info.get("externalUrl", ""),
                    "updated_at": info.get("postedOn", ""), "req_id": info.get("jobReqId", ""),
                    "start_date": info.get("startDate", ""),
                    "end_date": info.get("endDate", "")}, ""


def oracle_hcm_search(base, site_number, keyword, limit=25):
    """Oracle careers sites answer the same search their own pages call."""
    url = (f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
           f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site_number},"
           f"keyword={quote(keyword)},limit={limit},sortBy=POSTING_DATES_DESC")
    status, answer, error = fetch_json(url)
    if not answer:
        return status, [], error
    jobs = []
    for item in answer.get("items", []):
        for j in item.get("requisitionList", []):
            jobs.append({"title": j.get("Title", ""), "location": j.get("PrimaryLocation", ""),
                         "url": (f"{base}/hcmUI/CandidateExperience/en/sites/{site_number}"
                                 f"/job/{j.get('Id')}"),
                         "id": str(j.get("Id", "")), "updated_at": j.get("PostedDate", "")})
    return status, jobs, ""


JOB_READERS = {"greenhouse": greenhouse_job, "lever": lever_job, "ashby": ashby_job,
               "smartrecruiters": smartrecruiters_job}
