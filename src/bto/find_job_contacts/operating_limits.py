"""Validate private contact-search limits before admitting provider work."""

import ast
import math
from functools import lru_cache
from pathlib import Path

LIMIT_NAMES = frozenset(
    {
        "PEOPLE_SEARCHES_PER_DAY",
        "APIFY_USD_PER_PEOPLE_SEARCH",
        "APIFY_USD_FOR_LINKEDIN_POSTS",
        "APIFY_MONTHLY_CAP_SHARE",
        "PEOPLE_SEARCHES_IN_AVERAGE_COST",
        "PEOPLE_SEARCH_COST_ESTIMATE_USD",
        "HUNTER_HOLD_BACK_BELOW_CREDITS",
        "HUNTER_CREDITS_PER_PEOPLE_SEARCH",
        "FULL_LOOKUP_RECRUITERS",
        "FULL_LOOKUP_HIRING_TEAM",
        "WEB_SEARCHES_PER_LOOK_UP",
        "LINKEDIN_POSTED_WITHIN",
        "LINKEDIN_POSTS_SEARCHED",
        "LINKEDIN_POSTS_READ",
        "LINKEDIN_PEOPLE_PER_POST",
    }
)


def validate_limits(values):
    """Reject incomplete, non-finite or inconsistent operating limits without logging values."""
    if not isinstance(values, dict) or set(values) != LIMIT_NAMES:
        raise ValueError("Private contact limits have missing or unknown fields")
    nonnegative_integers = {
        "PEOPLE_SEARCHES_PER_DAY",
        "FULL_LOOKUP_RECRUITERS",
        "FULL_LOOKUP_HIRING_TEAM",
    }
    positive_integers = {
        "PEOPLE_SEARCHES_IN_AVERAGE_COST",
        "WEB_SEARCHES_PER_LOOK_UP",
        "LINKEDIN_POSTS_SEARCHED",
        "LINKEDIN_POSTS_READ",
        "LINKEDIN_PEOPLE_PER_POST",
    }
    for key in nonnegative_integers | positive_integers:
        value = values[key]
        minimum = 1 if key in positive_integers else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("Private contact count limits must be valid integers")
    numbers = LIMIT_NAMES - nonnegative_integers - positive_integers - {"LINKEDIN_POSTED_WITHIN"}
    for key in numbers:
        value = values[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(
                "Private contact spending and credit limits must be finite and nonnegative"
            )
    if not 0 < values["APIFY_MONTHLY_CAP_SHARE"] <= 1:
        raise ValueError(
            "Private contact monthly share must be greater than zero and no more than one"
        )
    if values["APIFY_USD_FOR_LINKEDIN_POSTS"] > values["APIFY_USD_PER_PEOPLE_SEARCH"]:
        raise ValueError("Private post-search limit exceeds the complete search limit")
    if values["LINKEDIN_POSTS_READ"] > values["LINKEDIN_POSTS_SEARCHED"]:
        raise ValueError("Private post-read count exceeds the post-search count")
    if values["WEB_SEARCHES_PER_LOOK_UP"] > 4:
        raise ValueError("Private person lookup exceeds the available query templates")
    window = values["LINKEDIN_POSTED_WITHIN"]
    if not isinstance(window, str) or not window.strip() or len(window) > 64:
        raise ValueError("Private post-search time window is required")
    # Provider prices and minimum accepted charge are technical API constraints.
    search = round(values["LINKEDIN_POSTS_SEARCHED"] * 0.002, 3)
    reads = 2 * values["LINKEDIN_POSTS_READ"]
    per_read = round((values["APIFY_USD_FOR_LINKEDIN_POSTS"] - search) / reads, 3)
    if (
        search < 0.01
        or per_read < 0.01
        or search + reads * per_read > values["APIFY_USD_FOR_LINKEDIN_POSTS"] + 1e-9
    ):
        raise ValueError(
            "Private post-search limit cannot fund the configured reads within the provider minimums"
        )
    return values


@lru_cache(maxsize=1)
def load_limits(path=None):
    """Read literal private data; never execute the file or load the public example."""
    target = Path(path) if path else Path(__file__).with_name("limits.py")
    try:
        tree = ast.parse(target.read_text())
        assignments = [
            node
            for node in tree.body
            if not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        ]
        if len(assignments) != 1:
            raise ValueError
        node = assignments[0]
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
            or node.targets[0].id != "LIMITS"
        ):
            raise ValueError
        values = ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, TypeError) as error:
        raise ValueError("Private contact limits are missing or invalid") from error
    return validate_limits(values)
