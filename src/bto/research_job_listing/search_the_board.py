"""Find vacancy candidates through hiring-platform APIs, board pages and sitemaps.

These reads need no search-engine key. Candidates carry title and JD overlap
scores for ordering; prove_same_job still verifies every proposed vacancy.
"""

import html
import logging
import re
import urllib.parse

from ..log_errors import log_failure
from . import private_adapters
from .board_address import Board
from .fetch_page import fetch, fetch_json, host, registered_domain
from .hiring_systems import (
    JOB_LISTERS,
    JOB_READERS,
    is_aggregator,
    oracle_hcm_search,
    search_workday,
    workday_job,
)
from .prove_same_job import share_of_jd_on_page, title_words

log = logging.getLogger(__name__)

# The place name a board's location filter understands, by BTO market.
MARKET_NAMES = {"sg": "Singapore", "hk": "Hong Kong", "th": "Thailand", "au": "Australia",
                "nz": "New Zealand"}

# Words in an address slug that say nothing about which job it is.
_SLUG_NOISE = {"pte", "ltd", "limited", "inc", "llc", "plc", "group", "holdings", "asia", "pacific",
               "singapore", "hong", "kong", "thailand", "co", "company", "corporation", "corp",
               "international", "branch", "se", "sea", "the", "bank", "services", "technologies",
               "technology", "public", "operations", "far", "east", "trading", "expert",
               "regional", "global", "apac", "sdn", "bhd", "pty", "australia", "new", "zealand",
               "hk", "sg", "th", "and", "of", "&", "headquarters", "hq"}


# ── the vendor behind a careers page ──────────────────────────────────────

