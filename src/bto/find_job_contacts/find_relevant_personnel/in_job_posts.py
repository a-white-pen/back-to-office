"""Find staff LinkedIn posts about the vacancy and people interacting with them.

Match the listing title while keeping seniority, then read reactions and
comments for the first matching posts. Only people whose headline names the
employer can be looked up. Apify runs share this step's spending limit."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from ..identify_company import text_names_company
from ..providers import apify
from .classify_found_persons import PLACE_AND_JOINING_WORDS, FoundPerson, plain_letters

log = logging.getLogger(__name__)

# Current provider pricing per post or person; private limits choose the volume.
POST_UNIT_USD = 0.002
# The post-search and post-comments Actors refuse a cap below USD 0.01.
MINIMUM_USD = 0.01
# Short forms of seniority that posts use for the listing's word.
SHORT_FORMS = {"sr": "senior", "snr": "senior", "jr": "junior", "mgr": "manager"}


@dataclass
class FoundPost:
    """One post about the listing. `reacted_by` and `commented_by` hold
    {name, headline, linkedin_url, works_at_company} for each person."""

    post_url: str
    author: FoundPerson
    repost_of_name: str | None
    posted_at: datetime | None
    post_text: str | None
    reacted_by: list = field(default_factory=list)
    commented_by: list = field(default_factory=list)


def _search_charge():
    """Bound the post search by its configured volume and provider unit price."""
    return round(config.LINKEDIN_POSTS_SEARCHED * POST_UNIT_USD, 3)


def _read_charge():
    """Divide the remaining private step budget among reactions and comments."""
    return round((config.APIFY_USD_FOR_LINKEDIN_POSTS - _search_charge()) /
                 (2 * config.LINKEDIN_POSTS_READ), 3)


def find_posts_about_the_listing(company_name, job_title, listing_title, runs):
    """(posts, people): the posts about the listing, and every author or
    reactor or commenter who works at the company, as FoundPerson records.
    LinkedIn is searched for `job_title`, the job title without seniority or
    place, such as "Analytics Engineer"; a post must name `listing_title`, the
    title as listed. Raises apify.ApifyRefused when Apify fails."""
    if not company_name or not job_title:
        return [], []
    rows = runs.run(
        apify.POST_SEARCH,
        {"searchQueries": [job_title], "authorsCompanies": [company_name],
         "maxPosts": config.LINKEDIN_POSTS_SEARCHED, "sortBy": "relevance", "postedLimit": config.LINKEDIN_POSTED_WITHIN,
         "profileScraperMode": "short"},
        max_charge_usd=_search_charge(), minimum_charge_usd=MINIMUM_USD,
    ) or []
    posts = [post for row in rows
             if (post := _post_from_row(row, listing_title or job_title)) is not None][:config.LINKEDIN_POSTS_READ]
    people = [post.author for post in posts]
    for post in posts:
        post.reacted_by = _read_people_who_reacted_or_commented(
            apify.POST_REACTIONS, post.post_url, company_name, runs)
        post.commented_by = _read_people_who_reacted_or_commented(
            apify.POST_COMMENTS, post.post_url, company_name, runs)
        for entry in post.reacted_by + post.commented_by:
            if entry["works_at_company"]:
                people.append(FoundPerson(
                    name=entry["name"], found_by="posts", provenance="provider_inferred",
                    title=entry["headline"], linkedin_url=entry["linkedin_url"],
                    only_in_posts=True))
    log.info("LinkedIn posts company=%r posts=%d people_at_company=%d",
             company_name, len(posts), len(people))
    return posts, people


def _post_from_row(row, listing_title):
    """A FoundPost when the row is a post about the listing, else None."""
    url = row.get("linkedinUrl")
    author = row.get("author") or {}
    repost = row.get("repost") or {}
    text = row.get("content") or repost.get("content") or None
    written = (row.get("content"), repost.get("content"),
               (row.get("article") or {}).get("title"), (repost.get("article") or {}).get("title"))
    if not url or not author.get("name") or not any(
            _names_title(part, listing_title) for part in written):
        return None
    posted = (row.get("postedAt") or {}).get("date")
    return FoundPost(
        post_url=url,
        author=FoundPerson(
            name=" ".join(author["name"].split()), found_by="posts",
            provenance="provider_inferred", title=author.get("info") or None,
            linkedin_url=_without_query(author.get("linkedinUrl"))),
        repost_of_name=((repost.get("author") or {}).get("name") or None) if repost else None,
        posted_at=datetime.fromisoformat(posted.replace("Z", "+00:00")) if posted else None,
        post_text=text,
    )


def _names_title(text, listing_title):
    """True when `text` has the listing title's words together and in order."""
    wanted = _title_words(re.sub(r"\(.*?\)", " ", listing_title or ""))
    words = _title_words(text)
    return bool(wanted) and any(words[start:start + len(wanted)] == wanted
                                for start in range(len(words) - len(wanted) + 1))


def _title_words(text):
    """Lower-case words without punctuation, place or joining words:
    "Senior Analyst - APAC Growth" gives ["senior", "analyst", "growth"]."""
    words = re.findall(r"[a-z][a-z&+]*", plain_letters(text))
    return [SHORT_FORMS.get(word, word) for word in words if word not in PLACE_AND_JOINING_WORDS]


def _read_people_who_reacted_or_commented(actor_id, post_url, company_name, runs):
    """The people who reacted to or commented on one post, in LinkedIn's order."""
    rows = runs.run(
        actor_id,
        {"posts": [post_url], "maxItems": config.LINKEDIN_PEOPLE_PER_POST, "profileScraperMode": "short"},
        max_charge_usd=_read_charge(), minimum_charge_usd=MINIMUM_USD,
    ) or []
    people = []
    for row in rows:
        # Read actor-shaped records; rows without a person's name are skipped.
        person = row.get("actor") or {}
        name = " ".join((person.get("name") or "").split())
        if not name:
            continue
        headline = person.get("position") or person.get("headline") or ""
        people.append({
            "name": name,
            "headline": headline or None,
            "linkedin_url": _without_query(person.get("linkedinUrl")),
            "works_at_company": text_names_company(headline, company_name),
        })
    return people


def _without_query(url):
    return (url or "").split("?")[0] or None
