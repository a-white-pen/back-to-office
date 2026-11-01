"""Read private contact-search limits and public provider key names.

Read keys from the environment first, then the local private .env file.
"""

import os

from .. import settings
from .operating_limits import LIMIT_NAMES, load_limits


def __getattr__(name):
    """Keep the existing constant interface while loading private limits only on use."""
    if name in LIMIT_NAMES:
        return load_limits()[name]
    raise AttributeError(name)


# Provider key names are interfaces, not credentials.
APIFY_TOKEN = "APIFY_TOKEN"
HUNTER_API_KEY = "HUNTER_API_KEY"
BRAVE_FIND_EMPLOYER_API_KEY = "BRAVE_FIND_EMPLOYER_API_KEY"
BRAVE_FIND_PEOPLE_API_KEY = "BRAVE_FIND_PEOPLE_API_KEY"
BRAVE_LOOK_UP_PERSON_API_KEY = "BRAVE_LOOK_UP_PERSON_API_KEY"

# LinkedIn location filters and the place words used in web searches.
MARKET_LOCATIONS = {
    "sg": ["Singapore"],
    "hk": ["Hong Kong"],
    "th": ["Thailand"],
    "au": ["Australia"],
    "nz": ["New Zealand"],
}


def read_key(name):
    """The value of one key from the environment or .env, or "" when it is not configured."""
    return os.environ.get(name) or settings._dotenv().get(name) or ""


def place_name(market):
    """The market's main place name, such as "Singapore", or "" when unknown."""
    return (MARKET_LOCATIONS.get((market or "").lower()) or [""])[0]
