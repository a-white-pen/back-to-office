"""Run and read the LinkedIn Apify Actor for one linkedin × market run.

Called by:
    fetch_job_listings.run

Calls:
    fetch_job_listings.apify_client

One Actor run covers every configured term with the rolling-24-hour
window (`r86400`) and `saveOnlyUniqueItems`, so each job id comes back at
most once. Before the (paid) start, preflight verifies the Actor is still
the approved one — identity, PAY_PER_EVENT pricing, and start/result event
prices at or below the approved ceilings — and snapshots the account's
remaining credit. The output is validated by row shape, never by pinning a
build id.

Main surface:
    adapter(market)                     per-market configuration
    validate_actor(client)              identity + event-price preflight
    run_all_terms(client, adapter, ...) the one Actor run -> evidence dict
    envelope(...)                       the raw stored envelope (token redacted)

Row validation/parsing is linkedin/parse.py; persistence is
storage/databricks/write.py; the cost row is storage/postgres/write.py.
"""

import json
import logging

from ..apify_client import active_pricing, actor_summary
from . import searches
from .markets import (
    ACTOR_ID,
    ACTOR_NAME,
    ENRICH_COMPANY_DATA,
    EXPECTED_PRICING_MODEL,
    MARKETS,
    MAX_RESULT_EVENT_PRICE_USD,
    MAX_START_EVENT_PRICE_USD,
    PUBLISHED_AT_WINDOW,
    RESULT_EVENT,
    START_EVENT,
)

log = logging.getLogger(__name__)

BOARD = "linkedin"


class LinkedinAdapter:
    board = BOARD
    actor_id = ACTOR_ID
    actor_name = ACTOR_NAME

    def __init__(self, market):
        try:
            self.config = MARKETS[market]
        except KeyError:
            raise ValueError(f"no LinkedIn configuration for market {market!r}")
        self.market = market
        self.terms = list(searches.ACTIVE_TERMS)
        self.max_plausible_unique = self.config.max_plausible_unique

    def actor_input(self):
        """The proven one-run Actor input: every term, one location, rolling
        24 hours, provider-side unique ids."""
        return {
            "keyword": list(self.terms),
            "locations": [self.config.location],
            "publishedAt": PUBLISHED_AT_WINDOW,
            "saveOnlyUniqueItems": True,
            "enrichCompanyData": ENRICH_COMPANY_DATA,
        }


def adapter(market):
    return LinkedinAdapter(market)


def validate_actor(client):
    """Prove the Actor is still the approved one before the paid start:
    same id and owner, still PAY_PER_EVENT, and both charged events priced
    at or below the approved ceilings. Returns {"actor": …, "pricing": …}."""
    actor = client.actor(ACTOR_ID)
    summary = actor_summary(actor)
    if summary.get("id") != ACTOR_ID:
        raise ValueError("Apify LinkedIn Actor id changed")
    if summary.get("name") != ACTOR_NAME:
        raise ValueError(f"Apify LinkedIn Actor ownership/name changed: "
                         f"{summary.get('name')!r}")

    pricing = active_pricing(actor)
    if pricing.get("pricingModel") != EXPECTED_PRICING_MODEL:
        raise ValueError(f"Apify LinkedIn Actor pricing model changed to "
                         f"{pricing.get('pricingModel')!r}")
    events = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    result = events.get(RESULT_EVENT) or {}
    start = events.get(START_EVENT) or {}
    tiers = result.get("eventTieredPricingUsd") or {}
    result_prices = sorted({
        float(info["tieredEventPriceUsd"])
        for info in tiers.values()
        if isinstance(info, dict)
        and isinstance(info.get("tieredEventPriceUsd"), (int, float))
    })
    start_price = start.get("eventPriceUsd")
    if not result_prices:
        raise ValueError("Apify LinkedIn Actor result-event prices are missing")
    if max(result_prices) > MAX_RESULT_EVENT_PRICE_USD + 1e-12:
        raise ValueError(f"Apify LinkedIn Actor result price rose to "
                         f"USD {max(result_prices):.8f}")
    if (not isinstance(start_price, (int, float))
            or float(start_price) > MAX_START_EVENT_PRICE_USD + 1e-12):
        raise ValueError(f"Apify LinkedIn Actor start-event price changed to "
                         f"{start_price!r}")
    return {
        "actor": summary,
        "pricing": {
            "pricingModel": pricing.get("pricingModel"),
            "pricingStartedAt": pricing.get("startedAt"),
            "resultEvent": RESULT_EVENT,
            "resultTierPricesUsd": result_prices,
            "startEvent": START_EVENT,
            "startEventPriceUsd": float(start_price),
        },
    }


def run_all_terms(client, adapter, *, timeout_s):
    """The one billed Actor run covering every configured term. The charge
    is hard-capped at the market's approved ceiling on the Actor start."""
    return client.run_actor(
        ACTOR_ID,
        adapter.actor_input(),
        max_total_charge_usd=adapter.config.max_total_charge_usd,
        timeout_s=timeout_s,
    )


def envelope(adapter, preflight_evidence, evidence, apify_token):
    """The raw stored envelope for the run: terms, actor identity, pricing,
    run metadata and the complete dataset. Serialized canonically, with the
    API token redacted."""
    retained = {
        "provider": "apify",
        "board": adapter.board,
        "market": adapter.market,
        "terms": list(adapter.terms),
        "actor": preflight_evidence["actor"],
        "pricing": preflight_evidence["pricing"],
        **evidence,
    }
    raw = json.dumps(retained, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    if apify_token:
        raw = raw.replace(apify_token.encode(), b"{redacted}")
    return raw
