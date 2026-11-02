"""Fictional operating limits. Copy to ignored limits.py and configure explicitly.

This example is never loaded automatically. Use provider-supported time windows.
"""

LIMITS = {
    "PEOPLE_SEARCHES_PER_DAY": 5,
    "APIFY_USD_PER_PEOPLE_SEARCH": 0.8,
    "APIFY_USD_FOR_LINKEDIN_POSTS": 0.08,
    "APIFY_MONTHLY_CAP_SHARE": 0.2,
    "PEOPLE_SEARCHES_IN_AVERAGE_COST": 6,
    "PEOPLE_SEARCH_COST_ESTIMATE_USD": 0.45,
    "HUNTER_HOLD_BACK_BELOW_CREDITS": 30,
    "HUNTER_CREDITS_PER_PEOPLE_SEARCH": 6,
    "FULL_LOOKUP_RECRUITERS": 1,
    "FULL_LOOKUP_HIRING_TEAM": 1,
    "WEB_SEARCHES_PER_LOOK_UP": 2,
    "LINKEDIN_POSTED_WITHIN": "month",
    "LINKEDIN_POSTS_SEARCHED": 6,
    "LINKEDIN_POSTS_READ": 2,
    "LINKEDIN_PEOPLE_PER_POST": 4,
}
