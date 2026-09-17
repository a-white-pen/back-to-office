"""Call SEEK / JobStreet / JobsDB search + detail endpoints for one run.

Called by:
    fetch_job_listings.run (through the shared card-board collection loop)

Calls:
    fetch_job_listings.http_client

One platform serves all five markets: a JSON search API (100 results a page,
pages from p001, keywords sent as an exact quoted phrase to suppress fuzzy
expansion) and a GraphQL detail endpoint (one POST per job, zone-scoped).

The change signal is `{listingDate}|{sha256 of 14 stable card fields}` — the
recipe is the data dictionary's contract; do not substitute another.

Main surface:
    adapter(board, market) -> CARD-BOARD ADAPTER for the shared loop
        .search_page(term, page, pause[, partition])
        .card_id / .card_signal
        .fetch_detail(job_id, pause)
        .partitions_for(total)     AU-only re-sweep by state for oversize terms

Mapping payloads into Bronze is seek/parse.py; persistence is
storage/databricks/write.py.
"""

import hashlib
import json
import logging
import urllib.parse
from dataclasses import dataclass

from .. import http_client
from . import searches
from .markets import MARKETS

log = logging.getLogger(__name__)

PAGE_SIZE = 100
FIRST_PAGE = 1                       # SEEK-family pages are numbered from p001
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}

# The 14 stable search-card fields inside the change-signal fingerprint.
# Contract order and membership — see the data dictionary before touching.
CARD_SIGNAL_FIELDS = (
    "listingDate", "title", "advertiser", "companyName", "salaryLabel",
    "locations", "classifications", "teaser", "workTypes",
    "workArrangements", "bulletPoints", "tags", "roleId", "displayType",
)


def card_change_signal(hit):
    """`{listingDate}|{sha256}` over the canonicalised 14-field card subset.
    An absent field participates as JSON null, so every fingerprint covers
    all fourteen keys."""
    stable = {field: hit.get(field) for field in CARD_SIGNAL_FIELDS}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    return f"{hit['listingDate']}|{hashlib.sha256(encoded).hexdigest()}"


def build_detail_query(zone):
    """The proven wide GraphQL selection for one market zone."""
    return ("""
query JobDetailsForScraper($id: ID!) {
  jobDetails(id: $id) {
    companyProfile(zone: "%(zone)s") { id name }
    gfjInfo { location { countryCode } workTypes { label } }
    job {
      id
      title
      content(platform: WEB)
      status
      isExpired
      isVerified
      sourceZone
      listedAt { dateTimeUtc }
      expiresAt { dateTimeUtc }
      abstract
      phoneNumber
      salary { label currencyLabel(zone: "%(zone)s") }
      workTypes { label }
      advertiser { id name }
      location { label }
      classifications { label(languageCode: "en") }
      contactMatches { type value }
      products { questionnaire { questions } bullets }
    }
  }
}
""" % {"zone": zone}).strip()


@dataclass
class SearchPage:
    raw: bytes
    hits: list
    total: int
    has_next: bool


class SeekAdapter:
    page_size = PAGE_SIZE
    first_page = FIRST_PAGE

    def __init__(self, board, market):
        try:
            config = MARKETS[(board, market)]
        except KeyError:
            raise ValueError(f"no SEEK-family configuration for {board} × {market}")
        self.board = board
        self.market = market
        self.config = config
        self.terms = searches.terms_for(board, market)
        self.canary_terms = config.canary_terms
        self.min_plausible_unique = config.min_plausible_unique
        self.max_plausible_unique = config.max_plausible_unique
        self.detail_api = f"{config.base_url}/graphql"
        self.detail_query = build_detail_query(config.zone)

    def search_url(self, term, page, partition=None):
        params = {
            "sitekey": self.config.sitekey,
            "sourcesystem": "houston",
            # exact quoted phrase: suppresses the platform's fuzzy expansion
            "keywords": f'"{term}"',
            "page": page,
            "pageSize": PAGE_SIZE,
        }
        if partition is not None:
            params["where"] = partition
        return (f"{self.config.base_url}/api/jobsearch/v5/search?"
                + urllib.parse.urlencode(params))

    def search_page(self, term, page, pause, partition=None):
        raw = http_client.call(self.search_url(term, page, partition),
                               headers=HEADERS, pause=pause)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"{self.board}_{self.market} search returned "
                             f"invalid JSON for {term!r} p{page}") from error
        metadata = payload.get("solMetadata") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("data"), list)
                or not isinstance(payload.get("totalCount"), int)
                or not isinstance(metadata, dict)
                or metadata.get("pageSize") != PAGE_SIZE
                or metadata.get("pageNumber") != page):
            raise ValueError(
                f"{self.board}_{self.market} response shape changed for "
                f"{term!r} p{page}: {raw[:200]!r}")
        for hit in payload["data"]:
            if (not isinstance(hit, dict) or not hit.get("id")
                    or not isinstance(hit.get("listingDate"), str)):
                raise ValueError(
                    f"{self.board}_{self.market} hit shape changed for "
                    f"{term!r} p{page}: {str(hit)[:200]}")
        return SearchPage(
            raw=raw,
            hits=payload["data"],
            total=payload["totalCount"],
            has_next=len(payload["data"]) == PAGE_SIZE,
        )

    @staticmethod
    def card_id(hit):
        return str(hit["id"]) if hit.get("id") else None

    @staticmethod
    def card_signal(hit):
        return card_change_signal(hit)

    def partitions_for(self, total):
        """Partitions to re-sweep a term with when its advertised total
        exceeds the platform's per-query ceiling; empty = no re-sweep."""
        if (self.config.search_partitions
                and self.config.max_results_per_query is not None
                and total >= self.config.max_results_per_query):
            return self.config.search_partitions
        return ()

    def fetch_detail(self, board_job_id, pause):
        """One GraphQL full-JD fetch. Returns the exact uncompressed response
        body — the bytes the content_hash is taken over — after proving the
        response really is this job's detail."""
        raw = http_client.call(
            self.detail_api,
            {
                "operationName": "JobDetailsForScraper",
                "variables": {"id": board_job_id},
                "query": self.detail_query,
            },
            headers=HEADERS, pause=pause)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"{self.board}_{self.market} detail returned "
                             f"invalid JSON for {board_job_id}") from error
        job = None
        if isinstance(payload, dict) and not payload.get("errors"):
            job = ((payload.get("data") or {}).get("jobDetails") or {}).get("job")
        if (not isinstance(job, dict)
                or str(job.get("id")) != str(board_job_id)
                or not isinstance(job.get("title"), str)
                or not isinstance(job.get("content"), str)):
            raise ValueError(
                f"{self.board}_{self.market} detail shape changed for "
                f"{board_job_id}: {raw[:200]!r}")
        return raw


def adapter(board, market):
    return SeekAdapter(board, market)
