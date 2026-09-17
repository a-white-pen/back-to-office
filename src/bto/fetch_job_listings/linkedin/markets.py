"""LinkedIn market/location + Actor configuration for the four markets.

Called by:
    fetch_job_listings.linkedin.fetch, fetch_job_listings.linkedin.parse

LinkedIn is collected through the cheap_scraper Apify Actor: one Actor run
covers all configured terms with a rolling-24-hour window (`r86400`) and
provider-side deduplication (`saveOnlyUniqueItems`). The Actor bills per
event (start + per dataset item); the expected event-price ceilings below
are verified before every run, and the run itself is hard-capped at the
market's charge ceiling.

"""

from dataclasses import dataclass

ACTOR_ID = "2rJKkhh7vjpX7pvjg"
ACTOR_NAME = "cheap_scraper/linkedin-job-scraper"
EXPECTED_PRICING_MODEL = "PAY_PER_EVENT"
RESULT_EVENT = "apify-default-dataset-item"
START_EVENT = "apify-actor-start"
MAX_RESULT_EVENT_PRICE_USD = 0.0007
MAX_START_EVENT_PRICE_USD = 0.005
PUBLISHED_AT_WINDOW = "r86400"        # rolling past 24 hours
ENRICH_COMPANY_DATA = True            # company fields ride along uncharged


@dataclass(frozen=True)
class LinkedinMarket:
    market: str
    location: str                 # the Actor's locations input
    location_tokens: tuple        # fallback market test: lowercase substrings
                                  # accepted as in-market when the jobUrl
                                  # yields no country subdomain
    max_total_charge_usd: float   # hard cap sent on the Actor start
    plausibility_floor: int       # fewer unique jobs than this is suspicious
    max_plausible_unique: int     # above this, abnormal volume — stop safely


MARKETS = {
    "sg": LinkedinMarket("sg", "Singapore", ("singapore",), 2.0, 500, 5000),
    "hk": LinkedinMarket("hk", "Hong Kong", ("hong kong",), 1.0, 50, 3000),
    "th": LinkedinMarket("th", "Thailand",
                         ("thailand", "bangkok metropolitan area", "pattaya"),
                         1.0, 30, 3000),
    "au": LinkedinMarket("au", "Australia", ("australia",), 1.0, 100, 8000),
}
