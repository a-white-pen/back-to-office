"""Indeed market + Actor configuration for all eight supported markets.

Called by:
    fetch_job_listings.indeed.fetch, fetch_job_listings.indeed.parse

Indeed is collected through the Curious Coder Apify Actor, which bills per
event: once to start a run and once per dataset item it returns. One Actor
run per configured query; the Actor input differs by market only in
country/location.

"""

from dataclasses import dataclass

ACTOR_ID = "qA8rz8tR61HdkfTBL"
ACTOR_NAME = "curious_coder/indeed-scraper"
EXPECTED_PRICING_MODEL = "PAY_PER_EVENT"
RESULT_EVENT = "apify-default-dataset-item"
START_EVENT = "apify-actor-start"
MAX_RESULT_EVENT_PRICE_USD = 0.0001
MAX_START_EVENT_PRICE_USD = 0.0001
POSTED_WITHIN_DAYS = 1


@dataclass(frozen=True)
class IndeedMarket:
    market: str
    country_code: str             # ISO code the rows carry (GB for uk)
    country_name: str             # the Actor's location input
    indeed_country: str           # the Actor's country input
    indeed_origin: str            # market site origin, for reference
    location_markers: tuple       # lowercase substrings accepted as in-market
    canary_term: str              # a query that can never legitimately be 0
    max_plausible_unique: int     # above this, the board is abnormal — stop safely


MARKETS = {
    "sg": IndeedMarket("sg", "SG", "Singapore", "sg", "https://sg.indeed.com",
                       ("singapore",), "solutions consultant", 8000),
    "hk": IndeedMarket("hk", "HK", "Hong Kong", "hk", "https://hk.indeed.com",
                       ("hong kong",), "solutions consultant", 8000),
    "th": IndeedMarket("th", "TH", "Thailand", "th", "https://th.indeed.com",
                       ("thailand",), "developer", 8000),
    "au": IndeedMarket("au", "AU", "Australia", "au", "https://au.indeed.com",
                       ("australia",), "solutions consultant", 8000),
    "nz": IndeedMarket("nz", "NZ", "New Zealand", "nz", "https://nz.indeed.com",
                       ("new zealand",), "solutions consultant", 8000),
    "uk": IndeedMarket("uk", "GB", "United Kingdom", "uk", "https://uk.indeed.com",
                       ("united kingdom", "england", "scotland", "wales",
                        "northern ireland"), "solutions consultant", 8000),
    "us": IndeedMarket("us", "US", "United States", "us", "https://www.indeed.com",
                       ("united states",), "solutions consultant", 8000),
    "ca": IndeedMarket("ca", "CA", "Canada", "ca", "https://ca.indeed.com",
                       ("canada",), "solutions consultant", 8000),
}
