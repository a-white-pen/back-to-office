"""Find GitHub, X, Instagram and personal sites with company evidence.

GitHub evidence comes from the profile's company or bio, or public company
organisation membership. Other accounts need a search result naming the
company. Without that evidence, show "same name, not confirmed"; GitHub also
requires a single exact-name match. Phone lookup reuses these web results.
"""

import html
import re
import urllib.parse

from ..research_job_listing import brave_search
from . import config
from .find_relevant_personnel.classify_found_persons import plain_letters
from .identify_company import registrable_domain, text_names_company
from .providers import github

RESULTS_PER_SEARCH = 10
# Path words on x.com and instagram.com that are pages, not accounts.
NOT_HANDLES = {"i", "search", "hashtag", "intent", "share", "home", "explore", "p", "reel",
               "reels", "stories", "status", "accounts", "about", "login"}
SOCIAL_HOSTS = {"x.com": "x", "twitter.com": "x", "instagram.com": "instagram"}
NOT_PERSONAL_HOSTS = ("linkedin.", "facebook.", "github.", "medium.", "youtube.", "wikipedia.",
                      "x.com", "twitter.", "instagram.", "tiktok.")


def search_the_web_for_person(name, company_name, *, searches=1):
    """Web-search answers about the person, from one search or, for a Look up
    click, up to config.WEB_SEARCHES_PER_LOOK_UP."""
    queries = [f'"{name}" "{company_name}"',
               f'"{name}" site:x.com OR site:twitter.com',
               f'"{name}" site:instagram.com',
               f'"{name}" "{company_name}" phone'][:max(1, searches)]
    # A refused search answers nothing; the rest of the lookup still runs.
    answers = []
    for query in queries:
        answers += brave_search.search(query, key_name=config.BRAVE_LOOK_UP_PERSON_API_KEY,
                                       count=RESULTS_PER_SEARCH)
    # Brave marks the matched words with <strong>, which would split a name.
    return [{**answer, "title": _plain_text(answer.get("title")),
             "description": _plain_text(answer.get("description"))} for answer in answers]


def find_online_accounts(name, company, pages, *, use_github=True):
    """`channels` entries for the person's accounts, attributed first.
    Raises github.GitHubUnavailable when GitHub refuses, so the caller can
    stop asking it for the rest of the search."""
    channels = []
    if use_github:
        channels += github_accounts(name, company)
    channels += accounts_on_pages(name, company.name, pages)
    return sorted(channels, key=lambda channel: not channel["attributed"])


def github_accounts(name, company):
    """At most one GitHub account for the person, attributed when its profile
    or organisation membership names the company."""
    # The GitHub organisation is guessed from the website's name: example for example.com.
    organisation = registrable_domain(company.website).split(".")[0] if company.website else ""
    exact = []
    for login in github.search_users_by_name(name):
        profile = github.user_profile(login)
        same_name = plain_letters(profile.get("name") or "") == plain_letters(name)
        evidence = _github_evidence(profile, company)
        # The membership check costs a read from GitHub's hourly allowance,
        # so it is made only for an account with exactly the person's name.
        if evidence is None and same_name and organisation and github.is_public_member(
                organisation, login):
            evidence = f"member of the {organisation} organisation"
        channel = {"kind": "github", "value": login, "url": f"https://github.com/{login}",
                   "attributed": evidence is not None, "evidence": evidence}
        if evidence is not None:
            return [channel]
        if same_name:
            exact.append(channel)
    return exact if len(exact) == 1 else []


def accounts_on_pages(name, company_name, pages):
    """X, Instagram and personal-site channels from web-search answers whose
    title names the person."""
    channels, seen = [], set()
    for page in pages:
        url = page.get("url") or ""
        title = page.get("title") or ""
        if plain_letters(name) not in plain_letters(title):
            continue
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        kind = SOCIAL_HOSTS.get(host)
        if kind:
            handle = (parts.path.strip("/").split("/") or [""])[0]
            if not handle or handle.lower() in NOT_HANDLES:
                continue
            value = f"@{handle}" if kind == "x" else handle
            address = f"https://{host}/{handle}"
        elif _looks_personal(host, name):
            kind, value, address = "personal_site", host, f"https://{host}"
        else:
            continue
        if address in seen:
            continue
        seen.add(address)
        attributed = text_names_company(f"{title} {page.get('description') or ''}", company_name)
        channels.append({
            "kind": kind, "value": value, "url": address, "attributed": attributed,
            "evidence": f"page names {company_name}" if attributed else None,
        })
    return channels


def _github_evidence(profile, company):
    """Why a GitHub profile is the person's at this company, or None."""
    stem = re.sub(r"[^a-z0-9]", "", plain_letters(company.name or ""))
    company_field = profile.get("company") or ""
    if stem and stem in re.sub(r"[^a-z0-9]", "", plain_letters(company_field)):
        return f"company {company_field.strip()}"
    if text_names_company(profile.get("bio") or "", company.name or ""):
        return f"bio names {company.name}"
    return None


def _plain_text(markup):
    """Brave's title or description without HTML tags or entities, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup or "")).split())


def _looks_personal(host, name):
    """Match hosts built from the person's first and last names."""
    if not host or any(blocked in host for blocked in NOT_PERSONAL_HOSTS):
        return False
    words = re.findall(r"[a-z]+", plain_letters(name))
    letters = re.sub(r"[^a-z]", "", host.split(".")[0])
    return len(words) >= 2 and words[0] in letters and words[-1] in letters
