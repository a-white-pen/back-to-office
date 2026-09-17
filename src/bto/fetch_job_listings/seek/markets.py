"""SEEK-platform market configuration: AU/NZ/SG/HK/TH source parameters.

Called by:
    fetch_job_listings.seek.fetch

One platform, five markets, three brands: SEEK (AU, NZ), JobStreet (SG) and
JobsDB (HK, TH). Only the site configuration differs — base URL, sitekey,
GraphQL zone, plausibility guards, and (AU only) state partitions used when a
term's result count exceeds what one query can page through.

"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeekMarket:
    board: str                    # jobstreet · jobsdb · seek
    market: str                   # sg · hk · th · au · nz
    base_url: str
    sitekey: str
    zone: str                     # GraphQL zone for companyProfile/currency
    canary_terms: frozenset       # terms that can never legitimately return 0
    min_plausible_unique: int     # below this, a full sweep is garbage
    max_plausible_unique: int     # above this, the board is abnormal — stop safely
    search_partitions: tuple = field(default=())
    max_results_per_query: int | None = None


MARKETS = {
    ("jobstreet", "sg"): SeekMarket(
        board="jobstreet", market="sg",
        base_url="https://sg.jobstreet.com", sitekey="sg", zone="asia-6",
        canary_terms=frozenset({"solutions consultant", "software engineer"}),
        min_plausible_unique=3000, max_plausible_unique=30000,
    ),
    ("jobsdb", "hk"): SeekMarket(
        board="jobsdb", market="hk",
        base_url="https://hk.jobsdb.com", sitekey="hk", zone="asia-1",
        canary_terms=frozenset({"business analyst", "software engineer"}),
        min_plausible_unique=1300, max_plausible_unique=15000,
    ),
    ("jobsdb", "th"): SeekMarket(
        board="jobsdb", market="th",
        base_url="https://th.jobsdb.com", sitekey="th", zone="asia-3",
        canary_terms=frozenset({"business analyst", "software engineer"}),
        min_plausible_unique=1000, max_plausible_unique=12000,
    ),
    ("seek", "au"): SeekMarket(
        board="seek", market="au",
        base_url="https://au.seek.com", sitekey="au", zone="anz-1",
        canary_terms=frozenset({"business analyst", "software engineer"}),
        min_plausible_unique=2500, max_plausible_unique=30000,
        # AU is large enough that one query can exceed the platform's paging
        # ceiling; broad terms are then re-swept per state.
        search_partitions=(
            "New South Wales NSW", "Victoria VIC", "Queensland QLD",
            "Western Australia WA", "South Australia SA",
            "Australian Capital Territory ACT", "Tasmania TAS",
            "Northern Territory NT",
        ),
        max_results_per_query=500,
    ),
    ("seek", "nz"): SeekMarket(
        board="seek", market="nz",
        base_url="https://nz.seek.com", sitekey="nz", zone="anz-2",
        canary_terms=frozenset({"business analyst", "software engineer"}),
        min_plausible_unique=400, max_plausible_unique=8000,
    ),
}
