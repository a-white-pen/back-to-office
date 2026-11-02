"""Propose employer-owned careers pages from up to two fixed Brave searches.

The second query runs only when the first gives no candidates. The board
check in run separately decides whether a candidate is a usable job board.
"""

from . import brave_search
from .check_employer_board import employer_controls
from .fetch_page import host
from .guess_employer_website import careers_addresses
from .hiring_systems import is_aggregator, is_reposting_address
from .search_the_board import MARKET_NAMES

# Job-listing research's own Brave key. websearch_for_vacancy uses it too.
KEY_NAME = "BRAVE_FIND_DIRECT_LINK_API_KEY"


def board_queries(listing):
    """The two queries for where this employer publishes its jobs;
    board_candidates sends the second only when the first finds nothing."""
    if not listing.employer:
        return []
    market_name = MARKET_NAMES.get(listing.market, "")
    return [f"{listing.employer} careers job openings {market_name}".strip(),
            f'"{listing.employer}" current job vacancies apply']


def board_candidates(listing, calls, refusals):
    """Addresses on hosts this employer controls that Brave links to its
    careers, and the usual careers addresses on those hosts.

    Skipped when the key is not configured. A Brave refusal is appended to
    `refusals` and counts as finding nothing.
    """
    if not brave_search.is_configured(KEY_NAME):
        return []
    hits, hosts = [], []
    for query in board_queries(listing):
        for row in brave_search.search(query, key_name=KEY_NAME, count=20, refusals=refusals):
            url = row.get("url") or ""
            if not url or is_reposting_address(url) or is_aggregator(url):
                continue
            if not employer_controls(url, listing.employer, listing.company_website or ""):
                continue
            hits.append(url)
            if host(url) not in hosts:
                hosts.append(host(url))
        calls["search"] += 1
        if hits:
            break
    # Try every returned address before expanding common careers paths,
    # so the first host cannot consume the whole candidate budget.
    candidates = list(dict.fromkeys(hits))
    candidates += [f"https://{employer_host}" for employer_host in hosts
                   if f"https://{employer_host}" not in candidates]
    for employer_host in hosts:
        candidates += [address for address in careers_addresses(f"https://{employer_host}", 3)
                       if address not in candidates]
    return candidates