def boards_behind_careers_page(url, calls, depth=0):
    """The hiring-software boards behind a careers address, read from its HTML.

    A script-built page names its vendor only in its script bundle, so up to
    three bundles are read; a corporate homepage gets one hop along a careers
    link (`depth` stops it there).
    """
    boards = []
    calls["http"] += 1
    page = fetch(url, timeout=15)
    markup = page.html or ""
    if not markup:
        return boards
    page_host = host(page.final_url or url)
    base = f"https://{page_host}"
    for match in re.finditer(r"https?://([a-z0-9.-]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
                             r"([A-Za-z0-9_-]+)", markup):
        tenant = match.group(1).split(".")[0]
        boards.append(Board("workday", tenant,
                            {"tenant": tenant, "site": match.group(2), "host": match.group(1)}))
        break
    for match in re.finditer(r"https?://([a-z0-9-]+\.fa\.[a-z0-9.-]*oraclecloud\.com)", markup):
        site = re.search(r"CX_\d+", markup)
        boards.append(Board("oracle_hcm", f"https://{match.group(1)}",
                            {"site": site.group(0) if site else "CX_1"}))
        break
    for match in re.finditer(r"https?://[a-z0-9.-]*(boards|job-boards)\.greenhouse\.io/"
                             r"([A-Za-z0-9_-]+)", markup):
        boards.append(Board("greenhouse", match.group(2), {}))
        break
    for match in re.finditer(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)", markup):
        boards.append(Board("lever", match.group(1), {}))
        break
    for match in re.finditer(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", markup):
        boards.append(Board("ashby", match.group(1), {}))
        break
    for match in re.finditer(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
                             markup):
        boards.append(Board("smartrecruiters", match.group(1), {}))
        break
    lowered = markup.lower()
    if ("/services/rss/job" in markup or 'class="jobTitle-link"' in markup
            or "successfactors" in lowered or "jobs.sap.com" in lowered or "csb-jobs" in lowered):
        boards.append(Board("successfactors", base, {}))
    if "phenom" in lowered or "/api/apply/v2/jobs" in markup:
        boards.append(Board("phenom", base, {"domain": registered_domain(page_host)}))
    if "eightfold" in lowered:
        match = re.search(r"https?://([a-z0-9.-]+\.eightfold\.ai)", markup)
        if match:
            boards.append(Board("eightfold", f"https://{match.group(1)}", {}))
    boards.extend(private_adapters.discover_boards(markup, base))
    # A script-built careers site names its vendor only inside its script bundle: one hop into it.
    if not boards and depth == 0:
        for script in re.findall(r'<script[^>]+src=["\']?([^"\' >]+\.js[^"\' >]*)', markup)[:3]:
            calls["http"] += 1
            bundle = fetch(urllib.parse.urljoin(page.final_url or url, script),
                           timeout=15).html or ""
            boards.extend(private_adapters.discover_boards(bundle, base))
            match = re.search(r"https?://([a-z0-9.-]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
                              r"([A-Za-z0-9_-]+)", bundle)
            if match:
                tenant = match.group(1).split(".")[0]
                boards.append(Board("workday", tenant,
                                    {"tenant": tenant, "site": match.group(2),
                                     "host": match.group(1)}))
            if "/api/apply/v2/jobs" in bundle:
                boards.append(Board("phenom", base, {"domain": registered_domain(page_host)}))
            if boards:
                break
    # A corporate homepage: follow one careers link.
    if depth == 0 and not boards:
        links = re.findall(r'href=["\']([^"\']*(?:career|jobs|join-us|joinus|vacanc|opportunit)'
                           r'[^"\']*)["\']', markup, re.IGNORECASE)
        seen = set()
        for link in links[:6]:
            address = urllib.parse.urljoin(page.final_url or url, link)
            if address in seen or host(address).endswith(("linkedin.com", "facebook.com",
                                                          "instagram.com", "twitter.com",
                                                          "youtube.com")):
                continue
            seen.add(address)
            boards += boards_behind_careers_page(address, calls, depth=1)
            if boards:
                break
    return boards


# ── the vendors' own search ───────────────────────────────────────────────

def _search_successfactors(base, keyword, calls):
    calls["api"] += 1
    page = fetch(f"{base}/services/rss/job/?locale=en_US"
                 f"&keywords={urllib.parse.quote('(' + keyword + ')')}", timeout=15)
    found = []
    for item in re.findall(r"<item>(.*?)</item>", page.html or "", re.DOTALL):
        title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        description = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
        title_text = re.sub(r"^<!\[CDATA\[|\]\]>$", "",
                            html.unescape((title.group(1) if title else "").strip()))
        description_text = re.sub(r"^<!\[CDATA\[|\]\]>$", "",
                                  description.group(1) if description else "")
        found.append({"title": title_text, "location": "",
                      "url": html.unescape((link.group(1) if link else "").strip()).split("?")[0],
                      "text": re.sub(r"<[^>]+>", " ", html.unescape(description_text))})
    return found


def _search_phenom(base, domain, query, location, calls):
    calls["api"] += 1
    _, answer, _ = fetch_json(f"{base}/api/apply/v2/jobs?domain={domain}"
                              f"&query={urllib.parse.quote(query)}"
                              f"&location={urllib.parse.quote(location)}&start=0&num=10")
    return [{"title": p.get("name", ""), "location": p.get("location", ""),
             "url": p.get("canonicalPositionUrl") or p.get("positionUrl", ""), "text": ""}
            for p in ((answer or {}).get("positions") or [])]


def _search_eightfold(base, query, location, calls):
    calls["api"] += 1
    _, answer, _ = fetch_json(f"{base}/api/apply/v2/jobs"
                              f"?domain={host(base).split('.')[0]}.com"
                              f"&query={urllib.parse.quote(query)}"
                              f"&location={urllib.parse.quote(location)}&start=0&num=10",
                              headers={"Referer": base + "/careers",
                                       "Accept": "application/json, text/plain, */*"})
    return [{"title": p.get("name", ""), "location": p.get("location", ""),
             "url": p.get("canonicalPositionUrl") or f"{base}/careers/job/{p.get('id')}",
             "text": ""}
            for p in ((answer or {}).get("positions") or [])]


def title_similarity(a, b):
    """The share of title words two titles have in common, 0 to 1."""
    words_a, words_b = set(title_words(a)), set(title_words(b))
    return len(words_a & words_b) / len(words_a | words_b) if words_a and words_b else 0.0


def search_board(board, listing, calls):
    """Candidates from one board for this listing, found by its title.

    Each candidate carries title, location, url, text, query,
    title_similarity, containment and board.
    """
    candidates = []
    market_name = MARKET_NAMES.get(listing.market, listing.market)
    query = listing.title[:60]
    try:
        if board.vendor == "workday":
            calls["api"] += 1
            _, jobs, _ = search_workday(board.extra["tenant"], board.extra["site"],
                                        board.extra["host"], query)
            batch = [{**j, "text": "", "query": query} for j in jobs[:20]]
            ranked = sorted(batch, key=lambda c: -title_similarity(listing.title, c["title"]))[:6]
            site_marker = "/" + board.extra["site"]
            for candidate in ranked:
                path = (candidate["url"].split(site_marker, 1)[1]
                        if site_marker in candidate["url"] else "")
                if path:
                    calls["api"] += 1
                    _, job, _ = workday_job(board.extra["tenant"], board.extra["site"],
                                            board.extra["host"], path)
                    candidate["text"] = job.get("text", "")
            candidates = ranked
        elif board.vendor == "successfactors":
            candidates = [{**item, "query": query}
                          for item in _search_successfactors(board.tenant, query, calls)]
        elif board.vendor == "oracle_hcm":
            site = board.extra.get("site", "CX_1")
            calls["api"] += 1
            _, jobs, _ = oracle_hcm_search(board.tenant, site, query)
            candidates += [{**j, "text": "", "query": query} for j in jobs]
            # Oracle's keyword finder is loose; page through the market's jobs
            # unless a near-exact title is already in hand.
            if not any(title_similarity(listing.title, c["title"]) >= 0.8 for c in candidates):
                for offset in range(0, 250, 25):
                    calls["api"] += 1
                    _, answer, _ = fetch_json(
                        f"{board.tenant}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                        "?onlyData=true&expand=requisitionList.secondaryLocations"
                        f"&finder=findReqs;siteNumber={site},"
                        f"location={urllib.parse.quote(market_name)},limit=25,offset={offset},"
                        "sortBy=POSTING_DATES_DESC")
                    jobs = [j for item in (answer or {}).get("items", [])
                            for j in item.get("requisitionList", [])]
                    if not jobs:
                        break
                    candidates += [
                        {"title": j.get("Title", ""), "location": j.get("PrimaryLocation", ""),
                         "url": (f"{board.tenant}/hcmUI/CandidateExperience/en/sites/{site}"
                                 f"/job/{j.get('Id')}"),
                         "text": "", "query": "location-page"}
                        for j in jobs]
                    if any(title_similarity(listing.title, c["title"]) >= 0.8 for c in candidates):
                        break
            closest = sorted(candidates, key=lambda c: -title_similarity(listing.title, c["title"]))
            for candidate in closest[:3]:
                job_id = candidate["url"].rsplit("/", 1)[-1]
                calls["api"] += 1
                _, answer, _ = fetch_json(
                    f"{board.tenant}/hcmRestApi/resources/latest"
                    "/recruitingCEJobRequisitionDetails?expand=all"
                    f"&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={site}")
                item = ((answer or {}).get("items") or [{}])[0]
                description = html.unescape(str(item.get("ExternalDescriptionStr", "")))
                candidate["text"] = re.sub(r"<[^>]+>", " ", description)
        elif board.vendor in JOB_LISTERS:
            calls["api"] += 1
            _, jobs, _ = JOB_LISTERS[board.vendor](board.tenant)
            ranked = sorted(jobs, key=lambda j: -title_similarity(listing.title, j["title"]))[:5]
            for job in ranked:
                if title_similarity(listing.title, job["title"]) < 0.34:
                    continue
                calls["api"] += 1
                _, detail, _ = JOB_READERS[board.vendor](board.tenant, job["id"])
                candidates.append({**job, "text": detail.get("text", ""), "query": "list"})
        elif board.vendor == "phenom":
            candidates = [{**p, "query": query}
                          for p in _search_phenom(board.tenant, board.extra.get("domain", ""),
                                                  query, market_name, calls)]
        elif board.vendor == "eightfold":
            candidates = [{**p, "query": query}
                          for p in _search_eightfold(board.tenant, query, market_name, calls)]
        else:
            candidates = private_adapters.search_board(board, listing, calls) or []
    except Exception as error:
        # Keep candidates already read and let the other sources continue.
        log_failure(log, "Board search failed board=%s", board.label(),
                    error=error, level=logging.WARNING)
    for candidate in candidates:
        candidate["title_similarity"] = round(
            title_similarity(listing.title, candidate["title"]), 2)
        candidate["containment"] = (
            round(share_of_jd_on_page(listing.jd_text, candidate.get("text", "")), 2)
            if candidate.get("text") else None)
        candidate["board"] = board.label()
    return candidates


# ── the board's own search page and sitemap, for hosts with no known API ──

_SEARCH_PATTERNS = ("/search-jobs?k={q}&l={loc}", "/jobs?search={q}",
                    "/search/?q={q}&locationsearch={loc}", "/jobs/?search={q}&country={loc}",
                    "/en/jobs?search={q}", "/job-search?keywords={q}&location={loc}",
                    "/en-us/job-search?search={q}", "/search?q={q}",
                    "/jobs/search?keyword={q}&location={loc}", "/careers/search?q={q}")


def search_careers_site(base, listing, calls, max_patterns=6):
    """Try the common search-page addresses on a careers host and return the
    job links whose link text resembles the title. Candidates carry no text;
    the caller reads and checks each page."""
    market_name = MARKET_NAMES.get(listing.market, listing.market)
    query = urllib.parse.quote(listing.title[:60])
    location = urllib.parse.quote(market_name)
    found, seen = [], set()
    for pattern in _SEARCH_PATTERNS[:max_patterns]:
        calls["http"] += 1
        page = fetch(base + pattern.format(q=query, loc=location), timeout=15)
        if not page.html or page.status != 200:
            continue
        for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page.html,
                                 re.DOTALL | re.IGNORECASE):
            href, text = match.group(1), re.sub(r"<[^>]+>|\s+", " ", match.group(2)).strip()
            if not re.search(r"/job[s]?/|/position|/vacanc|/career[s]?/[^/]*\d", href,
                             re.IGNORECASE):
                continue
            address = urllib.parse.urljoin(page.final_url or base, href)
            if address in seen or is_aggregator(address):
                continue
            if title_similarity(listing.title, text) >= 0.5:
                seen.add(address)
                found.append({"title": text[:120], "location": "", "url": address, "text": "",
                              "query": pattern.split("?")[0]})
        if found:
            break
    if not found:
        # A careers site that answers the search address with a category
        # index rather than jobs: SuccessFactors sites hang every job off
        # /go/<category>/<id>/. One hop into the categories, then the same
        # link rule.
        calls["http"] += 1
        page = fetch(base + "/search/", timeout=15)
        categories = [urllib.parse.urljoin(page.final_url or base, href)
                      for href in re.findall(r'href=["\']([^"\']*/(?:go|category|categories|jobs)/'
                                             r'[^"\']*)["\']', page.html or "", re.IGNORECASE)]
        for category in list(dict.fromkeys(categories))[:4]:
            calls["http"] += 1
            category_page = fetch(category, timeout=15)
            for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                     category_page.html or "", re.DOTALL | re.IGNORECASE):
                href, text = match.group(1), re.sub(r"<[^>]+>|\s+", " ", match.group(2)).strip()
                if not re.search(r"/job/", href, re.IGNORECASE):
                    continue
                address = urllib.parse.urljoin(category_page.final_url or category, href)
                if address in seen or is_aggregator(address):
                    continue
                if title_similarity(listing.title, text) >= 0.5:
                    seen.add(address)
                    found.append({"title": text[:120], "location": "", "url": address, "text": "",
                                  "query": "category"})
            if found:
                break
    return found


