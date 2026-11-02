"""Propose vacancy pages from Brave searches restricted to the board's host.

Try a printed requisition number first, then the title. The vacancy check in
run reads each candidate because a search index can be stale.
"""

import re
import urllib.parse

from . import brave_search
from .fetch_page import host
from .hiring_systems import is_aggregator, is_reposting_address
from .prove_same_job import requisition_ids
from .websearch_for_employer_board import KEY_NAME

MAX_CANDIDATES = 6
_JOB_PATH = re.compile(r"(?i)/job|/career|/position|/vacanc|/opening|/requisition|-\d{4,}")


def vacancy_queries(listing, board_host, requisitions=()):
    """Where is this job on that board? The requisition number first when there is one."""
    queries = []
    if board_host and requisitions:
        queries.append(f"site:{board_host} {requisitions[0]}")
    if board_host:
        queries.append(f"site:{board_host} {listing.title[:58]}")
    return queries


def vacancy_candidates(listing, board, calls, refusals):
    """Job addresses on the board's host that Brave returns for this listing.

    Skipped when the key is not configured. A Brave refusal is appended to
    `refusals` and counts as finding nothing.
    """
    if not brave_search.is_configured(KEY_NAME):
        return []
    candidates = []
    for query in vacancy_queries(listing, host(board), requisition_ids(listing)):
        for row in brave_search.search(query, key_name=KEY_NAME, count=10, refusals=refusals):
            url = row.get("url") or ""
            if not url or is_reposting_address(url) or is_aggregator(url):
                continue
            if not _JOB_PATH.search(urllib.parse.urlsplit(url).path):
                continue
            if url not in candidates:
                candidates.append(url)
        calls["search"] += 1
        if candidates:
            break
    return candidates[:MAX_CANDIDATES]
