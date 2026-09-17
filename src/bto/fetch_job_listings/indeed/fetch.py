"""Run and read the Indeed Apify Actor for one indeed × market run.

Called by:
    fetch_job_listings.run

Calls:
    fetch_job_listings.apify_client

One Actor run per configured query, `postedWithinDays = 1`, no result cap
(an omitted count means "all available"). The Actor is validated by output
shape and pricing model — never by pinning a build id, which would break
every time the provider publishes.

Main surface:
    adapter(market)              per-market configuration + the query plan
    validate_actor(client)       identity/pricing preflight, returns summary
    run_query(client, ...)       one Actor run -> terminal evidence dict
    envelope(...)                the raw stored envelope for one Actor run
                                 (token redacted before it goes anywhere)

Row validation/parsing is indeed/parse.py; persistence is
storage/databricks/write.py; cost rows are storage/postgres/write.py.
"""

import json
import logging

from ..apify_client import actor_summary
from . import searches
from .markets import ACTOR_ID, ACTOR_NAME, EXPECTED_PRICING_MODEL, MARKETS, POSTED_WITHIN_DAYS

log = logging.getLogger(__name__)

BOARD = "indeed"


class IndeedAdapter:
    board = BOARD
    actor_id = ACTOR_ID
    actor_name = ACTOR_NAME

    def __init__(self, market):
        try:
            self.config = MARKETS[market]
        except KeyError:
            raise ValueError(f"no Indeed configuration for market {market!r}")
        self.market = market
        self.queries = list(searches.SEARCH_QUERIES)
        self.max_plausible_unique = self.config.max_plausible_unique

    def actor_input(self, query_text):
        """The proven Actor input for one query. Count is omitted: the
        published schema scrapes all available items when absent."""
        return {
            "country": self.config.indeed_country,
            "query": query_text,
            "location": self.config.country_name,
            "radiusKm": 25,
            "postedWithinDays": str(POSTED_WITHIN_DAYS),
        }

    def is_canary(self, query):
        return self.config.canary_term in query.atomic_terms


def adapter(market):
    return IndeedAdapter(market)


def validate_actor(client):
    """Prove the Actor is still the one we approved, before any spend:
    same id, same owner/name, still the monthly-rental pricing model.
    Output shape is validated per row as datasets arrive (parse.py)."""
    summary = actor_summary(client.actor(ACTOR_ID))
    if summary.get("id") != ACTOR_ID:
        raise ValueError("Apify Indeed Actor id changed")
    if summary.get("name") != ACTOR_NAME:
        raise ValueError(f"Apify Indeed Actor ownership/name changed: "
                         f"{summary.get('name')!r}")
    if summary.get("pricingModel") != EXPECTED_PRICING_MODEL:
        raise ValueError(f"Apify Indeed Actor pricing changed: "
                         f"{summary.get('pricingModel')!r} — inspect before running")
    return summary


def run_query(client, adapter, query, *, max_charge_usd, timeout_s):
    """One billed Actor run for one query. Returns apify_client.run_actor()
    evidence (terminal run metadata + the complete dataset)."""
    return client.run_actor(
        ACTOR_ID,
        adapter.actor_input(query.text),
        max_total_charge_usd=max_charge_usd,
        timeout_s=timeout_s,
    )


def envelope(adapter, query, actor_metadata, evidence, apify_token):
    """The raw stored envelope for one Actor run: actor input, run metadata
    and the complete dataset — everything needed to reproduce the run.
    Serialized canonically, with the API token redacted."""
    retained = {
        "provider": "apify",
        "board": adapter.board,
        "market": adapter.market,
        "term": query.text,
        "query_key": query.key,
        "atomic_terms": list(query.atomic_terms),
        "actor": actor_metadata,
        **evidence,
    }
    raw = json.dumps(retained, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    if apify_token:
        raw = raw.replace(apify_token.encode(), b"{redacted}")
    return raw