def _slug_words(slug):
    return {w for w in re.findall(r"[a-z0-9]+", urllib.parse.unquote(slug).lower())
            if len(w) > 2 and w not in _SLUG_NOISE}


def sitemap_candidates(base, listing, calls, max_children=8):
    """Job addresses from the careers site's sitemap whose slug matches the title.

    Careers sites publish one for search engines, so it is the cheapest
    complete list of such a site and, unlike its search box, it is static.
    Bounded: robots.txt, the index, and at most `max_children` child sitemaps,
    English ones first.
    """
    calls["http"] += 1
    robots = fetch(base + "/robots.txt", timeout=10)
    sitemaps = (re.findall(r"(?i)sitemap:\s*(\S+)", robots.text or robots.html or "")
                or [base + "/sitemap.xml"])
    wanted = _slug_words(listing.title)
    scored, seen, fetched = [], set(), 0
    queue = list(sitemaps[:2])
    while queue and fetched < max_children + 1:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        calls["http"] += 1
        fetched += 1
        xml = fetch(sitemap, timeout=20).html or ""
        locations = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
        children = [loc for loc in locations if loc.endswith(".xml") or ".xml?" in loc]
        if children:
            children.sort(key=lambda loc: (not re.search(r"(?i)job", loc),
                                           not re.search(r"(?i)[-_/]en[-_.]", loc)))
            queue += children[:max_children]
            continue
        for address in locations:
            path = urllib.parse.urlsplit(address).path.rstrip("/")
            segments = [p for p in path.split("/") if p]
            if len(segments) < 2:
                continue
            slug = max(segments, key=len)                 # the title slug is the longest segment
            slug_words = _slug_words(slug.replace("-", " ").replace("_", " "))
            overlap = (len(slug_words & wanted) / len(slug_words | wanted)
                       if slug_words | wanted else 0.0)
            if overlap >= 0.6:
                scored.append((overlap, address,
                               urllib.parse.unquote(slug).replace("-", " ").replace("_", " ")))
    scored.sort(reverse=True)
    found, addresses = [], set()
    for overlap, address, title in scored[:5]:
        if address not in addresses:
            addresses.add(address)
            found.append({"title": title, "location": "", "url": address, "text": "",
                          "query": f"sitemap:{overlap:.2f}"})
    return found
