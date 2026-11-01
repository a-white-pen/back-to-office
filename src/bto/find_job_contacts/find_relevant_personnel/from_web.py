"""Find LinkedIn profiles through recruiter and team searches on Brave.

Keep a profile only when its search-result title names the employer. These
matches are provider-inferred because a company name does not prove identity.
A headline containing only the employer's name supplies no job title."""

from ...research_job_listing import brave_search
from .. import config
from ..identify_company import text_names_company
from .classify_found_persons import FoundPerson, plain_letters

RESULTS_PER_SEARCH = 20


def find_people_on_the_web(company_name, job_words, place):
    """LinkedIn profiles found by web search whose title names the company."""
    if not company_name:
        return []
    searches = ["recruiter OR talent"]
    team_words = job_words[1:3] or job_words[:1]
    if team_words:
        searches.append(" OR ".join(f'"{word}"' for word in team_words))
    people, seen = [], set()
    for words in searches:
        query = f'site:linkedin.com/in "{company_name}" {place} {words}'.replace("  ", " ")
        # A refused search answers nothing; the LinkedIn searches still run.
        answers = brave_search.search(query, key_name=config.BRAVE_FIND_PEOPLE_API_KEY,
                                      count=RESULTS_PER_SEARCH)
        for answer in answers:
            url = answer.get("url") or ""
            title = (answer.get("title") or "").replace(" | LinkedIn", "")
            name, _, headline = title.partition(" - ")
            if ("linkedin.com/in/" not in url or url in seen or not name.strip()
                    or not headline.strip() or not text_names_company(title, company_name)):
                continue
            seen.add(url)
            headline = " ".join(headline.split())
            people.append(FoundPerson(
                name=" ".join(name.split()), found_by="web", provenance="provider_inferred",
                title=None if _only_names_company(headline, company_name) else headline,
                linkedin_url=url))
    return people


def _only_names_company(headline, company_name):
    """True when the headline says nothing but the company's name."""
    def letters(text):
        return "".join(c for c in plain_letters(text) if c.isalnum())
    return letters(headline) == letters(company_name)
