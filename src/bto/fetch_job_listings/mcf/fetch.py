"""Call MyCareersFuture search + detail endpoints for one MCF run.

Called by:
    fetch_job_listings.run (through the shared card-board collection loop)

Calls:
    fetch_job_listings.http_client

MCF is Singapore only. Search is a POST per page (100 results, pages from
p000); the full JD is a second GET per job. The change signal is the search
card's `metadata.updatedAt`, treated as an opaque string.

Main surface:
    adapter(market) -> CARD-BOARD ADAPTER consumed by the shared loop:
        .search_page(term, page, pause)  one validated search page
        .card_id / .card_signal          identity and change signal of a hit
        .fetch_detail(job_id, pause)     raw full-JD response bytes, validated

This module fetches and validates MCF responses only. Mapping payloads into
the Bronze table is mcf/parse.py; persistence is storage/databricks/write.py.
"""

import json
import logging
from dataclasses import dataclass

from .. import http_client
from . import searches

log = logging.getLogger(__name__)

BOARD = "mcf"
API = "https://api.mycareersfuture.gov.sg/v2"
PAGE_SIZE = 100
FIRST_PAGE = 0                       # MCF pages are numbered from p000
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}

# A broken API must fail loudly, never pass as an empty-but-healthy night.
CANARY_TERMS = frozenset({"developer", "software engineer"})
MIN_PLAUSIBLE_UNIQUE = 500     # full sweeps see ~6,800 unique; below = garbage
MAX_PLAUSIBLE_UNIQUE = 25000   # ~4x normal history; above = abnormal, stop safely


@dataclass
class SearchPage:
    raw: bytes          # verbatim response body, preserved as the raw artifact
    hits: list          # the page's result cards
    total: int          # the board's advertised total for the term
    has_next: bool


class McfAdapter:
    board = BOARD
    page_size = PAGE_SIZE
    first_page = FIRST_PAGE
    canary_terms = CANARY_TERMS
    min_plausible_unique = MIN_PLAUSIBLE_UNIQUE
    max_plausible_unique = MAX_PLAUSIBLE_UNIQUE

    def __init__(self, market):
        if market != "sg":
            raise ValueError(f"MCF is Singapore only, got market {market!r}")
        self.market = market
        self.terms = list(searches.ACTIVE_TERMS)

    def search_page(self, term, page, pause):
        raw = http_client.call(
            f"{API}/search?limit={PAGE_SIZE}&page={page}",
            {"sessionId": "", "search": term},
            headers=HEADERS, pause=pause)
        payload = json.loads(raw)
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("results"), list)
                or not isinstance(payload.get("total"), int)):
            raise ValueError(
                f"MCF response shape changed for {term!r} p{page}: {raw[:200]!r}")
        return SearchPage(
            raw=raw,
            hits=payload["results"],
            total=payload["total"],
            has_next="next" in (payload.get("_links") or {}),
        )

    @staticmethod
    def card_id(hit):
        return hit.get("uuid") or None

    @staticmethod
    def card_signal(hit):
        """MCF's change signal: the card's metadata.updatedAt, opaque."""
        return (hit.get("metadata") or {}).get("updatedAt")

    def fetch_detail(self, board_job_id, pause):
        """One full-JD fetch. Returns the exact uncompressed response body —
        the bytes the content_hash is taken over. Validated just enough to
        prove the response is this job's JSON detail, not an error page."""
        raw = http_client.call(f"{API}/jobs/{board_job_id}", headers=HEADERS,
                               pause=pause)
        detail = json.loads(raw)
        if not isinstance(detail, dict) or detail.get("uuid") != board_job_id:
            raise ValueError(
                f"MCF detail shape changed for {board_job_id}: {raw[:200]!r}")
        return raw


def adapter(market):
    return McfAdapter(market)
